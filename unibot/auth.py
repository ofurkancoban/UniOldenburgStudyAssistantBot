"""Access control, Stud.IP login, /login Mini App infrastructure and the instance lock.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import contextlib
import contextvars
import errno
import html
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import psutil
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Update,
    WebAppInfo,
)
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

import studip_oauth
from studip_http import BACKGROUND, INTERACTIVE, set_request_priority
from studip_login_server import LoginServer
import studip_session
from studip_session import StudIPSession, StumsState
from studip_tunnel import NamedTunnel, QuickTunnel
from unibot import db, state
from unibot.config import (
    ADMIN_USER_IDS,
    ALLOWED_USER_IDS,
    CLOUDFLARE_TUNNEL,
    CLOUDFLARE_TUNNEL_TOKEN,
    LOGIN_PUBLIC_URL,
    PRIMARY_ADMIN_ID,
    TELEGRAM_TOKEN,
)
from unibot.db import (
    is_user_admin_db,
    is_user_allowed_db,
    redeem_invite_token,
    update_user_activity,
)

# Credentials are never stored in .env or in memory; authentication happens via
# the Telegram Mini App (/login) which persists only cookies & OAuth2 tokens.
USERNAME = ""
PASSWORD = ""
TOTP_SECRET = ""

# The Telegram user an update (or a per-user background job) acts for. Set by
# _authorization_gate for every allowed update and by `acting_as()`, so code
# that asks for "the session" without naming a user gets that user's own
# Stud.IP/StuMS session instead of the admin's.
_current_user: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("current_telegram_user", default=None)


def current_user_id() -> Optional[int]:
    return _current_user.get()


def act_as_bot() -> None:
    """Mark the current task as the bot's own (background jobs started from a
    user's update must not inherit that user or its request priority)."""
    _current_user.set(None)
    set_request_priority(BACKGROUND)


def has_stored_login(sess) -> bool:
    """Whether a session has anything to sign in with (cookies or OAuth tokens)."""
    if sess is None or sess.explicitly_logged_out:
        return False
    if getattr(sess, "user_id", None):
        try:
            if db.get_user_scope(sess.user_id).credential("cookies"):
                return True
        except Exception:
            logging.debug("Ignored a non-critical error", exc_info=True)
    if sess.cookie_file and os.path.exists(sess.cookie_file):
        return True
    return bool(studip_oauth.oauth_enabled()
                and studip_oauth.load_tokens(getattr(sess, "user_id", None)) is not None)


@contextlib.contextmanager
def acting_as(user_id: Optional[int]):
    """Run a block on behalf of `user_id` (None: the bot's admin session)."""
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


# ── per-user rate limit ───────────────────────────────────────────────────────
# Sliding windows: at most RATE_PER_SECOND updates in one second and
# RATE_PER_MINUTE in a minute; going over starts a cooldown during which the
# user's updates are dropped (one warning when it starts).
RATE_PER_SECOND = 2
RATE_PER_MINUTE = 20
RATE_COOLDOWN_SECONDS = 30
RATE_WARNING = "⚠️ Please don't tap so fast. Try again in half a minute."
_rate_hits: dict[int, list] = {}
_rate_cooldown_until: dict[int, float] = {}


def rate_limit(user_id: int, now: Optional[float] = None) -> str:
    """"ok", "warn" (limit just exceeded: tell the user) or "drop"."""
    now = now if now is not None else time.monotonic()
    if now < _rate_cooldown_until.get(user_id, 0.0):
        return "drop"
    hits = [t for t in _rate_hits.get(user_id, []) if now - t < 60.0]
    hits.append(now)
    _rate_hits[user_id] = hits
    if sum(1 for t in hits if now - t < 1.0) > RATE_PER_SECOND or len(hits) > RATE_PER_MINUTE:
        _rate_cooldown_until[user_id] = now + RATE_COOLDOWN_SECONDS
        _rate_hits[user_id] = []
        return "warn"
    return "ok"


