"""Access control, Stud.IP login, /login Mini App infrastructure and the instance lock.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import errno
import html
import logging
import os
from typing import Optional

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonDefault, Update, WebAppInfo
from telegram.ext import ApplicationHandlerStop, ContextTypes
from telegram.error import TelegramError

import studip_oauth
from studip_login_server import LoginServer
from studip_session import StudIPSession
from studip_tunnel import QuickTunnel

from unibot import state
from unibot.config import (
    ADMIN_USER_IDS,
    ALLOWED_USER_IDS,
    CLOUDFLARE_TUNNEL,
    LOGIN_PUBLIC_URL,
    TELEGRAM_TOKEN,
)


# Credentials are never stored in .env or in memory; authentication happens via
# the Telegram Mini App (/login) which persists only cookies & OAuth2 tokens.
USERNAME = ""
PASSWORD = ""
TOTP_SECRET = ""


async def _authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop or onboard updates. Allowed users proceed; new users can redeem invite codes."""
    user = update.effective_user
    if user is not None and is_user_allowed(user.id):
        action = None
        if update.callback_query and update.callback_query.data:
            action = f"cb:{update.callback_query.data[:30]}"
        elif update.effective_message and update.effective_message.text:
            txt = update.effective_message.text.strip()
            if txt.startswith("/"):
                action = f"cmd:{txt.split()[0][:20]}"
        try:
            from unibot.db import update_user_activity
            update_user_activity(user.id, action=action)
        except Exception:
            pass
        return

    # User is not allowed yet. Check if they are submitting an invite code in private chat.
    if user is not None and update.effective_message and update.effective_chat and update.effective_chat.type == "private":
        text = (update.effective_message.text or "").strip()
        code = None
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                param = parts[1].strip()
                if param.startswith("inv_"):
                    code = param[4:]
                elif param.upper().startswith("UNI-"):
                    code = param.upper()
        elif text.upper().startswith("UNI-"):
            code = text.upper()

        if code:
            from unibot.db import redeem_invite_token
            ok, msg = redeem_invite_token(code, user.id, full_name=user.full_name, username=user.username)
            if ok:
                login_url = _login_public_url()
                keyboard = []
                if login_url:
                    keyboard.append([InlineKeyboardButton("🔐 Connect Stud.IP Account", web_app=WebAppInfo(url=login_url))])
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                welcome_text = (
                    "🎉 <b>Access Granted!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Welcome, <b>{html.escape(user.first_name or 'Student')}</b>!\n\n"
                    "Your UniOldenburg Assistant account has been activated.\n\n"
                    "To synchronize your courses, exams, and grades, "
                    "please connect your Stud.IP account below."
                )
                await update.effective_message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
                raise ApplicationHandlerStop
            else:
                await update.effective_message.reply_text(
                    f"❌ <b>Invalid Code:</b> {msg}\n\nPlease check the invite code from your administrator and try again.",
                    parse_mode="HTML",
                )
                raise ApplicationHandlerStop

        # No invite code provided
        gate_text = (
            "🔒 <b>UniOldenburg Student Assistant</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "This bot is currently private and invite-only.\n\n"
            "To gain access, please send your <b>personalized invite code</b> "
            "(e.g., <code>UNI-ABC-123</code>) provided by your administrator.\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        try:
            await update.effective_message.reply_text(gate_text, parse_mode="HTML")
        except TelegramError:
            pass
        raise ApplicationHandlerStop

    logging.warning("⛔ Ignored update from unauthorised user %s", user.id if user else "unknown")
    try:
        if update.callback_query:
            await update.callback_query.answer("⛔ Access denied. Not authorized.", show_alert=True)
        elif update.effective_message and update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text("⛔ Not authorized.")
    except TelegramError:
        pass
    raise ApplicationHandlerStop


def is_user_allowed(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS:
        return True
    try:
        from unibot.db import is_user_allowed_db
        return is_user_allowed_db(user_id)
    except Exception:
        return False


def is_user_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if ADMIN_USER_IDS and user_id in ADMIN_USER_IDS:
        return True
    try:
        from unibot.db import is_user_admin_db
        return is_user_admin_db(user_id)
    except Exception:
        return False

# ── single-instance lock ───────────────────────────────────────────────────────
LOCK_FILE = ".bot_instance.lock"


def acquire_instance_lock():
    """Ensure only one process instance runs (prevents getUpdates 409)."""
    try:
        # Check if lock exists and if the PID is still running
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r") as f:
                    pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    logging.error("❌ Another bot instance is actually running (PID %s). Exiting.", pid)
                    return False
                else:
                    logging.warning("⚠️ Stale lock file found (PID %s not running). Removing it.", pid)
                    os.remove(LOCK_FILE)
            except Exception as e:
                logging.warning("⚠️ Failed to read/validate lock file: %s. Attempting to overwrite.", e)
                try: os.remove(LOCK_FILE)
                except OSError: pass

        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            logging.error("Another bot instance is already running (concurrency race).")
            return False
        raise


def release_instance_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass


# ── selectors (IdP flow) ─────────────────────────────────────────────────────────
SELECTORS = {
    "start": "main#content-wrapper a[href*='login']",
    "user": "#Ecom_User_ID",
    "user_btn": "#loginButton2",
    "pass": "#nffc, input[type='password']",
    "pass_btn": "#loginButton2",
    "otp": "#nffc, input[name*='otp'], input[name*='token']",
    "otp_btn": "#loginButton2",
}


# ── Stud.IP login ──────────────────────────────────────────────────────────────
async def login_studip(notify=None):
    
    if state.global_session is None:
        state.global_session = StudIPSession(USERNAME, PASSWORD, TOTP_SECRET)
    
    if await state.global_session.is_logged_in():
        return state.global_session

    if notify:
        await notify("Starting login...")
    
    success = await state.global_session.login()
    
    if success:
        state._login_failure_notified = False
        if notify:
            await notify("Login successful.")
        logging.info("Logged in successfully (browser-less session).")
        return state.global_session
    else:
        if notify:
            await notify("Login failed.")
        await _notify_repeated_login_failure()
        raise RuntimeError("Failed to log in to Stud.IP.")


LOGIN_FAILURE_NOTIFY_AFTER = 3


async def _notify_repeated_login_failure() -> None:
    """Tell the user once (not on every retry) that automatic login keeps
    failing, typically after a password change, so they can sign in again."""
    if state._login_failure_notified or state.global_session is None or state._menu_app is None:
        return
    if state.global_session.login_failures < LOGIN_FAILURE_NOTIFY_AFTER:
        return
    state._login_failure_notified = True
    text = (
        f"🔴 Automatic Stud.IP login failed {state.global_session.login_failures} times in a row "
        "(password or 2FA changed?). Retries now back off up to 1 hour to avoid an account lock.\n"
        "Sign in with /login to restore the session."
    )
    for uid in ALLOWED_USER_IDS:
        try:
            await state._menu_app.bot.send_message(chat_id=uid, text=text)
        except Exception as exc:
            logging.warning("Could not send login-failure alert to %s: %s", uid, exc)


def _login_public_url() -> str:
    """Live public address of the login page, tunnel first."""
    if state.quick_tunnel and state.quick_tunnel.url:
        return state.quick_tunnel.url
    return LOGIN_PUBLIC_URL


def _get_or_init_session() -> StudIPSession:
    if state.global_session is None:
        state.global_session = StudIPSession(USERNAME, PASSWORD, TOTP_SECRET)
    return state.global_session


async def _on_proxy_session_authenticated() -> None:
    """Called when the user completes login in the proxied Stud.IP Mini App."""
    from unibot.studip_api import get_studip_user_profile
    session = _get_or_init_session()
    session.explicitly_logged_out = False
    session.last_interactive_activity = 0.0
    session.reset_login_backoff()
    state._login_failure_notified = False
    await session.save_cookies()

    studip_cookie = None
    if session.session:
        for c in session.session.cookie_jar:
            if c.key == "Studip_Session":
                studip_cookie = c.value
                break

    use_oauth = studip_oauth.oauth_enabled()
    need_oauth = use_oauth and studip_oauth.is_expired(studip_oauth.load_tokens())
    is_new_session = bool(studip_cookie and studip_cookie != state._last_notified_studip_cookie)

    if use_oauth and (is_new_session or need_oauth):
        state._studip_api_user_id = None
        state._studip_user_profile = None
        try:
            await studip_oauth.authorize_with_session(session)
            await get_studip_user_profile()
        except Exception as exc:
            logging.warning("⚠️ OAuth consent after proxy login failed: %s", exc)
    elif is_new_session or state._studip_user_profile is None:
        try:
            await get_studip_user_profile()
        except Exception as exc:
            logging.debug("Profile sync after login: %s", exc)

    if is_new_session:
        state._last_notified_studip_cookie = studip_cookie
        if state._menu_app is not None:
            for uid in ALLOWED_USER_IDS or []:
                try:
                    await state._menu_app.bot.send_message(
                        chat_id=uid,
                        text="✅ Signed in to Stud.IP.",
                    )
                except Exception as exc:
                    logging.warning("⚠️ Could not send login confirmation to %s: %s", uid, exc)


def _get_login_server() -> LoginServer:
    if state.login_server is None:
        state.login_server = LoginServer(
            _perform_web_login,
            bot_token=TELEGRAM_TOKEN,
            allowed_user_ids=set(ALLOWED_USER_IDS),
            base_url_provider=_login_public_url,
            session_provider=_get_or_init_session,
            on_session_authenticated=_on_proxy_session_authenticated,
        )
    return state.login_server


async def _clear_menu_button(app) -> None:
    """Reset the chat menu button to Telegram's default so the Stud.IP button is removed."""
    try:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as exc:
        logging.debug("Could not reset default menu button: %s", exc)
    for uid in ALLOWED_USER_IDS or []:
        try:
            await app.bot.set_chat_menu_button(
                chat_id=uid,
                menu_button=MenuButtonDefault(),
            )
            logging.info("🧹 Removed Stud.IP menu button for %s", uid)
        except Exception as exc:
            logging.warning("⚠️ Could not reset menu button for %s: %s", uid, exc)


async def _on_tunnel_url(url: str) -> None:
    """Log when cloudflared hands us a tunnel URL."""
    logging.info("🌐 Tunnel URL ready: %s", url)


async def _start_login_infra(app) -> None:
    """Start the login page and, optionally, the Cloudflare tunnel."""

    state._menu_app = app
    if state.global_session and state.global_session.session:
        for c in state.global_session.session.cookie_jar:
            if c.key == "Studip_Session":
                state._last_notified_studip_cookie = c.value
                break
    mode = CLOUDFLARE_TUNNEL
    server = _get_login_server()

    if mode == "quick":
        state.quick_tunnel = QuickTunnel(server.port, on_url_change=_on_tunnel_url)
        try:
            await state.quick_tunnel.start()
        except Exception as exc:
            logging.error("❌ Cloudflare quick tunnel failed: %s", exc)
            state.quick_tunnel = None

    await server.start()
    logging.info("🔐 Login page available at %s", server.base_url())
    await _clear_menu_button(app)


async def _perform_web_login(username: str, password: str, totp: str) -> tuple[bool, str]:
    """Log in with credentials typed into the login page, then forget them.

    Called by LoginServer. The password lives only in this call; what survives
    is the session cookie plus an OAuth refresh token.
    """

    session = state.global_session
    if session is None:
        session = StudIPSession(username, password, "", totp_code=totp or None)
        state.global_session = session
    else:
        session.username = username
        session.password = password
        session.totp_code = totp or None

    logging.info("🔐 Login requested from the web login page...")
    try:
        ok = await session.login(force=True)
    except Exception as exc:
        logging.error("❌ Web login crashed: %s", exc)
        return False, "Stud.IP login failed. Please try again."

    if not ok:
        return False, "Stud.IP login failed. Check your credentials and the 2FA code."

    # Drop the credentials immediately - only the cookie survives.
    session.password = ""
    session.totp_code = None
    state._studip_api_user_id = None

    use_oauth = studip_oauth.oauth_enabled()
    if use_oauth:
        try:
            await studip_oauth.authorize_with_session(session)
        except Exception as exc:
            logging.warning("⚠️ OAuth consent failed after web login: %s", exc)

    if state._menu_app is not None:
        for uid in ALLOWED_USER_IDS or []:
            try:
                await state._menu_app.bot.send_message(
                    chat_id=uid,
                    text="✅ Signed in to Stud.IP.",
                )
            except Exception as exc:
                logging.warning("⚠️ Could not send login confirmation to %s: %s", uid, exc)

    return True, ""


async def _send_login_prompt(reply_target, user_id: int) -> None:
    """Send the Stud.IP Mini App login button to a message target."""
    server = _get_login_server()
    await server.start()

    public = _login_public_url()
    if public.startswith("https://"):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Open Stud.IP (Mini App)", web_app=WebAppInfo(url=server.app_url()))],
        ])
        await reply_target.reply_text(
            "Tap the button below to open Stud.IP and sign in.\n"
            "Once signed in, your session and OAuth2 token will be kept alive automatically.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    else:
        link = server.new_link(user_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔐 Open login page", url=link)
        ]])
        await reply_target.reply_text(
            "Tap the button to open the login page (valid for 10 minutes).\n"
            f"Same Wi-Fi required - the page is served at {server.base_url()}.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def _perform_logout() -> None:
    """Log out of Stud.IP + NetIQ SSO and wipe stored cookies and OAuth2 tokens."""
    state._studip_api_user_id = None
    state._studip_user_profile = None
    state._last_notified_studip_cookie = None
    if state.global_session is not None:
        await state.global_session.logout()
    if studip_oauth.oauth_enabled():
        studip_oauth.clear_tokens()
    # Any Mini App / link session still open must not keep proxying the account
    if state.login_server is not None:
        state.login_server.revoke_all()


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/login - open the Stud.IP login page (Mini App or browser)."""
    user = update.effective_user
    if not is_user_allowed(user.id if user else None):
        await update.message.reply_text("⛔ Not authorised.")
        return
    await _send_login_prompt(update.message, user.id)


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logout - sign out of Stud.IP and clear stored session cookies & OAuth2 tokens."""
    user = update.effective_user
    if not is_user_allowed(user.id if user else None):
        await update.message.reply_text("⛔ Not authorised.")
        return
    await _perform_logout()
    await update.message.reply_text(
        "🚪 Logged out from Stud.IP. Saved session cookies and OAuth2 tokens have been cleared.\n"
        "Use /login or the 🔐 Login button under ℹ️ Status whenever you want to sign in again."
    )
