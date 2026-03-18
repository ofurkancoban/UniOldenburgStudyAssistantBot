import aiohttp
import asyncio
from bs4 import BeautifulSoup
import logging
import os
import hmac
import hashlib
import time
import struct
import base64
import json
import re
from urllib.parse import urljoin, urlparse, parse_qs

class StudIPSession:
    """
    An async, robust, 100% browser-less session handler for Stud.IP.
    Using aiohttp for high performance and non-blocking operation in bots.
    """
    
    def __init__(self, username, password, totp_secret, cookie_file="studip_cookies.json"):
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.cookie_file = cookie_file
        self.session = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        self.logger = logging.getLogger("StudIPSession")
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers)
            await self.load_cookies()

    def _get_totp_token(self):
        if not self.totp_secret:
            return ""
        try:
            secret = self.totp_secret.replace(' ', '').upper()
            key = base64.b32decode(secret)
            msg = struct.pack(">Q", int(time.time()) // 30)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            h = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1000000
            return "{:06d}".format(h)
        except Exception:
            return ""

    async def save_cookies(self):
        if not self.session: return
        cookies = {}
        for cookie in self.session.cookie_jar:
             cookies[cookie.key] = cookie.value
        with open(self.cookie_file, 'w') as f:
            json.dump(cookies, f)
        self.logger.info(f"Cookies saved to {self.cookie_file}")

    async def load_cookies(self):
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                    if self.session:
                        self.session.cookie_jar.update_cookies(cookies)
                self.logger.info(f"Cookies loaded from {self.cookie_file}")
                return True
            except Exception as e:
                self.logger.error(f"Failed to load cookies: {e}")
        return False

    async def is_logged_in(self):
        """
        Check if the session is currently authenticated.
        """
        await self.ensure_session()
        try:
            # Check a known authenticated page
            async with self.session.get("https://elearning.uni-oldenburg.de/dispatch.php/start", allow_redirects=True) as r:
                text = await r.text()
                url = str(r.url)
                
                # Markers of NOT being logged in
                is_guest = '"username":"nobody"' in text or '"me":{"username":"nobody"}' in text
                is_login_page = "/dispatch.php/login" in url or "index.php?again=yes" in url
                
                # Markers of BEING logged in
                has_logout = "logout" in text.lower()
                has_sidebar = "sidebar-widget" in text
                has_my_courses = "/dispatch.php/my_courses" in text
                has_user_id = "STUDIP.USER_ID" in text
                
                if (has_logout or has_sidebar or has_my_courses or has_user_id) and not is_guest and not is_login_page:
                    self.logger.debug(f"is_logged_in: True (markers found at {url})")
                    return True
                
                self.logger.info(f"is_logged_in: False (markers missing or Guest state at {url})")
                return False
        except Exception as e:
            self.logger.error(f"is_logged_in check failed: {e}")
            return False

    async def login(self, force=False):
        async with self._lock:
            await self.ensure_session()
            if not force and await self.is_logged_in():
                 self.logger.info("Session already logged in.")
                 return True

            self.logger.info("Starting fresh async browser-less login (NetIQ optimized)...")
            
            elearn = "https://elearning.uni-oldenburg.de"
            start_url = f"{elearn}/dispatch.php/my_courses"
            login_link_cancel = f"{elearn}/dispatch.php/login?again=yes&sso=oidc&cancel_login=1"
            
            async def follow_redirects(url, text, tag, depth=0):
                if depth > 20: 
                    self.logger.warning(f"[{tag}] Max redirect depth reached at {url}")
                    return url, text
                
                soup = BeautifulSoup(text, "html.parser")

                # 1. JS Redirect
                js_redirect = re.search(r"window\.location\.(?:href|assign|replace)\s*=\s*['\"]([^'\"]+)['\"]", text)
                if js_redirect:
                    nxt = urljoin(url, js_redirect.group(1))
                    self.logger.info(f"[{tag}] Following JS redirect: {nxt}")
                    async with self.session.get(nxt, headers={"Referer": url}) as r:
                         return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)

                # 2. Meta Refresh
                meta = soup.find("meta", attrs={"http-equiv": lambda x: x and x.lower() == "refresh"})
                if meta:
                    content = meta.get("content", "")
                    if "url=" in content.lower():
                        nxt = urljoin(url, content.lower().split("url=")[1].strip().strip("'\""))
                        self.logger.info(f"[{tag}] Following Meta Refresh: {nxt}")
                        async with self.session.get(nxt, headers={"Referer": url}) as r:
                            return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)
                
                # 3. AJAX Content Load (NetIQ specific)
                ajax_load = re.search(r"getToContent\(['\"]([^'\"]+)['\"]\s*,\s*['\"][^'\"]+['\"]\)", text)
                if ajax_load:
                    ajax_url = urljoin(url, ajax_load.group(1))
                    self.logger.info(f"[{tag}] Following NetIQ AJAX content load: {ajax_url}")
                    async with self.session.get(ajax_url, headers={"Referer": url}) as r:
                         return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)
                
                # 4. Script-based form auto-submit (e.g., NetIQ authCodeForm)
                form_submit = re.search(r"document\.forms\[['\"]([^'\"]+)['\"]\]\.submit\(\)", text)
                if form_submit:
                    form_name = form_submit.group(1)
                    form = soup.find("form", attrs={"name": form_name})
                    if form:
                        inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                        action = urljoin(url, form.get("action") or "")
                        method = (form.get("method") or "POST").upper()
                        self.logger.info(f"[{tag}] Script-submitting form '{form_name}' to {action}")
                        if method == "POST":
                            async with self.session.post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)
                        else:
                            async with self.session.get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)

                # 5. OAuth/SAML Auth Handoff Forms (Auto-submit via presence of sensitive keys)
                for form in soup.find_all("form"):
                    inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                    keys = set(k.lower() for k in inputs.keys())
                    if keys & {"code", "state", "id_token", "samlresponse", "relaystate", "wa", "wresult"}:
                        action = urljoin(url, form.get("action") or "")
                        method = (form.get("method") or "POST").upper()
                        self.logger.info(f"[{tag}] Auto-submitting auth handoff form to {action}")
                        if method == "POST":
                            async with self.session.post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)
                        else:
                            async with self.session.get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(str(r.url), await r.text(), tag, depth + 1)

                self.logger.debug(f"[{tag}] No more redirects detected at {url}")
                return url, text

            # Helper for form submission
            def get_payload(soup, overrides=None):
                form = soup.find("form")
                if not form: return None, None, None
                action = form.get("action") or ""
                method = (form.get("method") or "POST").upper()
                inputs = {}
                for inp in form.find_all("input"):
                    name = inp.get("name")
                    if name:
                        inputs[name] = inp.get("value") or ""
                if overrides:
                    inputs.update(overrides)
                return action, method, inputs

            # 1. Start flow
            async with self.session.get(start_url) as r:
                curr_url, curr_text = str(r.url), await r.text()
            
            async with self.session.get(login_link_cancel, headers={"Referer": start_url}) as r:
                curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "entry")

            # 2. Main Login Loop
            prev_text = ""
            for i in range(15):
                if curr_text == prev_text:
                    self.logger.warning("Page content stayed identical. Breaking loop.")
                    break
                prev_text = curr_text
                
                soup = BeautifulSoup(curr_text, "html.parser")
                page_text = curr_text.lower()
                
                # Dump for debug
                with open(f"/tmp/login_step_{i}.html", "w") as f:
                    f.write(f"URL: {curr_url}\n\n" + curr_text)
                self.logger.info(f"Step {i}: URL {curr_url}")
                
                if await self.is_logged_in():
                    self.logger.info("✅ Login successful.")
                    await self.save_cookies()
                    return True

                # Identification Logic
                
                # A. Legacy Login Page (Ecom fields)
                if "ecom_user_id" in page_text and "ecom_password" not in page_text:
                    self.logger.info(f"Step {i+1}: Username Submission")
                    action, method, payload = get_payload(soup, {"Ecom_User_ID": self.username, "loginButton2": "true"})
                    action = urljoin(curr_url, action)
                    async with self.session.post(action, data=payload, headers={"Referer": curr_url}) as r:
                        curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "user")
                    continue

                if "ecom_password" in page_text:
                    self.logger.info(f"Step {i+1}: Password Submission (Legacy)")
                    action, method, payload = get_payload(soup, {"Ecom_Password": self.password, "loginButton2": "true"})
                    action = urljoin(curr_url, action)
                    async with self.session.post(action, data=payload, headers={"Referer": curr_url}) as r:
                        curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "pass")
                    continue

                # B. contract Page (Unified)
                if "/osp/a/TOP/auth/app/contract" in curr_url or "/osp/a/TOP/auth/app/contract" in curr_text:
                    title_area = soup.find(id="authenticationAreaTitle")
                    title_text = title_area.get_text().lower() if title_area else ""
                    # Check if it's OTP based on title OR specific text
                    is_otp = any(x in title_text for x in ["one time", "otp", "code", "authenticator"])
                    # If title doesn't help, check whole page but more conservatively
                    if not is_otp and not ("password" in title_text):
                        is_otp = any(x in page_text for x in ["one time password", "totp code", "google authenticator"])

                    if is_otp:
                        self.logger.info(f"Step {i+1}: TOTP Submission (Contract)")
                        token = self._get_totp_token()
                        action, method, payload = get_payload(soup, {"nffc": token, "loginButton2": "Next"})
                    else:
                        self.logger.info(f"Step {i+1}: Password Submission (Contract)")
                        action, method, payload = get_payload(soup, {"nffc": self.password, "loginButton2": "Next"})
                    
                    action = urljoin(curr_url, action)
                    async with self.session.post(action, data=payload, headers={"Referer": curr_url}) as r:
                        curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "contract")
                    continue

                # C. Unhandled
                self.logger.warning(f"Step {i}: Unhandled state. URL: {curr_url}")
                if i > 10: break

            if await self.is_logged_in():
                 await self.save_cookies()
                 return True
            self.logger.error("Login failed after max steps.")
            return False

    async def get(self, url, **kwargs):
        await self.ensure_session()
        return self.session.get(url, **kwargs)

    async def post(self, url, **kwargs):
        await self.ensure_session()
        return self.session.post(url, **kwargs)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.logger.info("Session closed.")
