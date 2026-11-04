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
import html
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

logger = logging.getLogger("StudIPLoginServer")

TOKEN_TTL = 600          # seconds a link token stays valid
INITDATA_MAX_AGE = 3600  # seconds a Mini App payload stays valid
PROXY_COOKIE = "_bot_proxy_auth"
PROXY_SESSION_TTL = 3600

ELEARN_ORIGIN = "https://elearning.uni-oldenburg.de"
LOGIN_ORIGIN = "https://login.uni-oldenburg.de"
AUTH_ORIGIN = "https://auth.uni-oldenburg.de"

LoginCallback = Callable[[str, str, str], Awaitable[tuple[bool, str]]]

# Rewrite leading scheme+host in HTML attributes (action, href, src) without
# touching query-string parameters like `redirect_uri=https://...`.
_ATTR_ORIGIN_RE = re.compile(
    r'(\b(?:action|href|src)\s*=\s*["\'])https://(?:elearning|login|idp|auth)\.uni-oldenburg\.de(?::443)?/',
    re.IGNORECASE,
)

# Rewrite JS assignments like `window.location.href = "https://..."`
_JS_ORIGIN_RE = re.compile(
    r'((?:location(?:\.href)?|redirectUri)\s*=\s*["\'])https://(?:elearning|login|idp|auth)\.uni-oldenburg\.de(?::443)?/',
    re.IGNORECASE,
)

_LOCATION_ORIGIN_RE = re.compile(
    r"^https://(?:elearning|login|idp|auth)\.uni-oldenburg\.de(?::443)?(/.*)?$",
    re.IGNORECASE,
)

_HOP_BY_HOP_REQ = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "accept-encoding",
}

