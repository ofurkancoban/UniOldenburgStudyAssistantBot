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
from urllib.parse import urljoin, urlparse, urlsplit
from yarl import URL

import studip_oauth
from studip_utils import redact_url, route_proxy_url, unroute_proxy_url

# Wait this long after the 1st, 2nd, 3rd, 4th+ failed password/TOTP login in a
# row before the next automatic attempt. Without it every button tap and
# watcher cycle re-sent the password, which can get the account locked by
# NetIQ after a password change.
LOGIN_BACKOFF_SECONDS = (60, 300, 900, 3600)
# A positive HTML login check is trusted for this long, so handlers don't
# download a whole Stud.IP page before doing any actual work.
LOGIN_CHECK_CACHE_SECONDS = 60


class StudIPSession:
    """
    An async, robust, 100% browser-less session handler for Stud.IP.
    Using aiohttp for high performance and non-blocking operation in bots.
    """
    
    def __init__(self, username, password, totp_secret, cookie_file="studip_cookies.json", totp_code=None, user_id=None):
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        # A pre-generated 6-digit code (typed by the user in the /login form).
        # Used once, then cleared - the secret-based generator takes over.
        self.totp_code = totp_code
        self.cookie_file = cookie_file
        self.user_id = user_id
        self.session = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Encoding': 'gzip, deflate',
        }
        self.logger = logging.getLogger("StudIPSession")
        self._lock = asyncio.Lock()
        self.last_interactive_activity = 0.0
        self.explicitly_logged_out = False
        self.login_failures = 0
        self.login_blocked_until = 0.0
        self._logged_in_until = 0.0
        # Set by exam_reminder when StuMS refuses the login; background StuMS
        # checks pause until then (a new interactive login clears it).
        self.stums_blocked_until = 0.0

    def reset_login_backoff(self) -> None:
        """Forget earlier failed logins (after a successful interactive login)."""
        self.login_failures = 0
        self.login_blocked_until = 0.0
        self.stums_blocked_until = 0.0

    def invalidate_login_cache(self) -> None:
        self._logged_in_until = 0.0

    def _record_login_failure(self) -> None:
        self.login_failures += 1
        delay = LOGIN_BACKOFF_SECONDS[min(self.login_failures, len(LOGIN_BACKOFF_SECONDS)) - 1]
        self.login_blocked_until = time.time() + delay
        self.logger.warning(
            "Automatic login failed %sx in a row - next attempt in %s min", self.login_failures, delay // 60
        )

    async def __aenter__(self):
        await self.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
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
            logging.warning("TOTP code generation failed (check TOTP_SECRET)", exc_info=True)
            return ""

    def _sync_worker_cookies(self):
        """Mirror cookies for university domains to the Cloudflare Worker URL in cookie jar."""
        worker_url = os.getenv("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
        if not worker_url or not self.session:
            return
        try:
            target_hosts = (
                "elearning.uni-oldenburg.de",
                "idp.uni-oldenburg.de",
                "login.uni-oldenburg.de",
                "auth.uni-oldenburg.de",
                "stums.uni-oldenburg.de",
                "studip.uni-oldenburg.de",
            )
            for host in target_hosts:
                cookies = self.session.cookie_jar.filter_cookies(URL(f"https://{host}/"))
                if cookies:
                    cookie_dict = {k: (m.value if hasattr(m, "value") else str(m)) for k, m in cookies.items()}
                    self.session.cookie_jar.update_cookies(cookie_dict, response_url=URL(worker_url))
        except Exception as exc:
            self.logger.debug("Worker cookie sync skipped: %s", exc)

    def _capture_response_cookies(self, resp, target_host: str):
        """Extract Set-Cookie headers from proxy responses and map them back to university host."""
        if not self.session or not target_host:
            return
        try:
            worker_url = os.getenv("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
            set_cookies = resp.headers.getall("Set-Cookie", [])
            if not set_cookies:
                return

            from http.cookies import SimpleCookie
            for sc in set_cookies:
                c = SimpleCookie()
                try:
                    c.load(sc)
                except Exception:
                    continue
                for key, morsel in c.items():
                    val = morsel.value
                    if val is not None:
                        host_clean = target_host.split(":")[0].lower()
                        self.session.cookie_jar.update_cookies(
                            {key: val},
                            response_url=URL(f"https://{host_clean}/"),
                        )
                        if worker_url:
                            self.session.cookie_jar.update_cookies(
                                {key: val},
                                response_url=URL(worker_url),
                            )
        except Exception as exc:
            self.logger.debug("Failed capturing response cookies for %s: %s", target_host, exc)

    class _SRequestContext:
        def __init__(self, session_obj, method: str, url: str, **kwargs):
            self.session_obj = session_obj
            self.method = method
            self.url = str(url)
            self.kwargs = kwargs
            self._resp = None

        async def __aenter__(self):
            cookie_jar = self.session_obj.session.cookie_jar if self.session_obj.session else None
            routed_url, headers = route_proxy_url(self.url, self.kwargs.pop("headers", None), cookie_jar=cookie_jar)
            if headers:
                self.kwargs["headers"] = headers
            target_host = (urlsplit(self.url).hostname or "").lower()
            self._resp = await self.session_obj.session.request(self.method, routed_url, **self.kwargs)
            self.session_obj._capture_response_cookies(self._resp, target_host)
            return self._resp

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if self._resp:
                await self._resp.__aexit__(exc_type, exc_val, exc_tb)

    def _s_get(self, url, **kwargs):
        return self._SRequestContext(self, "GET", url, **kwargs)

    def _s_post(self, url, **kwargs):
        return self._SRequestContext(self, "POST", url, **kwargs)

    async def save_cookies(self):
        if not self.session:
            return
        worker_url = os.getenv("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
        worker_host = urlparse(worker_url).netloc.lower() if worker_url else ""

        entries = []
        seen = set()
        for cookie in self.session.cookie_jar:
            dom = cookie["domain"] or "elearning.uni-oldenburg.de"
            if worker_host and (dom.lower() == worker_host or dom.lower() == f".{worker_host}"):
                # Do not save synthetic worker cookies; real cookies are stored under university domains
                continue
            item_key = (cookie.key, dom, cookie["path"] or "/")
            if item_key in seen:
                continue
            seen.add(item_key)
            entries.append({
                "key": cookie.key,
                "value": cookie.value,
                "domain": dom,
                "path": cookie["path"] or "/",
            })
        self._sync_worker_cookies()
        if getattr(self, "user_id", None):
            try:
                from unibot import db
                db.save_user_credential(self.user_id, "cookies", entries)
                self.logger.info("Encrypted cookies saved to DB for user %s", self.user_id)
            except Exception as exc:
                self.logger.error("Failed to save cookies to DB for user %s: %s", self.user_id, exc)

            user_cookie_file = f"studip_cookies_{self.user_id}.json"
            if self.cookie_file != user_cookie_file and os.path.exists(user_cookie_file):
                try:
                    from studip_vault import write_vault_file
                    write_vault_file(user_cookie_file, entries, mode=0o600)
                except Exception:
                    pass

        if self.cookie_file:
            try:
                from studip_vault import write_vault_file
                write_vault_file(self.cookie_file, entries, mode=0o600)
                self.logger.info("Encrypted cookies saved to %s (mode 0600)", self.cookie_file)
            except Exception as exc:
                self.logger.error("Failed to save encrypted cookies to %s: %s", self.cookie_file, exc)

    async def load_cookies(self):
        data = None
        if getattr(self, "user_id", None):
            try:
                from unibot import db
                data = db.load_user_credential(self.user_id, "cookies")
                if data:
                    self.logger.info("Encrypted cookies loaded from DB for user %s", self.user_id)
            except Exception as exc:
                self.logger.warning("Failed to load cookies from DB for user %s: %s", self.user_id, exc)

        if data is None and self.cookie_file and os.path.exists(self.cookie_file):
            try:
                from studip_vault import read_vault_file
                data = read_vault_file(self.cookie_file, auto_migrate=True)
                if data is None:
                    with open(self.cookie_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                if data:
                    self.logger.info("Encrypted cookies loaded from %s", self.cookie_file)
            except Exception as e:
                self.logger.error("Failed to load cookies: %s", e)

        if self.session and data:
            try:
                worker_url = os.getenv("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
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
                            if worker_url:
                                self.session.cookie_jar.update_cookies(
                                    {key: val},
                                    response_url=URL(worker_url),
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
                        if worker_url:
                            self.session.cookie_jar.update_cookies(
                                {key: val},
                                response_url=URL(worker_url),
                            )
                self._sync_worker_cookies()
                return True
            except Exception as e:
                self.logger.error("Failed to apply cookies to session: %s", e)
        return False

    async def is_html_logged_in(self, use_cache: bool = True) -> bool:
        """Check if the browser-less HTML cookie session is currently authenticated.

        A positive result is cached for LOGIN_CHECK_CACHE_SECONDS; a negative
        result is cached for 15 seconds to avoid spamming the server."""
        now = time.time()
        if use_cache and now < self._logged_in_until:
            return True
        if use_cache and now < getattr(self, "_logged_out_until", 0.0):
            return False
        result = await self._check_html_logged_in()
        if result:
            self._logged_in_until = now + LOGIN_CHECK_CACHE_SECONDS
            self._logged_out_until = 0.0
        else:
            self._logged_in_until = 0.0
            self._logged_out_until = now + 15.0  # 15s negative cache
        return result

    async def _check_html_logged_in(self) -> bool:
        await self.ensure_session()
        try:
            async with self._s_get("https://elearning.uni-oldenburg.de/dispatch.php/start", allow_redirects=True) as r:
                text = await r.text()
                url = str(r.url)

                is_guest = '"username":"nobody"' in text or '"me":{"username":"nobody"}' in text
                is_login_page = "/dispatch.php/login" in url or "index.php?again=yes" in url

                has_logout = "logout" in text.lower()
                has_sidebar = "sidebar-widget" in text
                has_my_courses = "/dispatch.php/my_courses" in text
                has_user_id = "STUDIP.USER_ID" in text

                if (has_logout or has_sidebar or has_my_courses or has_user_id) and not is_guest and not is_login_page:
                    self.logger.debug("is_html_logged_in: True (markers found at %s)", redact_url(url))
                    return True

                self.logger.info("is_html_logged_in: False (markers missing or Guest state at %s)", redact_url(url))
                return False
        except Exception as e:
            self.logger.error("is_html_logged_in check failed: %s", e)
            return False

    async def heal_session(self) -> bool:
        """Attempt self-healing silent SSO renewal using stored NetIQ SSO cookies."""
        if self._lock.locked():
            return await self._heal_session_unlocked()
        async with self._lock:
            return await self._heal_session_unlocked()

    async def _heal_session_unlocked(self) -> bool:
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            return False
        # Throttle healing attempts: don't hammer NetIQ SSO if it failed in the last 60 seconds
        now = time.time()
        if now - getattr(self, "_last_heal_attempt", 0.0) < 60.0:
            return False
        self._last_heal_attempt = now

        has_cookies = False
        if getattr(self, "user_id", None):
            try:
                from unibot import db
                has_cookies = bool(db.load_user_credential(self.user_id, "cookies"))
            except Exception:
                pass
        if not has_cookies and (not self.cookie_file or not os.path.exists(self.cookie_file)):
            return False

        self.logger.info("🔧 [Self-Healing] Attempting silent SSO renewal using warm NetIQ cookies...")
        await self.ensure_session()
        elearn = "https://elearning.uni-oldenburg.de"
        login_link_cancel = f"{elearn}/dispatch.php/login?again=yes&sso=oidc&cancel_login=1"
        try:
            await self._run_sso_flow(login_link_cancel, referer=f"{elearn}/dispatch.php/my_courses")
            if await self.is_html_logged_in(use_cache=False):
                self.explicitly_logged_out = False
                await self.save_cookies()
                self.logger.info("✅ [Self-Healing] Session successfully healed and restored!")
                return True
            else:
                self.logger.warning("⚠️ [Self-Healing] Silent SSO flow finished but login markers missing.")
                return False
        except Exception as exc:
            self.logger.warning("⚠️ [Self-Healing] Silent SSO flow failed: %s", exc)
            return False

    async def is_logged_in(self, allow_oauth: bool = True) -> bool:
        """Check if authenticated (via HTML cookie, automatic self-healing, or OAuth2)."""
        if await self.is_html_logged_in():
            return True

        # Automatic self-healing: if HTML session dropped, silently renew via warm NetIQ cookies
        has_file = bool(self.cookie_file and os.path.exists(self.cookie_file))
        if has_file or getattr(self, "user_id", None):
            if await self.heal_session():
                return True

        use_oauth = studip_oauth.oauth_enabled()
        if allow_oauth and use_oauth:
            try:
                token = await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None))
                if token:
                    return True
            except Exception as exc:
                self.logger.warning("OAuth check failed: %s", exc)
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
            if not has_headless_creds:
                if await self.heal_session():
                    return True
                if not force and use_oauth:
                    try:
                        if await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None)):
                            return True
                    except Exception:
                        logging.debug("OAuth token not available", exc_info=True)
                return False

            if not force and time.time() < self.login_blocked_until:
                self.logger.info(
                    "Skipping automatic login: backing off after %s failure(s) for another %ss", self.login_failures, int(self.login_blocked_until - time.time())
                )
                return False

            self.logger.info("Starting fresh async browser-less login (NetIQ optimized)...")

            elearn = "https://elearning.uni-oldenburg.de"
            start_url = f"{elearn}/dispatch.php/my_courses"
            login_link_cancel = f"{elearn}/dispatch.php/login?again=yes&sso=oidc&cancel_login=1"

            # 1. Prime the session before triggering the login flow.
            try:
                async with self._s_get(start_url):
                    pass
                await self._run_sso_flow(login_link_cancel, referer=start_url)
                self.explicitly_logged_out = False
                self.reset_login_backoff()
                self._logged_in_until = time.time() + LOGIN_CHECK_CACHE_SECONDS
                await self.save_cookies()
                self.logger.info("✅ Login successful.")
                return True
            except Exception as e:
                self.logger.warning("SSO login flow did not complete: %s", e)
                self._record_login_failure()
                if not force and use_oauth:
                    try:
                        if await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None)):
                            self.logger.info("Using active OAuth2 token session.")
                            return True
                    except Exception:
                        logging.debug("OAuth token not available", exc_info=True)
                return False

    async def keep_alive(self) -> None:
        """Keep Stud.IP web session and NetIQ IDP cookies warm; heal session if needed."""
        if self.explicitly_logged_out or (time.time() - self.last_interactive_activity < 180):
            return
        use_oauth = studip_oauth.oauth_enabled()
        has_cookies = False
        if getattr(self, "user_id", None):
            try:
                from unibot import db
                has_cookies = bool(db.load_user_credential(self.user_id, "cookies"))
            except Exception:
                pass
        has_file = bool(self.cookie_file and os.path.exists(self.cookie_file))
        if not has_cookies and not has_file and (not use_oauth or studip_oauth.load_tokens(getattr(self, "user_id", None)) is None):
            return
        await self.ensure_session()
        if use_oauth:
            try:
                await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None))
            except Exception as exc:
                self.logger.debug("OAuth keep-alive skipped: %s", exc)

        for sso_url in (
            "https://login.uni-oldenburg.de/nidp/app",
            "https://auth.uni-oldenburg.de/osp/a/TOP/auth/app/",
            "https://stums.uni-oldenburg.de/qisserver/pages/cs/sys/portal/hisinoneStartPage.faces",
        ):
            try:
                async with self._s_get(sso_url, allow_redirects=True, timeout=15) as _:
                    pass
            except Exception as exc:
                self.logger.debug("SSO keep-alive skipped for %s: %s", redact_url(sso_url), exc)

        try:
            # keep-alive must really touch Stud.IP, so never answer from cache
            if not await self.is_html_logged_in(use_cache=False):
                # Silently heal session using warm NetIQ cookies
                await self.heal_session()
            else:
                await self.save_cookies()
        except Exception as exc:
            self.logger.debug("Web session keep-alive renewal skipped: %s", exc)

    async def logout(self) -> None:
        """Terminate Stud.IP + NetIQ SSO sessions, clear cookies and OAuth2 tokens."""
        async with self._lock:
            self.explicitly_logged_out = True
            self.invalidate_login_cache()
            self.reset_login_backoff()
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
                        async with self._s_get(url, allow_redirects=True, timeout=10) as _:
                            pass
                    except Exception as exc:
                        self.logger.debug("Upstream logout request skipped for %s: %s", redact_url(url), exc)
                self.session.cookie_jar.clear()

            if getattr(self, "user_id", None):
                try:
                    from unibot import db
                    db.delete_user_credentials(self.user_id)
                except Exception as exc:
                    self.logger.warning("Could not delete credentials for user %s: %s", self.user_id, exc)

            if self.cookie_file:
                for path in (self.cookie_file, f"{self.cookie_file}.tmp"):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError as exc:
                        self.logger.warning("Could not remove %s: %s", path, exc)

            studip_oauth.clear_tokens(getattr(self, "user_id", None))
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
            u = urlparse(url)
            if u.netloc.lower() == success_host.lower():
                if "/qisserver/" in u.path:
                    return "/callback/" not in u.path and "error" not in u.path.lower()
                return True
            return False

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
                    self.logger.warning("[%s] Max redirect depth reached at %s", tag, redact_url(url))
                    return url, text
                
                soup = BeautifulSoup(text, "html.parser")

                # 1. JS Redirect
                js_redirect = re.search(r"window\.location\.(?:href|assign|replace)\s*=\s*['\"]([^'\"]+)['\"]", text)
                if js_redirect:
                    nxt = urljoin(url, js_redirect.group(1))
                    self.logger.info("[%s] Following JS redirect: %s", tag, redact_url(nxt))
                    async with self._s_get(nxt, headers={"Referer": url}) as r:
                         return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                # 2. Meta Refresh
                meta = soup.find("meta", attrs={"http-equiv": lambda x: x and x.lower() == "refresh"})
                if meta:
                    content = meta.get("content", "")
                    if "url=" in content.lower():
                        nxt = urljoin(url, content.lower().split("url=")[1].strip().strip("'\""))
                        self.logger.info("[%s] Following Meta Refresh: %s", tag, redact_url(nxt))
                        async with self._s_get(nxt, headers={"Referer": url}) as r:
                            return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                
                # 3. AJAX Content Load (NetIQ specific)
                ajax_load = re.search(r"getToContent\(['\"]([^'\"]+)['\"]", text)
                if ajax_load:
                    ajax_url = urljoin(url, ajax_load.group(1))
                    self.logger.info("[%s] Following NetIQ AJAX content load: %s", tag, redact_url(ajax_url))
                    async with self._s_get(ajax_url, headers={"Referer": url}) as r:
                         return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                
                # 4. Script-based form auto-submit (e.g., NetIQ authCodeForm)
                form_submit = re.search(r"document\.(?:forms\[['\"]?([^'\"\]]+)['\"]?\]|([a-zA-Z0-9_]+))\s*\.\s*submit\(\)", text)
                if form_submit:
                    form_name = form_submit.group(1) or form_submit.group(2)
                    form = soup.find("form", attrs={"name": form_name}) or soup.find("form", id=form_name)
                    if form:
                        inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                        action = urljoin(url, form.get("action") or "")
                        method = (form.get("method") or "POST").upper()
                        self.logger.info("[%s] Script-submitting form '%s' to %s", tag, form_name, redact_url(action))
                        if method == "POST":
                            async with self._s_post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                        else:
                            async with self._s_get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                # 5. OAuth/SAML Auth Handoff Forms (Auto-submit via presence of sensitive keys)
                for form in soup.find_all("form"):
                    inputs = {i.get("name"): (i.get("value") or "") for i in form.find_all("input") if i.get("name")}
                    keys = set(k.lower() for k in inputs.keys())
                    if keys & {"code", "state", "id_token", "samlresponse", "relaystate", "wa", "wresult"}:
                        action = urljoin(url, form.get("action") or "")
                        method = (form.get("method") or "POST").upper()
                        self.logger.info("[%s] Auto-submitting auth handoff form to %s", tag, redact_url(action))
                        if method == "POST":
                            async with self._s_post(action, data=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)
                        else:
                            async with self._s_get(action, params=inputs, headers={"Referer": url}) as r:
                                return await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), tag, depth + 1)

                self.logger.debug("[%s] No more redirects detected at %s", tag, redact_url(url))
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
            return await self.is_html_logged_in(use_cache=False)

        # 1. Entry
        async with self._s_get(entry_url, headers={"Referer": referer} if referer else None) as r:
            curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "entry")

        # 2. Main state-machine loop
        prev_text = ""
        for i in range(15):
            if curr_text == prev_text:
                self.logger.warning("Page content stayed identical. Breaking loop.")
                break
            prev_text = curr_text

            soup = BeautifulSoup(curr_text, "html.parser")
            page_text = curr_text.lower()

            self.logger.info("Step %s: URL %s", i, redact_url(curr_url))

            if await check_done(curr_url, curr_text):
                return curr_url, curr_text

            # Identification Logic

            # A. Legacy Login Page (Ecom fields)
            if "ecom_user_id" in page_text and "ecom_password" not in page_text:
                if not self.username:
                    raise RuntimeError("Interactive login required (no stored username)")
                self.logger.info("Step %s: Username Submission", i+1)
                action, method, payload = get_payload(soup, {"Ecom_User_ID": self.username, "loginButton2": "true"})
                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "user")
                continue

            if "ecom_password" in page_text:
                if not self.password:
                    raise RuntimeError("Interactive login required (no stored password)")
                self.logger.info("Step %s: Password Submission (Legacy)", i+1)
                action, method, payload = get_payload(soup, {"Ecom_Password": self.password, "loginButton2": "true"})
                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "pass")
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
                    self.logger.info("Step %s: TOTP Submission (Contract)", i+1)
                    action, method, payload = get_payload(soup, {"nffc": token, "loginButton2": "Next"})
                else:
                    if not self.password:
                        raise RuntimeError("Interactive login required (no stored password)")
                    self.logger.info("Step %s: Password Submission (Contract)", i+1)
                    action, method, payload = get_payload(soup, {"nffc": self.password, "loginButton2": "Next"})

                action = urljoin(curr_url, action)
                async with self._s_post(action, data=payload, headers={"Referer": curr_url}) as r:
                    curr_url, curr_text = await follow_redirects(unroute_proxy_url(str(r.url)), await r.text(), "contract")
                continue

            # C. Unhandled
            self.logger.warning("Step %s: Unhandled state. URL: %s", i, redact_url(curr_url))
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
            token = await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None))
        except Exception as exc:  # never let a token problem break a request
            self.logger.warning("OAuth token unavailable: %s", exc)
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _merged_headers(self, url: str, extra: dict | None) -> dict | None:
        headers = dict(extra or {})
        headers.update(await self._bearer_headers(url))
        return headers or None

    async def get(self, url, **kwargs):
        await self.ensure_session()
        merged = await self._merged_headers(url, kwargs.pop("headers", None))
        cookie_jar = self.session.cookie_jar if self.session else None
        routed_url, headers = route_proxy_url(url, merged, cookie_jar=cookie_jar)
        if headers:
            kwargs["headers"] = headers
        resp = await self.session.get(routed_url, **kwargs)
        target_host = (urlsplit(url).hostname or "").lower()
        self._capture_response_cookies(resp, target_host)
        return resp

    async def post(self, url, **kwargs):
        await self.ensure_session()
        merged = await self._merged_headers(url, kwargs.pop("headers", None))
        cookie_jar = self.session.cookie_jar if self.session else None
        routed_url, headers = route_proxy_url(url, merged, cookie_jar=cookie_jar)
        if headers:
            kwargs["headers"] = headers
        resp = await self.session.post(routed_url, **kwargs)
        target_host = (urlsplit(url).hostname or "").lower()
        self._capture_response_cookies(resp, target_host)
        return resp

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.logger.info("Session closed.")
