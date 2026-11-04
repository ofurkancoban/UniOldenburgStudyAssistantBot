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
from yarl import URL

import studip_oauth

class StudIPSession:
    """
    An async, robust, 100% browser-less session handler for Stud.IP.
    Using aiohttp for high performance and non-blocking operation in bots.
    """
    
    def __init__(self, username, password, totp_secret, cookie_file="studip_cookies.json", totp_code=None):
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        # A pre-generated 6-digit code (typed by the user in the /login form).
        # Used once, then cleared - the secret-based generator takes over.
        self.totp_code = totp_code
        self.cookie_file = cookie_file
        self.session = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        self.logger = logging.getLogger("StudIPSession")
        self._lock = asyncio.Lock()
        self.last_interactive_activity = 0.0
        self.explicitly_logged_out = False

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
        if self.totp_code:
            code, self.totp_code = self.totp_code, None  # one-shot
            return code
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
        if not self.session:
            return
        entries = []
        for cookie in self.session.cookie_jar:
            entries.append({
                "key": cookie.key,
                "value": cookie.value,
                "domain": cookie["domain"] or "elearning.uni-oldenburg.de",
                "path": cookie["path"] or "/",
            })
        try:
            from studip_vault import write_vault_file
            write_vault_file(self.cookie_file, entries, mode=0o600)
            self.logger.info(f"Encrypted cookies saved to {self.cookie_file} (mode 0600)")
        except Exception as exc:
            self.logger.error(f"Failed to save encrypted cookies to {self.cookie_file}: {exc}")

    async def load_cookies(self):
        if os.path.exists(self.cookie_file):
            try:
                from studip_vault import read_vault_file
                data = read_vault_file(self.cookie_file, auto_migrate=True)
                if data is None:
                    with open(self.cookie_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                if self.session and data:
                    if isinstance(data, list):
                        for entry in data:
                            key = entry.get("key")
                            val = entry.get("value")
                            dom = (entry.get("domain") or "elearning.uni-oldenburg.de").lstrip(".")
                            path = entry.get("path") or "/"
                            if key is not None and val is not None:
                                self.session.cookie_jar.update_cookies(
                                    {key: val},
                                    response_url=URL(f"https://{dom}{path}"),
                                )
                    elif isinstance(data, dict):
                        for key, val in data.items():
                            if key in ("JSESSIONID", "UrnNovellNidpClusterMemberId", "_PA_SDK_SSO_") or key.startswith("x-oidp-"):
                                dom = "idp.uni-oldenburg.de"
                            else:
                                dom = "elearning.uni-oldenburg.de"
                            self.session.cookie_jar.update_cookies(
                                {key: val},
                                response_url=URL(f"https://{dom}/"),
                            )
                self.logger.info(f"Encrypted cookies loaded from {self.cookie_file}")
                return True
            except Exception as e:
                self.logger.error(f"Failed to load cookies: {e}")
        return False

    async def is_html_logged_in(self) -> bool:
        """Check if the browser-less HTML cookie session is currently authenticated."""
        await self.ensure_session()
        try:
            async with self.session.get("https://elearning.uni-oldenburg.de/dispatch.php/start", allow_redirects=True) as r:
                text = await r.text()
                url = str(r.url)

                is_guest = '"username":"nobody"' in text or '"me":{"username":"nobody"}' in text
                is_login_page = "/dispatch.php/login" in url or "index.php?again=yes" in url

                has_logout = "logout" in text.lower()
                has_sidebar = "sidebar-widget" in text
                has_my_courses = "/dispatch.php/my_courses" in text
                has_user_id = "STUDIP.USER_ID" in text

                if (has_logout or has_sidebar or has_my_courses or has_user_id) and not is_guest and not is_login_page:
                    self.logger.debug(f"is_html_logged_in: True (markers found at {url})")
                    return True

                self.logger.info(f"is_html_logged_in: False (markers missing or Guest state at {url})")
                return False
        except Exception as e:
            self.logger.error(f"is_html_logged_in check failed: {e}")
            return False

    async def heal_session(self) -> bool:
        """Attempt self-healing silent SSO renewal using stored NetIQ SSO cookies."""
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            return False
        if not os.path.exists(self.cookie_file):
            return False

        async with self._lock:
            self.logger.info("🔧 [Self-Healing] Attempting silent SSO renewal using warm NetIQ cookies...")
            await self.ensure_session()
            elearn = "https://elearning.uni-oldenburg.de"
            login_link_cancel = f"{elearn}/dispatch.php/login?again=yes&sso=oidc&cancel_login=1"
            try:
                await self._run_sso_flow(login_link_cancel, referer=f"{elearn}/dispatch.php/my_courses")
                if await self.is_html_logged_in():
                    self.explicitly_logged_out = False
                    await self.save_cookies()
                    self.logger.info("✅ [Self-Healing] Session successfully healed and restored!")
                    return True
                else:
                    self.logger.warning("⚠️ [Self-Healing] Silent SSO flow finished but login markers missing.")
                    return False
            except Exception as exc:
                self.logger.warning(f"⚠️ [Self-Healing] Silent SSO flow failed: {exc}")
                return False

    async def is_logged_in(self, allow_oauth: bool = True) -> bool:
        """Check if authenticated (via HTML cookie, automatic self-healing, or OAuth2)."""
        if await self.is_html_logged_in():
            return True

        # Automatic self-healing: if HTML session dropped, silently renew via warm NetIQ cookies
        if os.path.exists(self.cookie_file):
            if await self.heal_session():
                return True

        use_oauth = studip_oauth.oauth_enabled()
        if allow_oauth and use_oauth:
            try:
                token = await studip_oauth.get_access_token()
                if token:
                    return True
            except Exception as exc:
                self.logger.warning(f"OAuth check failed: {exc}")
        return False

    async def login(self, force=False):
        async with self._lock:
            await self.ensure_session()
            if not force and (
                self.explicitly_logged_out
                or (time.time() - self.last_interactive_activity < 180)
            ):
                return await self.is_logged_in()

            if not force and await self.is_html_logged_in():
                self.logger.info("Session already logged in.")
                self.explicitly_logged_out = False
                return True

            has_headless_creds = bool(
                self.username and self.password and (self.totp_secret or self.totp_code)
            )
            use_oauth = studip_oauth.oauth_enabled()
            if not has_headless_creds and not os.path.exists(self.cookie_file):
                if not force and use_oauth:
                    try:
                        if await studip_oauth.get_access_token():
                            return True
                    except Exception:
                        pass
                return False

            self.logger.info("Starting fresh async browser-less login (NetIQ optimized)...")

            elearn = "https://elearning.uni-oldenburg.de"
            start_url = f"{elearn}/dispatch.php/my_courses"
            login_link_cancel = f"{elearn}/dispatch.php/login?again=yes&sso=oidc&cancel_login=1"

            # 1. Prime the session before triggering the login flow.
            try:
                async with self.session.get(start_url) as r:
                    pass
                await self._run_sso_flow(login_link_cancel, referer=start_url)
                self.explicitly_logged_out = False
                await self.save_cookies()
                self.logger.info("✅ Login successful.")
                return True
            except Exception as e:
                self.logger.warning(f"SSO login flow did not complete: {e}")
                if not force and use_oauth:
                    try:
                        if await studip_oauth.get_access_token():
                            self.logger.info("Using active OAuth2 token session.")
                            return True
                    except Exception:
                        pass
                return False

    async def keep_alive(self) -> None:
        """Keep Stud.IP web session and NetIQ IDP cookies warm; heal session if needed."""
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            return
        use_oauth = studip_oauth.oauth_enabled()
        if not os.path.exists(self.cookie_file) and (not use_oauth or studip_oauth.load_tokens() is None):
            return
        await self.ensure_session()
        if use_oauth:
            try:
                await studip_oauth.get_access_token()
            except Exception as exc:
                self.logger.debug(f"OAuth keep-alive skipped: {exc}")

        for sso_url in (
            "https://login.uni-oldenburg.de/nidp/app",
            "https://auth.uni-oldenburg.de/osp/a/TOP/auth/app/",
            "https://stums.uni-oldenburg.de/qisserver/api/identityprovider/redirect/2/login",
        ):
            try:
                async with self.session.get(sso_url, allow_redirects=True, timeout=15) as _:
                    pass
            except Exception as exc:
                self.logger.debug(f"SSO keep-alive skipped for {sso_url}: {exc}")

        try:
            if not await self.is_html_logged_in():
                # Silently heal session using warm NetIQ cookies
                await self.heal_session()
            else:
                await self.save_cookies()
        except Exception as exc:
            self.logger.debug(f"Web session keep-alive renewal skipped: {exc}")

    async def logout(self) -> None:
        """Terminate Stud.IP + NetIQ SSO sessions, clear cookies and OAuth2 tokens."""
        async with self._lock:
            self.explicitly_logged_out = True
            self.username = ""
            self.password = ""
            self.totp_secret = ""
            self.totp_code = None
            if self.session and not self.session.closed:
                for url in (
                    "https://elearning.uni-oldenburg.de/dispatch.php/logout",
                    "https://auth.uni-oldenburg.de/osp/a/TOP/auth/app/logout",
                    "https://login.uni-oldenburg.de/nidp/app/logout",
                ):
                    try:
                        async with self.session.get(url, allow_redirects=True, timeout=10) as _:
                            pass
                    except Exception as exc:
                        self.logger.debug(f"Upstream logout request skipped for {url}: {exc}")
                self.session.cookie_jar.clear()

            for path in (self.cookie_file, f"{self.cookie_file}.tmp"):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError as exc:
                    self.logger.warning(f"Could not remove {path}: {exc}")

            studip_oauth.clear_tokens()
            self.logger.info("🚪 Stud.IP session, SSO cookies, and OAuth2 tokens cleared.")

    async def sso_handoff(self, entry_url: str, success_host: str, referer: str = None) -> str:
        """Run the same NetIQ SSO state machine as `login()`, but to reach a different
        relying party (e.g. stums.uni-oldenburg.de) instead of elearning.uni-oldenburg.de.

        A valid elearning session does not guarantee this succeeds in one plain
        HTTP redirect: the underlying NetIQ SSO trust can have its own, independent
        timeout, in which case this transparently falls through to the very same
        credential-entry steps `login()` uses (Ecom fields, TOTP/contract page).

        Returns the final URL once `success_host` is reached, or raises RuntimeError.
        """
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            raise RuntimeError("Interactive login or logout active; skipping background SSO handoff.")
        await self.ensure_session()

        async def is_done(url, _text):
            return urlparse(url).netloc == success_host

        final_url, _ = await self._run_sso_flow(entry_url, referer=referer, is_done=is_done)
        await self.save_cookies()
        return final_url

    async def _run_sso_flow(self, entry_url: str, referer: str = None, is_done=None):
        """Shared NetIQ SSO redirect + credential-entry state machine.

        `is_done(url, text)` decides when the flow has reached its destination;
        defaults to `self.is_logged_in()` (elearning-specific) when omitted, which
        is what `login()` uses. Returns (final_url, final_text) on success, raises
        RuntimeError if the flow gets stuck or exhausts its step budget.
        """
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

        async def check_done(url, text):
            if is_done:
                return await is_done(url, text)
            return await self.is_html_logged_in()

        # 1. Entry
        async with self.session.get(entry_url, headers={"Referer": referer} if referer else None) as r:
            curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "entry")

        # 2. Main state-machine loop
        prev_text = ""
        for i in range(15):
            if curr_text == prev_text:
                self.logger.warning("Page content stayed identical. Breaking loop.")
                break
            prev_text = curr_text

            soup = BeautifulSoup(curr_text, "html.parser")
            page_text = curr_text.lower()

            self.logger.info(f"Step {i}: URL {curr_url}")

            if await check_done(curr_url, curr_text):
                return curr_url, curr_text

            # Identification Logic

            # A. Legacy Login Page (Ecom fields)
            if "ecom_user_id" in page_text and "ecom_password" not in page_text:
                if not self.username:
                    raise RuntimeError("Interactive login required (no stored username)")
                self.logger.info(f"Step {i+1}: Username Submission")
                action, method, payload = get_payload(soup, {"Ecom_User_ID": self.username, "loginButton2": "true"})
                action = urljoin(curr_url, action)
                async with self.session.post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "user")
                continue

            if "ecom_password" in page_text:
                if not self.password:
                    raise RuntimeError("Interactive login required (no stored password)")
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
                    token = self._get_totp_token()
                    if not token:
                        raise RuntimeError("Interactive login required (no 2FA token available)")
                    self.logger.info(f"Step {i+1}: TOTP Submission (Contract)")
                    action, method, payload = get_payload(soup, {"nffc": token, "loginButton2": "Next"})
                else:
                    if not self.password:
                        raise RuntimeError("Interactive login required (no stored password)")
                    self.logger.info(f"Step {i+1}: Password Submission (Contract)")
                    action, method, payload = get_payload(soup, {"nffc": self.password, "loginButton2": "Next"})

                action = urljoin(curr_url, action)
                async with self.session.post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(str(r.url), await r.text(), "contract")
                continue

            # C. Unhandled
            self.logger.warning(f"Step {i}: Unhandled state. URL: {curr_url}")
            if i > 10: break

        if await check_done(curr_url, curr_text):
            return curr_url, curr_text
        raise RuntimeError(f"SSO flow did not complete; stuck at {curr_url}")

    async def _bearer_headers(self, url: str) -> dict:
        """OAuth bearer header for JSON:API requests (only if USE_STUDIP_OAUTH is enabled).

        Stud.IP's JSON:API accepts session cookies directly; OAuth tokens are only
        used when USE_STUDIP_OAUTH is explicitly enabled.
        """
        use_oauth = studip_oauth.oauth_enabled()
        if not use_oauth:
            return {}
        if "/jsonapi.php/" not in url:
            return {}
        try:
            token = await studip_oauth.get_access_token()
        except Exception as exc:  # never let a token problem break a request
            self.logger.warning(f"OAuth token unavailable: {exc}")
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _merged_headers(self, url: str, extra: dict | None) -> dict | None:
        headers = dict(extra or {})
        headers.update(await self._bearer_headers(url))
        return headers or None

    async def get(self, url, **kwargs):
        await self.ensure_session()
        kwargs["headers"] = await self._merged_headers(url, kwargs.get("headers"))
        return self.session.get(url, **kwargs)

    async def post(self, url, **kwargs):
        await self.ensure_session()
        kwargs["headers"] = await self._merged_headers(url, kwargs.get("headers"))
        return self.session.post(url, **kwargs)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.logger.info("Session closed.")
