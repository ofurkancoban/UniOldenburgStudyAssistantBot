import aiohttp
import asyncio
from dataclasses import dataclass, field
from typing import Optional
import logging
import os
import hmac
import hashlib
import time
import struct
import base64
import json
import random
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urljoin, urlsplit
from yarl import URL

import studip_oauth
from studip_http import (
    DEFAULT_TIMEOUT,
    THROTTLE,
    UNI_HOSTS,
    UniResponse,
    parse_retry_after,
    rewrite_headers,
    to_timeout,
    user_agent,
)
from studip_sso import SsoFlowMixin, _describe_page  # noqa: F401 - _describe_page re-exported
from studip_utils import redact_url, route_proxy_url, unroute_proxy_url

_REDIRECT_CODES = {301, 302, 303, 307, 308}




# Hosts the deployed worker refused (an older worker.js without that host in
# its allow-list, or a wrong secret): sent directly until the time stored here.
_WORKER_REFUSED: dict[str, float] = {}
WORKER_REFUSAL_RETRY_SECONDS = 3600


async def _worker_refusal(resp) -> str:
    """Why the worker itself (not the university) refused a request, or ""."""
    if resp.status not in (401, 403):
        return ""
    if resp.headers.get("X-Proxy-Error"):
        return resp.headers["X-Proxy-Error"]
    if "text/plain" not in resp.headers.get("Content-Type", "text/plain"):
        return ""
    try:
        body = (await resp.text())[:120]
    except Exception:
        return ""
    if body.startswith("Forbidden: host") or body.strip() == "Unauthorized":
        return body.strip()
    return ""


# Backup path: while the worker itself is down (unreachable, timing out, or
# answering with a Cloudflare platform error such as the daily request limit),
# requests go directly to the university and the worker is retried later.
# The pause doubles with every failure in a row, up to WORKER_DOWN_MAX_SECONDS.
WORKER_DOWN_MIN_SECONDS = 120
WORKER_DOWN_MAX_SECONDS = 1800
_WORKER_HEALTH = {"down_until": 0.0, "failures": 0, "failovers": 0}


def _worker_failure(resp) -> str:
    """Why a worker response is the worker's own failure (not the university's), or ""."""
    error = resp.headers.get("X-Proxy-Error", "")
    if error == "upstream":
        return "the worker could not reach the university"
    if error or resp.headers.get("X-Proxy-Upstream"):
        return ""
    if resp.status == 429 or resp.status >= 500:
        return f"Cloudflare answered HTTP {resp.status}"
    return ""


def worker_status() -> dict:
    """The worker's routing state for /healthcheck."""
    url = _worker_url()
    now = time.time()
    return {
        "configured": bool(url),
        "down_for": max(0.0, _WORKER_HEALTH["down_until"] - now),
        "failovers": _WORKER_HEALTH.get("failovers", 0),
        "refused_hosts": sorted(h for h, until in _WORKER_REFUSED.items() if until > now),
    }


def _worker_available() -> bool:
    return time.time() >= _WORKER_HEALTH["down_until"]