async def _rate_limited(update: Update, user_id: int) -> bool:
    """Apply the rate limit; True when the update must be dropped."""
    verdict = rate_limit(user_id)
    if verdict == "ok":
        return False
    if verdict == "warn":
        logging.info("Rate limit hit by user %s; cooling down", user_id)
        try:
            if update.callback_query:
                await update.callback_query.answer(RATE_WARNING, show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(RATE_WARNING)
        except TelegramError:
            pass
    elif update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError:
            pass
    return True


# ── user lifecycle (ACTIVE, NEEDS_REAUTH, INACTIVE) ──────────────────────────
# ACTIVE: background checks run. NEEDS_REAUTH: the university session expired
# and silent renewal failed twice in a row; background checks pause (no error
# logs) and the student is asked once to sign in again; a confirmed login
# makes them ACTIVE. INACTIVE: stopped with /stop or deleted their data; no
# background work at all until /start.
ACTIVE, NEEDS_REAUTH, INACTIVE = "active", "needs_reauth", "inactive"
REAUTH_AFTER_FAILURES = 2
REAUTH_PROMPT_INTERVAL_SECONDS = 6 * 3600
_renewal_failures: dict[int, int] = {}
_reauth_prompted_at: dict[int, float] = {}


def lifecycle_state(user_id: Optional[int]) -> str:
    if user_id is None:
        return ACTIVE
    try:
        return db.get_lifecycle(user_id)["state"]
    except Exception:
        logging.debug("Lifecycle lookup failed", exc_info=True)
        return ACTIVE


def _on_login_state(user_id: int, ok: bool) -> None:
    """studip_session listener: a confirmed login makes a NEEDS_REAUTH user
    ACTIVE again; repeated failed renewals move an ACTIVE one to NEEDS_REAUTH.
    Only registered users are tracked: session noise never creates a user."""
    if db.get_user(user_id) is None:
        return
    if ok:
        _renewal_failures.pop(user_id, None)
        if lifecycle_state(user_id) == NEEDS_REAUTH and db.set_lifecycle(user_id, ACTIVE, "signed in again"):
            logging.info("User %s signed in again: active", user_id)
        return
    _renewal_failures[user_id] = _renewal_failures.get(user_id, 0) + 1
    if _renewal_failures[user_id] >= REAUTH_AFTER_FAILURES and lifecycle_state(user_id) == ACTIVE:
        if db.set_lifecycle(user_id, NEEDS_REAUTH, "session expired, silent renewal failed"):
            logging.info("User %s needs to sign in again; background checks paused", user_id)


studip_session.LOGIN_STATE_LISTENERS.append(_on_login_state)


def reauth_prompt_due(user_id: int, now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    if now - _reauth_prompted_at.get(user_id, 0.0) < REAUTH_PROMPT_INTERVAL_SECONDS:
        return False
    _reauth_prompted_at[user_id] = now
    return True


REAUTH_TEXT = ("🔐 <b>Your university session has expired</b> (password change, timeout or 2FA).\n"
               "Background checks for you are paused until you sign in again. It takes one tap:")


class _ChatTarget:
    """reply_text() into a chat, for prompts sent outside an update."""

    def __init__(self, bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id

    async def reply_text(self, text, **kwargs):
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)


async def send_reauth_notice(bot, user_id: int) -> None:
    """Tell an expired student once, with the one-tap sign-in button."""
    await bot.send_message(chat_id=user_id, text=REAUTH_TEXT, parse_mode="HTML")
    await _send_login_prompt(_ChatTarget(bot, user_id), user_id)
    _reauth_prompted_at[user_id] = time.time()


async def _lifecycle_on_interaction(update: Update, user_id: int) -> None:
    """Resume a stopped student on /start; remind an expired one to sign in."""
    state_now = lifecycle_state(user_id)
    message = update.effective_message
    text = (message.text or "").strip() if message is not None else ""
    if state_now == INACTIVE and text.startswith("/start"):
        db.set_lifecycle(user_id, ACTIVE, "resumed with /start")
        logging.info("User %s resumed with /start", user_id)
    elif state_now == NEEDS_REAUTH and message is not None and reauth_prompt_due(user_id):
        try:
            await message.reply_text(REAUTH_TEXT, parse_mode="HTML")
            await _send_login_prompt(message, user_id)
        except TelegramError:
            logging.debug("Re-auth prompt failed", exc_info=True)


async def _authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop or onboard updates. Allowed users proceed (rate limited, with the
    lifecycle hooks); new users can redeem invite codes."""
    user = update.effective_user
    _current_user.set(None)
    set_request_priority(BACKGROUND)
    if user is not None and is_user_allowed(user.id):
        if await _rate_limited(update, user.id):
            raise ApplicationHandlerStop
        await _lifecycle_on_interaction(update, user.id)
        _current_user.set(user.id)
        set_request_priority(INTERACTIVE)
        action = None
        if update.callback_query and update.callback_query.data:
            action = f"cb:{update.callback_query.data[:30]}"
        elif update.effective_message and update.effective_message.text:
            txt = update.effective_message.text.strip()
            if txt.startswith("/"):
                action = f"cmd:{txt.split()[0][:20]}"
        try:
            update_user_activity(user.id, action=action)
        except Exception:
            logging.debug("Ignored a non-critical error", exc_info=True)
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
            ok, msg = redeem_invite_token(code, user.id, full_name=user.full_name, username=user.username)
            if ok:
                welcome_text = (
                    "🎉 <b>Welcome to UniOldenburg Assistant!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"Hello, <b>{html.escape(user.first_name or 'Student')}</b>! Your account has been activated.\n\n"
                    "Here is what you can do right away:\n"
                    "• <b>Sync Courses & Files:</b> Browse lecture slides, course files, and updates.\n"
                    "• <b>Timetable & Deadlines:</b> Check your upcoming schedule and room locations.\n"
                    "• <b>Mensa Menu & Tasks:</b> View daily meals and manage assignment deadlines.\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "👉 <i>Please sign in to Stud.IP to link your student profile:</i>"
                )
                await update.effective_message.reply_text(welcome_text, parse_mode="HTML")
                await _send_login_prompt(update.effective_message, user.id)
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
            "This bot works by private invitation only.\n\n"
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
        return is_user_allowed_db(user_id)
    except Exception:
        logging.warning("Access check failed for a user; denied", exc_info=True)
        return False


def is_user_admin(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    if ADMIN_USER_IDS and user_id in ADMIN_USER_IDS:
        return True
    try:
        return is_user_admin_db(user_id)
    except Exception:
        logging.warning("Admin check failed for a user; not an admin", exc_info=True)
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
                    # normal after every restart (PM2 stops the old process first)
                    logging.info("Stale lock file found (PID %s not running). Removing it.", pid)
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
def _admin_candidates() -> list[int]:
    ids: list[int] = []
    if PRIMARY_ADMIN_ID is not None:
        ids.append(PRIMARY_ADMIN_ID)
    ids += sorted(ALLOWED_USER_IDS)
    try:
        ids += [u["telegram_id"] for u in db.get_all_users() if u.get("role") == "admin"]
    except Exception:
        logging.debug("Ignored a non-critical error", exc_info=True)
    return [uid for i, uid in enumerate(ids) if uid not in ids[:i] and is_user_admin(uid)]


def pick_watcher_session(exclude: Optional[int] = None) -> StudIPSession:
    """The session the background watcher uses: a signed-in admin's own session.

    Prefers an admin session already open in this process, then an admin with
    a stored login. Without any, an unbound session that stays logged out
    until an admin signs in (it adopts the first admin who does)."""
    candidates = [uid for uid in _admin_candidates() if uid != exclude]
    for uid in candidates:
        sess = state.user_sessions.get(uid)
        if sess is not None and not sess.explicitly_logged_out:
            return sess
    for uid in candidates:
        try:
            has_login = bool(db.get_user_scope(uid).credential("cookies"))
        except Exception:
            logging.debug("Stored login unreadable for an admin candidate", exc_info=True)
            has_login = False
        if has_login or os.path.exists(f"studip_cookies_{uid}.json"):
            sess = StudIPSession(USERNAME, PASSWORD, TOTP_SECRET,
                                 cookie_file=f"studip_cookies_{uid}.json", user_id=uid)
            state.user_sessions[uid] = sess
            logging.info("🔐 Watcher session uses the stored login of admin %s", uid)
            return sess
    return StudIPSession(USERNAME, PASSWORD, TOTP_SECRET, cookie_file=None)


def get_user_session(user_id: Optional[int] = None) -> StudIPSession:
    """Retrieve or initialize the StudIPSession for a specific Telegram user.

    Without an explicit user_id the user the current update acts for is used;
    only outside of any user context (background jobs) is it the admin session."""
    if user_id is None:
        user_id = current_user_id()
    if user_id is None:
        watcher = state.global_session
        if watcher is None:
            watcher = state.global_session = pick_watcher_session()
        return watcher

    if user_id in state.user_sessions:
        return state.user_sessions[user_id]

    watcher = state.global_session
    if is_user_admin(user_id) and watcher is not None and not watcher.user_id:
        watcher.user_id = user_id
        watcher.cookie_file = f"studip_cookies_{user_id}.json"
        state.user_sessions[user_id] = watcher
        return watcher

    # Always the user's own file: the single-user era studip_cookies.json is
    # migrated to the primary admin at startup and never shared between admins.
    cookie_file = f"studip_cookies_{user_id}.json"

    sess = StudIPSession(
        USERNAME, PASSWORD, TOTP_SECRET,
        cookie_file=cookie_file,
        user_id=user_id,
    )
    state.user_sessions[user_id] = sess
    if state.global_session is None and is_user_admin(user_id):
        state.global_session = sess
    return sess


def personal_session(user_id: Optional[int]) -> StudIPSession:
    """The session for a student's personal StuMS data (grades, exam
    registrations, fees, official documents). It is always that student's
    own session: there is no fallback to the watcher or an admin account."""
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("personal data needs an explicit user id")
    return get_user_session(user_id)


async def login_studip(notify=None, user_id: Optional[int] = None):
    if user_id is None:
        user_id = current_user_id()
    session = get_user_session(user_id)
    if await session.is_logged_in():
        return session

    if notify:
        await notify("Starting login...")

    success = await session.login()

    if success:
        state._login_failure_notified = False
        if notify:
            await notify("Login successful.")
        logging.info("Logged in successfully (browser-less session).")
        return session
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
    # Only alert if headless credentials were configured. Interactive web logins
    # expire normally and should prompt for interactive sign-in instead of spamming alerts.
    has_headless = bool(state.global_session.username and state.global_session.password)
    if not has_headless:
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


def is_new_login_for(session: StudIPSession) -> bool:
    """True when the session's Stud.IP cookie changed since the last check."""
    current = None
    if session.session:
        for c in session.session.cookie_jar:
            if c.key == "Studip_Session":
                current = c.value
                break
    previous = session.last_seen_studip_cookie
    session.last_seen_studip_cookie = current
    return bool(current) and current != previous


def _login_public_url() -> str:
    """Live public address of the login page, tunnel first."""
    if state.quick_tunnel and state.quick_tunnel.url:
        return state.quick_tunnel.url
    return LOGIN_PUBLIC_URL


def _get_or_init_session() -> StudIPSession:
    return get_user_session(None)


async def _on_proxy_session_authenticated(user_id: Optional[int] = None) -> None:
    """Called when the user completes login in the proxied Stud.IP Mini App."""
    from unibot.studip_api import forget_user_profile, get_studip_user_profile

    session = get_user_session(user_id)
    if is_new_login_for(session):
        forget_user_profile(user_id)
    session.explicitly_logged_out = False
    session.last_interactive_activity = 0.0
    session.reset_login_backoff()
    state._login_failure_notified = False
    await session.save_cookies()

    # Signed in through StuMS: sign the same session in to Stud.IP as well.
    try:
        if not await session.is_html_logged_in(use_cache=False):
            if await session.heal_session():
                logging.info("✅ Stud.IP session established during login for user %s", user_id)
    except Exception as exc:
        logging.info("Stud.IP SSO handoff after proxy login failed for user %s: %s", user_id, exc)

    studip_cookie = None
    if session.session:
        for c in session.session.cookie_jar:
            if c.key == "Studip_Session":
                studip_cookie = c.value
                break

    use_oauth = studip_oauth.oauth_enabled()
    need_oauth = use_oauth and studip_oauth.is_expired(studip_oauth.load_tokens(user_id=user_id))
    is_new_session = bool(studip_cookie and studip_cookie != state._last_notified_studip_cookie)

    profile = {}
    if use_oauth and (is_new_session or need_oauth):
        if user_id is None or is_user_admin(user_id):
            state._studip_api_user_id = None
            state._studip_user_profile = None
        try:
            await studip_oauth.authorize_with_session(session, user_id=user_id)
            profile = await get_studip_user_profile(user_id=user_id, session=session)
        except Exception as exc:
            logging.warning("⚠️ OAuth consent after proxy login failed for user %s: %s", user_id, exc)
    elif is_new_session or state._studip_user_profile is None:
        try:
            profile = await get_studip_user_profile(user_id=user_id, session=session)
        except Exception as exc:
            logging.debug("Profile sync after login for user %s: %s", user_id, exc)

    if profile and user_id is not None:
        studip_user = profile.get("username")
        display_name = profile.get("display_name")
        if studip_user or display_name:
            db.upsert_user(telegram_id=user_id, studip_username=studip_user)

    # One sign-in connects both services: sign this session in to StuMS on
    # the server while the NetIQ SSO session from the Mini App is fresh.
    try:
        from stums_client import _establish_stums_session
        session.stums.blocked_until = 0.0  # a fresh login lifts the background pause
        await _establish_stums_session(session)
        await session.save_cookies()
        logging.info("🎓 StuMS session established during login for user %s", user_id)
    except Exception as exc:
        logging.info("StuMS SSO handoff after proxy login failed for user %s: %s", user_id, exc)

    if is_new_session and (user_id is None or is_user_admin(user_id)):
        state._last_notified_studip_cookie = studip_cookie

    if state._menu_app is not None:
        target_uids = [user_id] if user_id else (ALLOWED_USER_IDS or [])
        for uid in target_uids:
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
            bot_token=TELEGRAM_TOKEN,
            allowed_user_ids=set(ALLOWED_USER_IDS),
            base_url_provider=_login_public_url,
            session_provider=get_user_session,
            on_session_authenticated=_on_proxy_session_authenticated,
            is_allowed_callback=is_user_allowed,
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

    if mode == "named" and CLOUDFLARE_TUNNEL_TOKEN and LOGIN_PUBLIC_URL.startswith("https://"):
        state.quick_tunnel = NamedTunnel(server.port, LOGIN_PUBLIC_URL, CLOUDFLARE_TUNNEL_TOKEN,
                                         on_url_change=_on_tunnel_url)
        try:
            await state.quick_tunnel.start()
        except Exception as exc:
            logging.error("❌ Named Cloudflare tunnel failed: %s", exc)
            state.quick_tunnel = None
    elif mode == "quick":
        state.quick_tunnel = QuickTunnel(server.port, on_url_change=_on_tunnel_url)
        try:
            await state.quick_tunnel.start()
        except Exception as exc:
            logging.error("❌ Cloudflare quick tunnel failed: %s", exc)
            state.quick_tunnel = None

    await server.start()
    logging.info("🔐 Login page available at %s", server.base_url())
    await _clear_menu_button(app)


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
            "Tap the button below to sign in once with your university account.\n"
            "The same sign-in connects Stud.IP and StuMS (exams, grades), and both "
            "sessions are kept alive automatically.",
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


# ── account registry ──────────────────────────────────────────────────────────
# The one place that answers "which accounts exist / are signed in": the
# watcher, the course plan, the keep-alive loop and diagnostics all ask here.

def all_sessions() -> list:
    """Every open session once: the watcher's first, then each user's."""
    sessions = {}
    if state.global_session is not None:
        sessions[id(state.global_session)] = state.global_session
    for sess in state.user_sessions.values():
        if sess is not None:
            sessions.setdefault(id(sess), sess)
    return list(sessions.values())


@dataclass(frozen=True)
class Account:
    """One Telegram user's university account: it owns the user's
    StudIPSession (Stud.IP and StuMS sign-in, cookies, HTTP connections) and
    answers everything the bot needs to know about that account."""
    user_id: int
    session: StudIPSession

    @property
    def signed_in(self) -> bool:
        """Has a stored login (cookies or tokens) and did not log out."""
        return has_stored_login(self.session)

    @property
    def stums(self) -> StumsState:
        return self.session.stums

    @property
    def courses(self) -> dict:
        """{cid: name} of the courses Stud.IP lists for this user."""
        return state.user_courses_map.get(self.user_id) or {}

    @property
    def is_watcher(self) -> bool:
        return self.session is state.global_session

    async def close(self) -> None:
        await self.session.close()


def get_account(user_id: int) -> Account:
    """The user's Account, creating its session on first use."""
    return Account(user_id, state.user_sessions.get(user_id) or get_user_session(user_id))


def accounts() -> list:
    """Every active user's Account, admins from the environment first."""
    try:
        active = [u["telegram_id"] for u in db.get_all_users() if u.get("is_active", 1)]
    except Exception:
        logging.debug("User list unavailable", exc_info=True)
        active = []
    found = []
    for uid in dict.fromkeys(sorted(ALLOWED_USER_IDS) + active):
        try:
            found.append(get_account(uid))
        except Exception:
            logging.debug("No session for %s", uid, exc_info=True)
    return found


def signed_in_accounts() -> list:
    """The Accounts with a stored login whose lifecycle is ACTIVE: the ones
    background checks run for (NEEDS_REAUTH and INACTIVE students cost
    nothing until they sign in again or come back)."""
    return [account for account in accounts() if account.signed_in and lifecycle_state(account.user_id) == ACTIVE]


async def close_all_sessions() -> None:
    """Close every user's HTTP connections (on shutdown)."""
    for sess in all_sessions():
        try:
            await sess.close()
        except Exception:
            logging.debug("Closing a session failed", exc_info=True)


async def _perform_logout(user_id: Optional[int] = None) -> None:
    """Log out of Stud.IP and clear stored session cookies and OAuth2 tokens."""
    from unibot.studip_api import forget_user_profile

    forget_user_profile(user_id)
    if user_id is not None:
        sess = state.user_sessions.pop(user_id, None)
        if sess is not None:
            await sess.logout()
        db.get_user_scope(user_id).delete_credentials()
        if studip_oauth.oauth_enabled():
            studip_oauth.clear_tokens(user_id=user_id)
        db.update_user_studip(user_id, "")
        state.user_courses_map.pop(user_id, None)
        if state.global_session is sess:
            # keep the watcher running on another signed-in admin, if any
            state.global_session = pick_watcher_session(exclude=user_id)
            state._studip_api_user_id = None
            state._studip_user_profile = None
            state._last_notified_studip_cookie = None
        if sess is not None and sess is not state.global_session:
            await sess.close()  # its HTTP connections would otherwise leak
        return

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
    user, message = update.effective_user, update.message
    if message is None:
        return
    if user is None or not is_user_allowed(user.id):
        await message.reply_text("⛔ Not authorised.")
        return
    await _send_login_prompt(message, user.id)


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/logout - sign out of Stud.IP and clear stored session cookies & OAuth2 tokens."""
    user, message = update.effective_user, update.message
    uid = user.id if user else None
    if message is None:
        return
    if not is_user_allowed(uid):
        await message.reply_text("⛔ Not authorised.")
        return
    await _perform_logout(user_id=uid)
    await message.reply_text(
        "🚪 Logged out from Stud.IP. Saved session cookies and OAuth2 tokens have been cleared.\n"
        "Use /login or the 🔐 Login button under ℹ️ Status whenever you want to sign in again."
    )
