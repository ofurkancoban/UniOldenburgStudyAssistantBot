"""Live Stud.IP + Uni Oldenburg SSO Reverse Proxy for Telegram Mini App (/login).

Instead of a fake form, this serves the real Uni Oldenburg Central Authentication
(login.uni-oldenburg.de / auth.uni-oldenburg.de) and Stud.IP
(elearning.uni-oldenburg.de) pages inside the Telegram Mini App (or via a
single-use /login/<token> link), proxied directly through the bot's own
`StudIPSession` cookie jar.

Why this works without ever needing to log in again:
  1. Every HTTP request in the Mini App goes through `global_session.session`
     on the bot, so all cookies (`Studip_Session`, `JSESSIONID`, `_PA_SDK_SSO_`,
     `x-oidp-session*`) land directly in the bot's cookie jar.
  2. The moment the SSO redirect finishes and reaches Stud.IP, the bot saves
     the cookies to `studip_cookies.json` and runs the OAuth2 PKCE consent flow
     in the background to mint `studip_oauth_tokens.json` (access_token +
     refresh_token).
  3. Access to the proxy is strictly gated by Telegram Mini App `initData` HMAC
     verification (checked against `ALLOWED_USER_IDS`) or a single-use `/login`
     token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from studip_http import interactive_requests, user_agent
from studip_utils import redact_url

logger = logging.getLogger("StudIPLoginServer")

TOKEN_TTL = 600          # seconds a link token stays valid
INITDATA_MAX_AGE = 3600  # seconds a Mini App payload stays valid
PROXY_COOKIE = "_bot_proxy_auth"
PROXY_SESSION_TTL = 3600

ELEARN_ORIGIN = "https://elearning.uni-oldenburg.de"
LOGIN_ORIGIN = "https://login.uni-oldenburg.de"
AUTH_ORIGIN = "https://auth.uni-oldenburg.de"
STUMS_ORIGIN = "https://stums.uni-oldenburg.de"
# StuMS' own "log in via university SSO" entry; through this proxy it runs the
# NetIQ login in the bot's cookie jar and lands back on StuMS signed in.
STUMS_SSO_ENTRY_PATH = "/qisserver/api/identityprovider/redirect/2/login"
LoginCallback = Callable[[str, str, str], Awaitable[tuple[bool, str]]]

# Rewrite leading scheme+host in HTML attributes (action, href, src) without
# touching query-string parameters like `redirect_uri=https://...`.
_ATTR_ORIGIN_RE = re.compile(
    r'(\b(?:action|href|src)\s*=\s*["\'])https://(?:elearning|login|idp|auth|stums)\.uni-oldenburg\.de(?::443)?/',
    re.IGNORECASE,
)

# Rewrite JS assignments like `window.location.href = "https://..."`
_JS_ORIGIN_RE = re.compile(
    r'((?:location(?:\.href)?|redirectUri)\s*=\s*["\'])https://(?:elearning|login|idp|auth|stums)\.uni-oldenburg\.de(?::443)?/',
    re.IGNORECASE,
)

_LOCATION_ORIGIN_RE = re.compile(
    r"^https://(?:elearning|login|idp|auth|stums)\.uni-oldenburg\.de(?::443)?(/.*)?$",
    re.IGNORECASE,
)

_HOP_BY_HOP_REQ = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "accept-encoding",
    # added by the Cloudflare tunnel / reverse proxy in front of this server;
    # the university must neither see the student's IP nor proxy metadata
    "forwarded", "x-real-ip", "true-client-ip", "cdn-loop",
}
_STRIP_REQ_PREFIXES = ("x-forwarded-", "cf-", "x-telegram-")

_STRIP_RESP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "set-cookie", "x-upstream-set-cookie", "content-security-policy",
    "content-security-policy-report-only", "x-frame-options",
    "strict-transport-security",
}


def _lan_ip() -> str:
    """Best-effort LAN address of this machine."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.0.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _upstream_for_path(path: str) -> str:
    if path.startswith("/nidp/") or path == "/nidp":
        return LOGIN_ORIGIN
    if path.startswith("/osp/") or path == "/osp":
        return AUTH_ORIGIN
    if path.startswith("/qisserver/") or path == "/qisserver":
        return STUMS_ORIGIN
    return ELEARN_ORIGIN