_STRIP_RESP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "set-cookie", "content-security-policy",
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
    window.location.replace(data.redirect || '/_bot/entry');
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
function _botCloseApp() {
  try {
    if (window.Telegram && Telegram.WebApp && Telegram.WebApp.initData) {
      Telegram.WebApp.close();
    } else {
      window.close();
    }
  } catch(e){}
}
(function(){
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


class LoginServer:
    """Runs the Stud.IP + Uni Oldenburg SSO reverse proxy on the bot's event loop."""

    def __init__(self, on_login: LoginCallback, port: Optional[int] = None,
                 host: str = "0.0.0.0", bot_token: Optional[str] = None,
                 allowed_user_ids: Optional[set[int]] = None,
                 base_url_provider: Optional[Callable[[], str]] = None,
                 session_provider: Optional[Callable[[], object]] = None,
                 on_session_authenticated: Optional[Callable[[], Awaitable[None]]] = None):
        self.on_login = on_login
        self.port = int(port or os.getenv("LOGIN_PORT", "8899"))
        self.host = host
        self.bot_token = bot_token or os.getenv("TELEGRAM_TOKEN", "")
        self.allowed_user_ids = allowed_user_ids or set()
        self.base_url_provider = base_url_provider
        self.session_provider = session_provider
        self.on_session_authenticated = on_session_authenticated
        self._tokens: dict[str, dict] = {}
        self._proxy_sessions: dict[str, dict] = {}
        self._runner: Optional[web.AppRunner] = None
        self._busy = False
        self._last_auth_notify = 0.0

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

    def app_url(self) -> str:
        return f"{self.base_url()}/app"

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
            f"studip-proxy-cookie:{self.bot_token or 'local'}".encode("utf-8")
        ).digest()

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
            sess["expires"] = time.time() + PROXY_SESSION_TTL
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
                if self.allowed_user_ids and uid not in self.allowed_user_ids:
                    return None
                sess = {"user_id": uid, "expires": float(exp)}
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
    async def _app_gate(self, request: web.Request) -> web.Response:
        if self._check_proxy_session(request):
            raise web.HTTPFound("/_bot/entry")
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
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
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
        if not info:
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
        if not self._check_proxy_session(request):
            return web.HTTPFound("/app")

        relogin = request.query.get("relogin") == "1"
        studip_sess = self.session_provider() if self.session_provider else None
        if studip_sess is not None:
            studip_sess.last_interactive_activity = time.time()
            await studip_sess.ensure_session()
            if relogin:
                await studip_sess.logout()
                studip_sess.last_interactive_activity = time.time()
            elif await studip_sess.is_html_logged_in():
                studip_sess.explicitly_logged_out = False
                studip_sess.last_interactive_activity = 0.0
                self._schedule_auth_hook()
                raise web.HTTPFound("/dispatch.php/my_courses")
            else:
                if studip_sess.explicitly_logged_out or not os.path.exists(studip_sess.cookie_file):
                    studip_sess.session.cookie_jar.clear()
                try:
                    async with studip_sess.session.get(
                        f"{ELEARN_ORIGIN}/dispatch.php/my_courses",
                        allow_redirects=True,
                        timeout=10,
                    ) as _:
                        pass
                except Exception:
                    pass

        # Start the real Uni Oldenburg OIDC SSO flow
        raise web.HTTPFound("/dispatch.php/login?again=yes&sso=oidc&cancel_login=1")

    def _schedule_auth_hook(self) -> None:
        now = time.time()
        if now - self._last_auth_notify < 15.0:
            return
        self._last_auth_notify = now
        if self.on_session_authenticated:
            asyncio.create_task(self._run_auth_hook())

    async def _run_auth_hook(self) -> None:
        try:
            if self.on_session_authenticated:
                await self.on_session_authenticated()
        except Exception as exc:
            logger.warning("on_session_authenticated hook failed: %s", exc)

    # ── reverse proxy ─────────────────────────────────────────────────────
    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        if not self._check_proxy_session(request):
            if request.path in ("/", "/app"):
                return await self._app_gate(request)
            return web.HTTPFound("/app")

        studip_sess = self.session_provider() if self.session_provider else None
        if studip_sess is None:
            return web.Response(text="Bot session not initialized", status=503)
        await studip_sess.ensure_session()
        if request.path.startswith(("/nidp", "/osp", "/sso.php", "/dispatch.php/login")):
            studip_sess.last_interactive_activity = time.time()
        http = studip_sess.session

        path_qs = request.rel_url.path_qs
        upstream_origin = _upstream_for_path(request.path)
        target_url = f"{upstream_origin}{path_qs}"

        # Build upstream headers, rewriting Origin and Referer to match upstream
        fwd_headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in _HOP_BY_HOP_REQ or kl == "cookie":
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
            async with http.request(
                request.method,
                target_url,
                headers=fwd_headers,
                data=body,
                allow_redirects=False,
                timeout=30,
            ) as up_resp:
                status = up_resp.status
                raw_body = await up_resp.read()
                up_headers = up_resp.headers
        except Exception as exc:
            logger.error("Proxy upstream error for %s: %s", target_url, exc)
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
            text = raw_body.decode(charset, errors="replace")
            text = _ATTR_ORIGIN_RE.sub(r"\1/", text)
            text = _JS_ORIGIN_RE.sub(r"\1/", text)

            # Check if this is an authenticated Stud.IP page
            is_studip_auth = (
                upstream_origin == ELEARN_ORIGIN
                and ("STUDIP.USER_ID" in text or "/dispatch.php/my_courses" in text)
                and '"username":"nobody"' not in text
                and "/dispatch.php/login" not in request.path
            )
            if is_studip_auth:
                studip_sess.explicitly_logged_out = False
                studip_sess.last_interactive_activity = 0.0
                self._schedule_auth_hook()
                bar = (
                    _CONNECTED_BANNER_SNIPPET
                    + _STATUS_BAR_SNIPPET.replace("__STATUS_TEXT__", "✅ Bot connected")
                )
            else:
                bar = (
                    "<script>try{sessionStorage.removeItem('_bot_close_hint_dismissed');sessionStorage.removeItem('_bot_haptic_done');}catch(e){}</script>"
                    + _STATUS_BAR_SNIPPET.replace("__STATUS_TEXT__", "🔐 Uni Oldenburg Login")
                )

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
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
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
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        logger.info("Login server on :%s (public base: %s)", self.port, self.base_url())

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