def _mark_worker_down(logger, reason: str) -> None:
    failures = _WORKER_HEALTH["failures"] + 1
    pause = min(WORKER_DOWN_MAX_SECONDS, WORKER_DOWN_MIN_SECONDS * 2 ** (failures - 1))
    _WORKER_HEALTH.update(failures=failures, down_until=time.time() + pause,
                          failovers=_WORKER_HEALTH.get("failovers", 0) + 1)
    logger.warning("Cloudflare Worker unavailable (%s); sending requests directly for %s min.",
                   reason, max(1, pause // 60))


def _mark_worker_up(logger) -> None:
    if _WORKER_HEALTH["failures"]:
        logger.info("Cloudflare Worker reachable again; routing requests through it.")
    _WORKER_HEALTH.update(failures=0, down_until=0.0)  # "failovers" keeps counting


def _worker_url() -> str:
    return os.getenv("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")

# Wait this long after the 1st, 2nd, 3rd, 4th+ failed password/TOTP login in a
# row before the next automatic attempt. Without it every button tap and
# watcher cycle re-sent the password, which can get the account locked by
# NetIQ after a password change.
LOGIN_BACKOFF_SECONDS = (60, 300, 900, 3600)
# A positive HTML login check is trusted for this long, so handlers don't
# download a whole Stud.IP page before doing any actual work.
LOGIN_CHECK_CACHE_SECONDS = 60


@dataclass
class StumsState:
    """One account's StuMS sign-in, kept on its StudIPSession.

    verified_at   when StuMS last confirmed this session as signed in (0: never)
    referer       a StuMS page to send as Referer (the JSF app needs one)
    blocked_until background StuMS checks pause until then after a refused
                  login; an interactive sign-in clears it
    """
    verified_at: float = 0.0
    referer: Optional[str] = None
    blocked_until: float = 0.0
    _lock: Optional[asyncio.Lock] = field(default=None, repr=False)
    _lock_loop: object = field(default=None, repr=False)

    def lock(self) -> asyncio.Lock:
        """One StuMS sign-in at a time for this account (bound to the running loop)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock


class StudIPSession(SsoFlowMixin):
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
        # Cookie-less twin of `session` for requests sent through the
        # Cloudflare Worker: their cookies are managed by hand (see request()).
        self._proxy_http = None
        self.headers = {
            'User-Agent': user_agent(),
            'Accept-Encoding': 'gzip, deflate',
        }
        self.logger = logging.getLogger("StudIPSession")
        self._lock = asyncio.Lock()
        self.last_interactive_activity = 0.0
        self.explicitly_logged_out = False
        self.login_failures = 0
        self.login_blocked_until = 0.0
        self._logged_in_until = 0.0
        # This account's StuMS sign-in (verification, Referer, background pause)
        self.stums = StumsState()
        # the Stud.IP session cookie seen at the last login, to spot a new login
        self.last_seen_studip_cookie: Optional[str] = None

    def reset_login_backoff(self) -> None:
        """Forget earlier failed logins (after a successful interactive login)."""
        self.login_failures = 0
        self.login_blocked_until = 0.0
        self.stums.blocked_until = 0.0

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
        await self.close()

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                connector=aiohttp.TCPConnector(limit=20, limit_per_host=6, ttl_dns_cache=300),
                timeout=DEFAULT_TIMEOUT,
            )
            await self.load_cookies()
        if self._proxy_http is None or self._proxy_http.closed:
            self._proxy_http = aiohttp.ClientSession(
                headers=self.headers,
                cookie_jar=aiohttp.DummyCookieJar(),
                connector=aiohttp.TCPConnector(limit=20, limit_per_host=8, ttl_dns_cache=300),
                timeout=DEFAULT_TIMEOUT,
            )

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

    # ── HTTP transport ────────────────────────────────────────────────────
    # Every request of this session goes through `request()`: redirects are
    # followed hop by hop, so each hop is throttled, carries exactly the
    # cookies the browser would send to that host and path, and stores the
    # cookies it receives under the real university host. With the optional
    # Cloudflare Worker this keeps one cookie jar per real host instead of
    # mixing every host's JSESSIONID under the worker's hostname.

    def _via_worker(self, host: str) -> bool:
        if not _worker_url() or host not in UNI_HOSTS or not _worker_available():
            return False
        return time.time() >= _WORKER_REFUSED.get(host, 0.0)

    def _store_proxied_cookies(self, resp, real_url: str) -> None:
        """Store cookies a Worker-proxied response carried, under the real host."""
        raw = resp.headers.getall("X-Upstream-Set-Cookie", [])
        if not raw:
            # Older worker deployments pass a rewritten Set-Cookie (Domain removed)
            raw = resp.headers.getall("Set-Cookie", [])
        for header in raw:
            jar_entry = SimpleCookie()
            try:
                jar_entry.load(header)
            except CookieError:
                self.logger.debug("Skipping unparsable cookie from %s", redact_url(real_url))
                continue
            if jar_entry:
                self.session.cookie_jar.update_cookies(jar_entry, response_url=URL(real_url))

    async def _send_once(self, method: str, url: str, headers: dict, body: dict, timeout, extra: dict):
        host = (urlsplit(url).hostname or "").lower()
        hdrs = dict(headers)
        if self._via_worker(host):
            # The worker's hostname differs from the target, so aiohttp would
            # not attach the target's cookies itself: send them explicitly,
            # matched against the full URL (path-scoped cookies included).
            cookies = self.session.cookie_jar.filter_cookies(URL(url))
            if cookies:
                injected = "; ".join(f"{k}={m.value}" for k, m in cookies.items())
                existing = hdrs.pop("Cookie", None) or hdrs.pop("cookie", None)
                hdrs["Cookie"] = f"{existing}; {injected}" if existing else injected
            routed, hdrs = route_proxy_url(url, hdrs)
            http = self._proxy_http
        else:
            routed, http = url, self.session
        via_worker = http is self._proxy_http
        async with THROTTLE.slot(host):
            try:
                resp = await http.request(
                    method, routed, headers=hdrs or None, allow_redirects=False,
                    timeout=timeout or DEFAULT_TIMEOUT, **body, **extra,
                )
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if not via_worker:
                    THROTTLE.report(host, failed=True)
                    raise
                _mark_worker_down(self.logger, exc.__class__.__name__)
                # A request that never reached the worker is safe to resend;
                # after a timeout a POST may already have been delivered.
                if method in ("GET", "HEAD") or isinstance(exc, aiohttp.ClientConnectorError):
                    return await self._send_once(method, url, headers, body, timeout, extra)
                raise
        if via_worker:
            failure = _worker_failure(resp)
            if failure:
                resp.release()
                _mark_worker_down(self.logger, failure)
                if method in ("GET", "HEAD"):
                    return await self._send_once(method, url, headers, body, timeout, extra)
                raise aiohttp.ClientConnectionError(f"Cloudflare Worker failed: {failure}")
            _mark_worker_up(self.logger)
        if via_worker:
            reason = await _worker_refusal(resp)
            if reason:
                resp.release()
                _WORKER_REFUSED[host] = time.time() + WORKER_REFUSAL_RETRY_SECONDS
                self.logger.warning(
                    "Cloudflare Worker refused %s (%s); sending it directly for %s min. "
                    "Redeploy cloudflare_worker/worker.js to route it through the worker again.",
                    host, reason[:80], WORKER_REFUSAL_RETRY_SECONDS // 60,
                )
                return await self._send_once(method, url, headers, body, timeout, extra)
        THROTTLE.report(host, resp.status, parse_retry_after(resp.headers.get("Retry-After")))
        if via_worker:
            self._store_proxied_cookies(resp, url)
        return resp

    async def request(self, method: str, url, *, allow_redirects: bool = True, max_redirects: int = 20,
                      headers: dict | None = None, params=None, data=None, json=None, timeout=None,
                      **extra) -> UniResponse:
        """Send a request (following redirects manually) and return a UniResponse."""
        await self.ensure_session()
        method = method.upper()
        url = unroute_proxy_url(str(url))
        if params:
            url = str(URL(url).extend_query(params))
        req_headers = dict(headers or {})
        req_headers.update(await self._bearer_headers(url))
        body = {k: v for k, v in (("data", data), ("json", json)) if v is not None}
        timeout = to_timeout(timeout)
        history: list[str] = []
        current = url
        for _hop in range(max_redirects + 1):
            resp = await self._send_with_retry(method, current, req_headers, body, timeout, extra)
            location = resp.headers.get("Location")
            real_location = unroute_proxy_url(urljoin(current, location)) if location else None
            if not allow_redirects or resp.status not in _REDIRECT_CODES or not real_location:
                headers_out = None
                if location or self._via_worker((urlsplit(current).hostname or "").lower()):
                    headers_out = rewrite_headers(resp.headers, real_location)
                return UniResponse(resp, current, headers=headers_out, history=history)
            resp.release()
            history.append(current)
            if resp.status == 303 or (resp.status in (301, 302) and method == "POST"):
                if method != "HEAD":
                    method = "GET"
                body = {}
                for key in [k for k in req_headers if k.lower() in ("content-type", "content-length")]:
                    req_headers.pop(key, None)
            if urlsplit(real_location).hostname != urlsplit(current).hostname:
                for key in [k for k in req_headers if k.lower() == "authorization"]:
                    req_headers.pop(key, None)
            current = real_location
        raise RuntimeError(f"Too many redirects for {redact_url(url)}")

    async def _send_with_retry(self, method, url, headers, body, timeout, extra):
        """Idempotent requests get two quiet retries on network errors or 502/504."""
        attempts = 3 if method in ("GET", "HEAD") else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._send_once(method, url, headers, body, timeout, extra)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if attempt == attempts:
                    raise
                self.logger.debug("Retrying %s after %s", redact_url(url), exc.__class__.__name__)
            else:
                if resp.status not in (502, 504) or attempt == attempts:
                    return resp
                resp.release()
            await asyncio.sleep(1.5 * attempt + random.uniform(0, 1.0))
        raise RuntimeError("unreachable")

    class _RequestContext:
        """`async with session._s_get(url) as r:` support."""

        def __init__(self, session_obj, method: str, url, kwargs):
            self._args = (session_obj, method, url, kwargs)
            self._resp = None

        async def __aenter__(self):
            session_obj, method, url, kwargs = self._args
            self._resp = await session_obj.request(method, url, **kwargs)
            return self._resp

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if self._resp is not None:
                self._resp.release()

    def _s_get(self, url, **kwargs):
        return self._RequestContext(self, "GET", url, kwargs)

    def _s_post(self, url, **kwargs):
        return self._RequestContext(self, "POST", url, kwargs)

    async def save_cookies(self):
        if not self.session:
            return
        jar = self.session.cookie_jar
        host_only = getattr(jar, "_host_only_cookies", None)
        worker_host = (urlsplit(_worker_url()).hostname or "").lower() if _worker_url() else ""

        entries = []
        seen = set()
        for cookie in jar:
            dom = (cookie["domain"] or "elearning.uni-oldenburg.de").lower()
            if worker_host and dom.lstrip(".") == worker_host:
                continue  # never persist cookies scoped to the proxy itself
            item_key = (cookie.key, dom, cookie["path"] or "/")
            if item_key in seen:
                continue
            seen.add(item_key)
            entries.append({
                "key": cookie.key,
                "value": cookie.value,
                "domain": dom,
                "path": cookie["path"] or "/",
                "host_only": (dom, cookie.key) in host_only if host_only is not None else True,
            })
        if getattr(self, "user_id", None):
            # A user's login lives only in the encrypted credentials table; a
            # cookie file from before is removed once the database has it.
            try:
                from unibot import db
                db.save_user_credential(self.user_id, "cookies", entries)
                self.logger.debug("Encrypted cookies saved to DB for user %s", self.user_id)
            except Exception as exc:
                self.logger.error("Failed to save cookies to DB for user %s: %s", self.user_id, exc)
                return
            self._remove_cookie_files()
            return

        if self.cookie_file:
            try:
                from studip_vault import write_vault_file
                write_vault_file(self.cookie_file, entries, mode=0o600)
                self.logger.debug("Encrypted cookies saved to %s (mode 0600)", self.cookie_file)
            except Exception as exc:
                self.logger.error("Failed to save encrypted cookies to %s: %s", self.cookie_file, exc)

    def _remove_cookie_files(self) -> None:
        paths = {self.cookie_file, f"studip_cookies_{self.user_id}.json"} - {None, "", "studip_cookies.json"}
        for path in paths:
            for candidate in (path, f"{path}.tmp"):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                        self.logger.info("Removed %s: the login now lives in the database", candidate)
                except OSError:
                    self.logger.debug("Could not remove %s", candidate, exc_info=True)

    def _restore_cookie(self, key: str, val: str, dom: str, path: str, host_only: bool) -> None:
        morsel_holder = SimpleCookie()
        morsel_holder[key] = val
        morsel = morsel_holder[key]
        morsel["path"] = path or "/"
        if not host_only:
            morsel["domain"] = dom
        self.session.cookie_jar.update_cookies(morsel_holder, response_url=URL(f"https://{dom}/"))

    async def load_cookies(self):
        data = None
        migrate_to_db = False
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
                    migrate_to_db = bool(getattr(self, "user_id", None))
            except Exception as e:
                self.logger.error("Failed to load cookies: %s", e)

        if self.session and data:
            worker_host = (urlsplit(_worker_url()).hostname or "").lower() if _worker_url() else ""
            try:
                if isinstance(data, list):
                    for entry in data:
                        key = entry.get("key")
                        val = entry.get("value")
                        dom = (entry.get("domain") or "elearning.uni-oldenburg.de").lstrip(".").lower()
                        if key is None or val is None or (worker_host and dom == worker_host):
                            continue
                        host_only = entry.get("host_only")
                        if host_only is None:
                            # older files: a bare parent domain can only be a domain cookie
                            host_only = dom.count(".") > 1
                        try:
                            self._restore_cookie(key, val, dom, entry.get("path") or "/", bool(host_only))
                        except (CookieError, ValueError):
                            self.logger.debug("Skipping unrestorable cookie %s", key)
                elif isinstance(data, dict):
                    for key, val in data.items():
                        if key in ("JSESSIONID", "UrnNovellNidpClusterMemberId", "_PA_SDK_SSO_") or key.startswith("x-oidp-"):
                            dom = "idp.uni-oldenburg.de"
                        else:
                            dom = "elearning.uni-oldenburg.de"
                        try:
                            self._restore_cookie(key, val, dom, "/", True)
                        except (CookieError, ValueError):
                            self.logger.debug("Skipping unrestorable cookie %s", key)
                if migrate_to_db:
                    await self.save_cookies()  # into the database, then the file goes
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
                logging.debug("Ignored a non-critical error", exc_info=True)
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
            except Exception as exc:  # priming is best-effort; the SSO flow decides
                self.logger.debug("Login priming request failed: %s", exc)
            try:
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
                logging.debug("Ignored a non-critical error", exc_info=True)
        has_file = bool(self.cookie_file and os.path.exists(self.cookie_file))
        if not has_cookies and not has_file and (not use_oauth or studip_oauth.load_tokens(getattr(self, "user_id", None)) is None):
            return
        await self.ensure_session()
        if use_oauth:
            try:
                await studip_oauth.get_access_token(user_id=getattr(self, "user_id", None))
            except Exception as exc:
                self.logger.debug("OAuth keep-alive skipped: %s", exc)

        keepalive_urls = [
            "https://login.uni-oldenburg.de/nidp/app",
            "https://auth.uni-oldenburg.de/osp/a/TOP/auth/app/",
        ]
        stums_start = "https://stums.uni-oldenburg.de/qisserver/pages/cs/sys/portal/hisinoneStartPage.faces"
        # Touch StuMS only when this user has a StuMS session to keep warm;
        # without one the request would just open an anonymous session.
        if self.session.cookie_jar.filter_cookies(URL(stums_start)):
            keepalive_urls.append(stums_start)
        for sso_url in keepalive_urls:
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
            self.stums = StumsState()
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

    async def get(self, url, **kwargs) -> UniResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs) -> UniResponse:
        return await self.request("POST", url, **kwargs)

    async def close(self):
        closed = False
        for http in (self.session, getattr(self, "_proxy_http", None)):
            if http is not None and not http.closed:
                await http.close()
                closed = True
        if closed:
            self.logger.info("Session closed.")