def verify_init_data(init_data: str, bot_token: str,
                     max_age: int = INITDATA_MAX_AGE) -> Optional[dict]:
    """Validate Telegram Mini App initData and return its `user` dict."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    except ValueError:
        return None

    received = pairs.pop("hash", None)
    if not received:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        logger.warning("initData signature mismatch")
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and time.time() - auth_date > max_age:
        logger.warning("initData is stale (%ss old)", int(time.time() - auth_date))
        return None

    try:
        return json.loads(pairs.get("user", "{}")) or None
    except ValueError:
        return None


_GATE_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Stud.IP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
 body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
        background:#003f73; color:#fff; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:20px; text-align:center; }
 .box { max-width:340px; }
 .spinner { width:36px; height:36px; border:3px solid rgba(255,255,255,.3); border-top-color:#fff;
            border-radius:50%; margin:0 auto 14px; animation:spin .8s linear infinite; }
 @keyframes spin { to { transform:rotate(360deg); } }
 .err { background:#3a1d20; color:#ff9aa2; padding:12px 14px; border-radius:10px; font-size:13px; }
</style></head><body>
<div class="box" id="box">
  <div class="spinner"></div>
  <div>Loading Stud.IP…</div>
</div>
<script>
(async function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  const initData = tg ? tg.initData : '';
  const box = document.getElementById('box');
  if (!initData) {
    box.innerHTML = '<div class="err">Please open this page directly from the button in Telegram (/login).</div>';
    return;
  }
  try {
    const res = await fetch('/_bot/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
      credentials: 'same-origin'
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      box.innerHTML = '<div class="err">' + (data.error || 'Authentication failed.') + '</div>';
      return;
    }
    window.location.replace((data.redirect || '/_bot/entry') + window.location.search);
  } catch (e) {
    box.innerHTML = '<div class="err">Network error. Please try again.</div>';
  }
})();
</script>
</body></html>"""

_STATUS_BAR_SNIPPET = """
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<div id="_bot_bar" style="position:fixed;bottom:max(14px,env(safe-area-inset-bottom,14px));right:14px;z-index:999999;display:flex;gap:10px;align-items:center;background:rgba(15,23,42,0.88);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);color:#f8fafc;padding:7px 12px;border-radius:999px;box-shadow:0 8px 24px rgba(0,0,0,0.35);border:1px solid rgba(255,255,255,0.12);font:12px/1.2 -apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;">
  <span style="font-weight:500;display:flex;align-items:center;gap:6px;">__STATUS_TEXT__</span>
  <a href="/_bot/entry?relogin=1" onclick="try{sessionStorage.removeItem('_bot_close_hint_dismissed');}catch(e){}" style="color:#93c5fd;text-decoration:none;font-weight:600;padding:2px 6px;border-radius:6px;background:rgba(255,255,255,0.06);">Relink</a>
  <button type="button" onclick="if(window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData){Telegram.WebApp.close();}else{window.close();}" style="background:#2563eb;color:#fff;border:0;border-radius:999px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;box-shadow:0 2px 6px rgba(37,99,235,0.35);">Done</button>
</div>
"""

_CONNECTED_BANNER_SNIPPET = """
<style>
@keyframes _botModalFadeIn {
  0% { opacity: 0; }
  100% { opacity: 1; }
}
@keyframes _botModalPop {
  0% { transform: scale(0.92) translateY(16px); opacity: 0; }
  100% { transform: scale(1) translateY(0); opacity: 1; }
}
@keyframes _botPulseGlow {
  0%, 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.45); }
  50% { transform: scale(1.05); box-shadow: 0 0 0 12px rgba(16, 185, 129, 0); }
}
</style>
<div id="_bot_modal_backdrop" onclick="_botCloseApp()" style="display:flex;position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483647;background:rgba(15,23,42,0.65);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);align-items:center;justify-content:center;padding:20px;box-sizing:border-box;animation:_botModalFadeIn 0.25s ease-out forwards;touch-action:none;user-select:none;cursor:pointer;">
  <div id="_bot_modal_card" onclick="event.stopPropagation();" style="width:100%;max-width:320px;background:rgba(30,41,59,0.95);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-radius:24px;padding:28px 22px;box-shadow:0 24px 48px -12px rgba(0,0,0,0.65), 0 0 0 1px rgba(255,255,255,0.12);text-align:center;box-sizing:border-box;color:#f8fafc;animation:_botModalPop 0.3s cubic-bezier(0.16,1,0.3,1) forwards;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',sans-serif;cursor:default;">
    <div style="width:56px;height:56px;border-radius:18px;background:linear-gradient(135deg,#10b981,#059669);display:flex;align-items:center;justify-content:center;margin:0 auto 16px auto;animation:_botPulseGlow 2.5s infinite;box-shadow:0 8px 20px -4px rgba(16,185,129,0.5);">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
    </div>
    <div style="font-size:18px;font-weight:700;color:#ffffff;letter-spacing:-0.2px;margin-bottom:6px;">Login Successful</div>
    <div style="font-size:13px;line-height:1.45;color:#94a3b8;margin-bottom:22px;">Signed in to Stud.IP.<br>Session is active and connected to bot.</div>
    <button type="button" onclick="_botCloseApp()" style="width:100%;padding:13px 18px;border-radius:14px;background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#ffffff;font-size:15px;font-weight:600;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(37,99,235,0.4);display:flex;align-items:center;justify-content:center;gap:8px;-webkit-tap-highlight-color:transparent;">
      <span>Done (Return to Bot)</span>
    </button>
  </div>
</div>
<script>
// Close the Mini App. telegram-web-app.js may still be loading, so retry
// briefly before falling back to window.close().
function _botCloseApp(tries) {
  tries = typeof tries === 'number' ? tries : 0;
  try {
    if (window.Telegram && Telegram.WebApp) { Telegram.WebApp.close(); return; }
  } catch(e){}
  if (tries < 15) { setTimeout(function(){ _botCloseApp(tries + 1); }, 100); return; }
  try { window.close(); } catch(e){}
}
(function(){
  // close automatically one second after the sign-in
  setTimeout(function(){ _botCloseApp(0); }, 1000);
  try {
    var b = document.getElementById('_bot_bar');
    if (b) b.style.display = 'none';
    document.body.style.overflow = 'hidden';
    document.documentElement.style.overflow = 'hidden';
    if (window.Telegram && Telegram.WebApp) {
      Telegram.WebApp.ready();
      Telegram.WebApp.expand();
      if (Telegram.WebApp.MainButton) {
        Telegram.WebApp.MainButton.setText("Done (Return to Bot)").show().onClick(_botCloseApp);
      }
      if (Telegram.WebApp.HapticFeedback && !sessionStorage.getItem('_bot_haptic_done')) {
        sessionStorage.setItem('_bot_haptic_done', '1');
        Telegram.WebApp.HapticFeedback.notificationOccurred('success');
      }
    }
  } catch(e){}
})();
</script>
"""


class _RedactedAccessLogger(AbstractAccessLogger):
    """Access log line without the query string, Referer or User-Agent.

    aiohttp's default format logs the full request line, and the SSO redirects
    through this proxy carry one-time OAuth codes and state values in the
    query string (and in the Referer of the next request)."""

    def log(self, request, response, time):
        self.logger.info('%s "%s %s" %s %s %.3fs', request.remote, request.method,
                         request.path, response.status, response.body_length, time)


class LoginServer:
    """Runs the Stud.IP + Uni Oldenburg SSO reverse proxy on the bot's event loop."""

    def __init__(self, on_login: LoginCallback, port: Optional[int] = None,
                 host: Optional[str] = None, bot_token: Optional[str] = None,
                 allowed_user_ids: Optional[set[int]] = None,
                 base_url_provider: Optional[Callable[[], str]] = None,
                 session_provider: Optional[Callable[..., object]] = None,
                 on_session_authenticated: Optional[Callable[..., Awaitable[None]]] = None,
                 is_allowed_callback: Optional[Callable[[int], bool]] = None):
        self.on_login = on_login
        self.port = int(port or os.getenv("LOGIN_PORT", "8899"))
        self.host = host or self._default_host()
        self.bot_token = bot_token or os.getenv("TELEGRAM_TOKEN", "")
        self.allowed_user_ids = allowed_user_ids or set()
        self.base_url_provider = base_url_provider
        self.session_provider = session_provider
        self.on_session_authenticated = on_session_authenticated
        self.is_allowed_callback = is_allowed_callback
        self._tokens: dict[str, dict] = {}
        self._proxy_sessions: dict[str, dict] = {}
        # Mixed into the cookie signing key. A fresh one per process and per
        # revoke_all() invalidates every proxy cookie handed out before.
        self._cookie_salt = secrets.token_bytes(16)
        self._runner: Optional[web.AppRunner] = None
        self._busy = False
        # per Telegram user, so two students signing in at once both get
        # their post-login hook (StuMS handoff, profile sync)
        self._last_auth_notify: dict = {}

    @staticmethod
    def _default_host() -> str:
        """Loopback when a tunnel or reverse proxy fronts the server (nothing
        else needs to reach it); all interfaces only for the LAN link mode."""
        explicit = os.getenv("LOGIN_HOST", "").strip()
        if explicit:
            return explicit
        if os.getenv("LOGIN_PUBLIC_URL", "").strip() or os.getenv("CLOUDFLARE_TUNNEL", "").strip():
            return "127.0.0.1"
        return "0.0.0.0"

    def _is_allowed(self, user_id) -> bool:
        # Check dynamic allowlist/DB callback first, then static allowlist
        if self.is_allowed_callback is not None:
            try:
                if self.is_allowed_callback(user_id):
                    return True
            except Exception as exc:
                logger.warning("is_allowed_callback error: %s", exc)
        return bool(self.allowed_user_ids) and user_id in self.allowed_user_ids

    def _resolve_session(self, user_id: Optional[int] = None):
        if not self.session_provider:
            return None
        try:
            return self.session_provider(user_id)
        except TypeError:
            return self.session_provider()

    def revoke_all(self) -> None:
        """Invalidate every link token and proxy cookie (called on /logout)."""
        self._tokens.clear()
        self._proxy_sessions.clear()
        self._cookie_salt = secrets.token_bytes(16)

    # ── urls / tokens ─────────────────────────────────────────────────────
    def base_url(self) -> str:
        if self.base_url_provider:
            dynamic = (self.base_url_provider() or "").strip()
            if dynamic:
                return dynamic.rstrip("/")
        public = os.getenv("LOGIN_PUBLIC_URL", "").strip()
        if public:
            return public.rstrip("/")
        return f"http://{_lan_ip()}:{self.port}"

    def app_url(self, target: str = "") -> str:
        """Mini App URL; target="stums" signs in to StuMS instead of Stud.IP."""
        return f"{self.base_url()}/app" + ("?target=stums" if target == "stums" else "")

    def new_link(self, user_id: int) -> str:
        self._purge()
        token = secrets.token_urlsafe(24)
        self._tokens[token] = {"user_id": user_id, "expires": time.time() + TOKEN_TTL}
        return f"{self.base_url()}/login/{token}"

    def _purge(self) -> None:
        now = time.time()
        for token in [t for t, d in self._tokens.items() if d["expires"] < now]:
            self._tokens.pop(token, None)
        for sid in [s for s, d in self._proxy_sessions.items() if d["expires"] < now]:
            self._proxy_sessions.pop(sid, None)

    def _cookie_key(self) -> bytes:
        return hashlib.sha256(
            b"studip-proxy-cookie:" + self._cookie_salt + (self.bot_token or "local").encode("utf-8")
        ).digest()

    def _csrf_token(self, sid: str) -> str:
        """Per-session token required by state-changing GET links (Relink)."""
        return hmac.new(self._cookie_key(), f"csrf:{sid}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _create_proxy_session(self, user_id: int) -> str:
        self._purge()
        exp = int(time.time() + PROXY_SESSION_TTL)
        payload = f"{user_id}.{exp}"
        sig = hmac.new(self._cookie_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        sid = f"{payload}.{sig}"
        self._proxy_sessions[sid] = {
            "user_id": user_id,
            "expires": float(exp),
        }
        return sid

    def _check_proxy_session(self, request: web.Request) -> Optional[dict]:
        self._purge()
        sid = request.cookies.get(PROXY_COOKIE, "")
        sess = self._proxy_sessions.get(sid)
        if sess:
            # No sliding expiry: a cookie stops working at its signed expiry
            # however often it is used.
            if sess["expires"] < time.time():
                self._proxy_sessions.pop(sid, None)
                return None
            sess["sid"] = sid
            return sess
        parts = sid.split(".")
        if len(parts) == 3:
            uid_s, exp_s, sig = parts
            try:
                uid = int(uid_s)
                exp = int(exp_s)
            except ValueError:
                return None
            if exp < time.time():
                return None
            expected = hmac.new(
                self._cookie_key(), f"{uid}.{exp}".encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(sig, expected):
                if not self._is_allowed(uid):
                    return None
                sess = {"user_id": uid, "expires": float(exp), "sid": sid}
                self._proxy_sessions[sid] = sess
                return sess
        return None

    def _set_auth_cookie(self, response: web.StreamResponse, sid: str, request: web.Request) -> None:
        is_https = (
            request.secure
            or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
            or self.base_url().startswith("https://")
        )
        response.set_cookie(
            PROXY_COOKIE,
            sid,
            max_age=PROXY_SESSION_TTL,
            httponly=True,
            secure=is_https,
            samesite="None" if is_https else "Lax",
            path="/",
        )

    # ── auth gate handlers ────────────────────────────────────────────────
    @staticmethod
    def _entry_target(request: web.Request) -> str:
        return "stums" if request.query.get("target") == "stums" else ""

    async def _app_gate(self, request: web.Request) -> web.Response:
        if self._check_proxy_session(request):
            raise web.HTTPFound("/_bot/entry?target=stums" if self._entry_target(request) else "/_bot/entry")
        return web.Response(
            text=_GATE_PAGE,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _bot_auth(self, request: web.Request) -> web.Response:
        user = verify_init_data(
            request.headers.get("X-Telegram-Init-Data", ""),
            self.bot_token,
        )
        if not user:
            return web.json_response({"ok": False, "error": "Invalid Telegram data."}, status=401)
        user_id = user.get("id")
        if not self._is_allowed(user_id):
            logger.warning("Mini App access from unauthorised user %s", user_id)
            return web.json_response({"ok": False, "error": "Not authorized."}, status=403)

        sid = self._create_proxy_session(user_id)
        resp = web.json_response({"ok": True, "redirect": "/_bot/entry"})
        self._set_auth_cookie(resp, sid, request)
        return resp

    async def _token_login(self, request: web.Request) -> web.Response:
        self._purge()
        token = request.match_info["token"]
        info = self._tokens.pop(token, None)
        if not info or not self._is_allowed(info.get("user_id")):
            return web.Response(
                text="<h1>Link expired</h1><p>Please send /login again in Telegram.</p>",
                status=410,
                content_type="text/html",
                charset="utf-8",
            )
        sid = self._create_proxy_session(info["user_id"])
        resp = web.HTTPFound("/_bot/entry")
        self._set_auth_cookie(resp, sid, request)
        raise resp

    async def _bot_entry(self, request: web.Request) -> web.Response:
        proxy_sess = self._check_proxy_session(request)
        if not proxy_sess:
            raise web.HTTPFound("/app")

        user_id = proxy_sess.get("user_id")
        # Relink logs the bot out of Stud.IP, so it must not be triggerable by
        # a cross-site GET (the cookie is SameSite=None for the Mini App
        # iframe): it only counts with this session's CSRF token.
        relogin = request.query.get("relogin") == "1" and hmac.compare_digest(
            request.query.get("csrf", ""), self._csrf_token(proxy_sess.get("sid", ""))
        )
        studip_sess = self._resolve_session(user_id)
        if studip_sess is not None and self._entry_target(request) == "stums" and not relogin:
            studip_sess.last_interactive_activity = time.time()
            await studip_sess.ensure_session()
            studip_sess.stums.verified_at = 0.0
            raise web.HTTPFound(STUMS_SSO_ENTRY_PATH)
        if studip_sess is not None:
            studip_sess.last_interactive_activity = time.time()
            await studip_sess.ensure_session()
            if relogin:
                await studip_sess.logout()
                studip_sess.last_interactive_activity = time.time()
            elif await studip_sess.is_html_logged_in(use_cache=False):
                studip_sess.explicitly_logged_out = False
                studip_sess.last_interactive_activity = 0.0
                # the hook also signs this session in to StuMS on the server
                self._schedule_auth_hook(user_id)
                raise web.HTTPFound("/dispatch.php/my_courses")
            else:
                has_cookies = False
                if getattr(studip_sess, "user_id", None):
                    try:
                        from unibot import db
                        has_cookies = bool(db.load_user_credential(studip_sess.user_id, "cookies"))
                    except Exception:
                        logging.debug("Ignored a non-critical error", exc_info=True)
                has_file = bool(studip_sess.cookie_file and os.path.exists(studip_sess.cookie_file))
                if studip_sess.explicitly_logged_out or (not has_cookies and not has_file):
                    studip_sess.session.cookie_jar.clear()
                try:
                    with interactive_requests():
                        async with await studip_sess.get(
                            f"{ELEARN_ORIGIN}/dispatch.php/my_courses",
                            allow_redirects=True,
                            timeout=10,
                        ) as _:
                            pass
                except Exception:
                    logging.debug("Stud.IP login check request failed", exc_info=True)

        # Start the real Uni Oldenburg OIDC SSO flow
        raise web.HTTPFound("/dispatch.php/login?again=yes&sso=oidc&cancel_login=1")

    def _schedule_auth_hook(self, user_id: Optional[int] = None) -> None:
        now = time.time()
        if now - self._last_auth_notify.get(user_id, 0.0) < 15.0:
            return
        self._last_auth_notify[user_id] = now
        if self.on_session_authenticated:
            asyncio.create_task(self._run_auth_hook(user_id))

    async def _run_auth_hook(self, user_id: Optional[int] = None) -> None:
        try:
            if self.on_session_authenticated:
                try:
                    await self.on_session_authenticated(user_id)
                except TypeError:
                    await self.on_session_authenticated()
        except Exception as exc:
            logger.warning("on_session_authenticated hook failed: %s", exc)

    # ── reverse proxy ─────────────────────────────────────────────────────
    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        proxy_sess = self._check_proxy_session(request)
        if not proxy_sess:
            if request.path in ("/", "/app"):
                return await self._app_gate(request)
            raise web.HTTPFound("/app")
        user_id = proxy_sess.get("user_id")
        relink_href = f"/_bot/entry?relogin=1&csrf={self._csrf_token(proxy_sess.get('sid', ''))}"

        studip_sess = self._resolve_session(user_id)
        if studip_sess is None:
            return web.Response(text="Bot session not initialized", status=503)
        await studip_sess.ensure_session()
        stums_signed_in = time.time() - studip_sess.stums.verified_at < 120
        if request.path.startswith(("/nidp", "/osp", "/sso.php", "/dispatch.php/login")) or (
                request.path.startswith("/qisserver") and not stums_signed_in):
            # a sign-in is in progress: background SSO handoffs wait for it.
            # Page requests of an already signed-in StuMS do not count, or they
            # would block the server-side Stud.IP handoff after a StuMS login.
            studip_sess.last_interactive_activity = time.time()

        path_qs = request.rel_url.path_qs
        upstream_origin = _upstream_for_path(request.path)
        target_url = f"{upstream_origin}{path_qs}"

        # Build upstream headers, rewriting Origin and Referer to match upstream
        fwd_headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in _HOP_BY_HOP_REQ or kl == "cookie" or kl.startswith(_STRIP_REQ_PREFIXES):
                continue
            if kl == "user-agent":
                # one User-Agent per session: the bot keeps using these
                # cookies in the background with its own User-Agent
                fwd_headers[k] = user_agent()
                continue
            if kl == "origin":
                fwd_headers[k] = upstream_origin
                continue
            if kl == "referer":
                ref_parts = urlsplit(v)
                ref_origin = _upstream_for_path(ref_parts.path or "/")
                ref_qs = f"?{ref_parts.query}" if ref_parts.query else ""
                fwd_headers[k] = f"{ref_origin}{ref_parts.path}{ref_qs}"
                continue
            fwd_headers[k] = v

        body = await request.read() if request.can_read_body else None

        try:
            with interactive_requests():
                up_resp = await studip_sess.request(
                    request.method,
                    target_url,
                    headers=fwd_headers,
                    data=body,
                    allow_redirects=False,
                    timeout=30,
                )
            async with up_resp:
                status = up_resp.status
                raw_body = await up_resp.read()
                up_headers = up_resp.headers
        except Exception as exc:
            logger.error("Proxy upstream error for %s: %s", redact_url(target_url), exc)
            return web.Response(text=f"Upstream error: {exc}", status=502)

        resp_headers = {}
        for k, v in up_headers.items():
            kl = k.lower()
            if kl in _STRIP_RESP_HEADERS:
                continue
            if kl == "location":
                m = _LOCATION_ORIGIN_RE.match(v.strip())
                if m:
                    v = m.group(1) or "/"
                resp_headers[k] = v
                continue
            resp_headers[k] = v

        resp_headers["Cache-Control"] = "no-store"

        ctype = up_headers.get("Content-Type", "")
        if "text/html" in ctype.lower():
            for hk in [k for k in resp_headers if k.lower() == "content-type"]:
                resp_headers.pop(hk, None)
            charset = "utf-8"
            if "charset=" in ctype.lower():
                charset = ctype.lower().split("charset=")[-1].split(";")[0].strip() or "utf-8"
            try:
                text = raw_body.decode(charset, errors="replace")
            except LookupError:  # unknown charset label from upstream
                text = raw_body.decode("utf-8", errors="replace")
            text = _ATTR_ORIGIN_RE.sub(r"\1/", text)
            text = _JS_ORIGIN_RE.sub(r"\1/", text)

            # Check if this is an authenticated Stud.IP or StuMS page
            is_studip_auth = (
                upstream_origin == ELEARN_ORIGIN
                and ("STUDIP.USER_ID" in text or "/dispatch.php/my_courses" in text)
                and '"username":"nobody"' not in text
                and "/dispatch.php/login" not in request.path
            )
            lowered = text.lower()
            is_stums_auth = (
                upstream_origin == STUMS_ORIGIN
                and "/qisserver/" in request.path
                and "/callback" not in request.path
                and "Login notwendig" not in text
                # HISinOne's anonymous pages carry the login form (asdf/fdsa)
                and 'name="asdf"' not in text
                and ("abmelden" in lowered or "logout" in lowered)
            )
            if is_stums_auth:
                studip_sess.stums.verified_at = time.time()
                studip_sess.stums.referer = f"{STUMS_ORIGIN}{request.path}"
                studip_sess.stums.blocked_until = 0.0  # interactive sign-in lifts the pause

            if is_studip_auth or is_stums_auth:
                studip_sess.explicitly_logged_out = False
                studip_sess.last_interactive_activity = 0.0
                # The hook signs in to the other service on the server
                # (Stud.IP -> StuMS and StuMS -> Stud.IP) with the fresh SSO
                # session, so the Mini App can close right away.
                self._schedule_auth_hook(user_id)
                bar = (
                    _STATUS_BAR_SNIPPET.replace("__STATUS_TEXT__", "✅ Bot connected")
                    + _CONNECTED_BANNER_SNIPPET
                )
            else:
                bar = (
                    "<script>try{sessionStorage.removeItem('_bot_close_hint_dismissed');sessionStorage.removeItem('_bot_haptic_done');}catch(e){}</script>"
                    + _STATUS_BAR_SNIPPET.replace("__STATUS_TEXT__", "🔐 Uni Oldenburg Login")
                )

            bar = bar.replace("/_bot/entry?relogin=1", relink_href)
            if "</body>" in text:
                text = text.replace("</body>", f"{bar}</body>", 1)
            else:
                text += bar

            return web.Response(
                text=text,
                status=status,
                headers=resp_headers,
                content_type="text/html",
                charset="utf-8",
            )

        return web.Response(
            body=raw_body,
            status=status,
            headers=resp_headers,
        )

    # ── fallback JSON API login ───────────────────────────────────────────
    async def _api_login(self, request: web.Request) -> web.Response:
        user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), self.bot_token)
        if not user:
            return web.json_response({"ok": False, "error": "Invalid Telegram data."}, status=401)
        user_id = user.get("id")
        if not self._is_allowed(user_id):
            return web.json_response({"ok": False, "error": "Not authorised."}, status=403)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid request."}, status=400)
        if self._busy:
            return web.json_response({"ok": False, "error": "Login already running."}, status=429)
        self._busy = True
        try:
            ok, message = await self.on_login(
                str(body.get("username", "")).strip(),
                str(body.get("password", "")),
                str(body.get("totp", "")).strip().replace(" ", ""),
            )
        finally:
            self._busy = False
        if not ok:
            return web.json_response({"ok": False, "error": message}, status=401)
        return web.json_response({"ok": True})

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get("/app", self._app_gate)
        app.router.add_post("/_bot/auth", self._bot_auth)
        app.router.add_get("/_bot/entry", self._bot_entry)
        app.router.add_get("/login/{token}", self._token_login)
        app.router.add_post("/api/login", self._api_login)
        app.router.add_get("/health", lambda _r: web.Response(text="ok"))
        app.router.add_route("*", "/{tail:.*}", self._proxy)
        self._runner = web.AppRunner(app, access_log_class=_RedactedAccessLogger)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        logger.info("Login server on :%s (public base: %s)", self.port, self.base_url())

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
