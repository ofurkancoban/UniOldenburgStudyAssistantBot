"""Access control, Stud.IP login, /login Mini App infrastructure and the instance lock.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import atexit
import contextlib
import contextvars
import importlib
import html
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

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
    db.clear_private_write_context()
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
        with db.private_write_context(user_id):
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
RATE_TABLE_PRUNE_AT = 2000  # entries before idle users are dropped


def _prune_rate_tables(now: float) -> None:
    """Forget users idle for over a minute and finished cooldowns, so
    accounts that wrote once don't stay in memory for good."""
    for uid in [u for u, hits in _rate_hits.items() if not hits or now - hits[-1] >= 60.0]:
        _rate_hits.pop(uid, None)
    for uid in [u for u, until in _rate_cooldown_until.items() if until <= now]:
        _rate_cooldown_until.pop(uid, None)


def rate_limit(user_id: int, now: Optional[float] = None) -> str:
    """"ok", "warn" (limit just exceeded: tell the user) or "drop"."""
    now = now if now is not None else time.monotonic()
    if len(_rate_hits) + len(_rate_cooldown_until) > RATE_TABLE_PRUNE_AT:
        _prune_rate_tables(now)
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


def forget_user_runtime_state(user_id: int) -> None:
    """Drop ephemeral authentication and rate-limit state for a deleted user."""
    uid = int(user_id)
    for table in (_rate_hits, _rate_cooldown_until, _renewal_failures, _reauth_prompted_at,
                  _gate_replied):
        table.pop(uid, None)
    _invite_fail_closed.discard(uid)


def lifecycle_state(user_id: Optional[int]) -> str:
    if user_id is None:
        return ACTIVE
    try:
        return db.get_lifecycle(user_id)["state"]
    except Exception:
        logging.debug("Lifecycle lookup failed", exc_info=True)
        # A failed lookup must not silently resume background access to a
        # student's account. A later successful interaction or DB recovery
        # can restore the real lifecycle state.
        return INACTIVE


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
    """Whether an expired user may be prompted again.

    The caller records the timestamp only after both Telegram messages have
    been delivered, so a transient send failure remains retryable.
    """
    now = now if now is not None else time.time()
    return now - _reauth_prompted_at.get(user_id, 0.0) >= REAUTH_PROMPT_INTERVAL_SECONDS


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
    with acting_as(user_id):
        if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                or lifecycle_state(user_id) != NEEDS_REAUTH):
            return
        await bot.send_message(chat_id=user_id, text=REAUTH_TEXT, parse_mode="HTML")
        if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                or lifecycle_state(user_id) != NEEDS_REAUTH):
            return
        await _send_login_prompt(_ChatTarget(bot, user_id), user_id)
        if db.private_context_is_current(user_id) and lifecycle_state(user_id) == NEEDS_REAUTH:
            _reauth_prompted_at[user_id] = time.time()


async def _lifecycle_on_interaction(update: Update, user_id: int) -> None:
    """Resume a stopped student on /start; remind an expired one to sign in."""
    state_now = lifecycle_state(user_id)
    message = update.effective_message
    text = (message.text or "").strip() if message is not None else ""
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if state_now == INACTIVE and command == "/start":
        db.set_lifecycle(user_id, ACTIVE, "resumed with /start")
        logging.info("User %s resumed with /start", user_id)
    elif state_now == NEEDS_REAUTH and message is not None and reauth_prompt_due(user_id):
        try:
            await message.reply_text(REAUTH_TEXT, parse_mode="HTML")
            await _send_login_prompt(message, user_id)
            if (db.private_context_is_current(user_id) and is_user_allowed(user_id)
                    and lifecycle_state(user_id) == NEEDS_REAUTH):
                _reauth_prompted_at[user_id] = time.time()
        except TelegramError:
            logging.debug("Re-auth prompt failed", exc_info=True)


async def _authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop or onboard updates. Allowed users proceed (rate limited, with the
    lifecycle hooks); new users can redeem invite codes."""
    user = update.effective_user
    _current_user.set(None)
    db.clear_private_write_context()
    set_request_priority(BACKGROUND)
    if user is not None and is_user_allowed(user.id):
        if db.private_deletion_in_progress(user.id):
            # Do not let a concurrent /start reactivate the account or let a
            # stale callback start new user-scoped work before deletion ends.
            raise ApplicationHandlerStop
        chat = update.effective_chat
        if chat is None or chat.type != "private":
            # replies carry private data (grades, files, schedule): never answer in a group
            logging.info("Ignored an update from a non-private chat")
            raise ApplicationHandlerStop
        # Capture the account generation before the first await. If deletion
        # starts and finishes while Telegram is answering a prompt, this
        # update must remain stale after deletion has reopened the account.
        _current_user.set(user.id)
        db.set_private_write_context(user.id)
        if await _rate_limited(update, user.id):
            _current_user.set(None)
            db.clear_private_write_context()
            raise ApplicationHandlerStop
        if not is_user_allowed(user.id) or not db.private_context_is_current(user.id):
            _current_user.set(None)
            db.clear_private_write_context()
            raise ApplicationHandlerStop
        message = update.effective_message
        command_text = (message.text or "").strip() if message is not None else ""
        if command_text.startswith("/"):
            command = command_text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command != "/cancel" and context is not None and context.user_data is not None:
                # A slash command is a deliberate change of activity. Do not
                # leave a text/photo wizard armed to consume an unrelated
                # message sent after that command (notably admin broadcasts
                # and wardrobe photos).
                for key in (
                    "pending_step", "task_wizard", "fastenroll_wizard",
                    "studip_reply_target", "pending_voice", "pending_voice_clarify",
                ):
                    context.user_data.pop(key, None)
        await _lifecycle_on_interaction(update, user.id)
        # The lifecycle hook can await a re-auth prompt. Access can be revoked,
        # or deletion can finish, while that reply is in flight.
        if not is_user_allowed(user.id) or not db.private_context_is_current(user.id):
            _current_user.set(None)
            db.clear_private_write_context()
            raise ApplicationHandlerStop
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
                else:
                    code = invite_code_in(param)
        else:
            code = invite_code_in(text)

        if code:
            result = _redeem_invite_with_limit(
                code, user.id, full_name=user.full_name, username=user.username
            )
            if result is None:
                # Codes are short (24 bits), so guessing is capped per account.
                # No reply: answering every guess would also burn the bot's
                # send quota.
                raise ApplicationHandlerStop
            ok, msg = result
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
                    "👉 <i>Please sign in to Stud.IP to link your student profile. "
                    "Right after, I'll ask for your semester and a few preferences.</i>"
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
        if _gate_reply_due(user.id):
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


# Failed invite code guesses per Telegram account: at most
# INVITE_MAX_FAILURES in any INVITE_FAILURE_WINDOW seconds. Kept in the
# database, so a restart doesn't reset anyone's count.
INVITE_MAX_FAILURES = 5
INVITE_FAILURE_WINDOW = 3600
INVITE_FAILURES_PATH = "invite_failures.json"
# The "invite only" reply goes to an unknown account at most this often.
GATE_REPLY_INTERVAL = 6 * 3600
_gate_replied: dict[int, float] = {}
_invite_failures_lock = threading.RLock()
_invite_fail_closed: set[int] = set()


def _load_invite_failures(now: float) -> Optional[dict]:
    try:
        raw = db.load(INVITE_FAILURES_PATH) if db.exists(INVITE_FAILURES_PATH) else {}
    except Exception:
        logging.debug("Invite failure record unreadable", exc_info=True)
        return None
    if not isinstance(raw, dict):
        logging.warning("Invite failure record has an invalid shape; denying invite attempts")
        return None
    recent = {}
    for uid, times in raw.items():
        if not isinstance(uid, str) or not uid.isdecimal() or not isinstance(times, list):
            logging.warning("Invite failure record has an invalid entry; denying invite attempts")
            return None
        kept = []
        for value in times:
            if isinstance(value, bool):
                logging.warning("Invite failure record has an invalid timestamp; denying invite attempts")
                return None
            try:
                stamp = float(value)
            except (TypeError, ValueError, OverflowError):
                logging.warning("Invite failure record has an invalid timestamp; denying invite attempts")
                return None
            age = now - stamp
            if not math.isfinite(stamp) or age < 0:
                logging.warning("Invite failure record has an invalid timestamp; denying invite attempts")
                return None
            if age < INVITE_FAILURE_WINDOW:
                kept.append(stamp)
        if kept:
            recent[str(uid)] = kept
    return recent


# What a code looks like once dashes and spaces are gone (create_invite_token:
# "UNI-" and six hex digits), so "uniabc123" and "UNI abc 123" count too while
# ordinary words starting with "uni" don't use up the guess limit.
_INVITE_SHAPE = re.compile(r"UNI[0-9A-F]{6}")


def invite_code_in(text: str) -> Optional[str]:
    """The invite code a message is, written any common way, or None."""
    raw = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    return raw if _INVITE_SHAPE.fullmatch(raw) else None


def _invite_attempts_exhausted(user_id: int) -> bool:
    now = time.time()
    with _invite_failures_lock:
        if user_id in _invite_fail_closed:
            return True
        failures = _load_invite_failures(now)
        return failures is None or len(failures.get(str(user_id), [])) >= INVITE_MAX_FAILURES


def _record_invite_failure(user_id: int) -> None:
    now = time.time()
    with _invite_failures_lock:
        failures = _load_invite_failures(now)
        if failures is None:
            return  # Preserve unreadable state; the gate remains fail-closed.
        failures.setdefault(str(user_id), []).append(now)
        try:
            db.save(INVITE_FAILURES_PATH, failures)
        except Exception:
            logging.warning("Could not record a failed invite code", exc_info=True)
            _invite_fail_closed.add(user_id)
        if len(failures[str(user_id)]) == INVITE_MAX_FAILURES:
            logging.warning("Invite code guessing capped for a Telegram account for %ss", INVITE_FAILURE_WINDOW)


def _redeem_invite_with_limit(code: str, user_id: int, full_name=None, username=None):
    """Serialize failed-attempt checks with redemption so concurrent guesses
    cannot all pass the limit check before any of them is recorded."""
    with _invite_failures_lock:
        if _invite_attempts_exhausted(user_id):
            return None
        result = redeem_invite_token(code, user_id, full_name=full_name, username=username)
        if not result[0]:
            _record_invite_failure(user_id)
        return result


def forget_user_invite_failures(user_id: int) -> None:
    """Delete one account's persisted failed-invite history and stale backup."""
    uid = int(user_id)
    with _invite_failures_lock:
        if db.exists(INVITE_FAILURES_PATH):
            failures = _load_invite_failures(time.time())
            if failures is None:
                raise RuntimeError("Invite failure history is unreadable; cannot verify its deletion")
            failures.pop(str(uid), None)
            if failures:
                db.save(INVITE_FAILURES_PATH, failures)
            else:
                db.delete(INVITE_FAILURES_PATH)
        elif db.has_imported_backup(INVITE_FAILURES_PATH):
            db.delete_imported_backup(INVITE_FAILURES_PATH)

        if db.exists(INVITE_FAILURES_PATH):
            remaining = _load_invite_failures(time.time())
            if remaining is None or str(uid) in remaining:
                raise RuntimeError("Invite failure history remains after deletion")
        if db.has_imported_backup(INVITE_FAILURES_PATH):
            db.delete_imported_backup(INVITE_FAILURES_PATH)
        if db.has_imported_backup(INVITE_FAILURES_PATH):
            raise RuntimeError("Imported invite failure history remains after deletion")
        _invite_fail_closed.discard(uid)


def _reset_invite_failures() -> None:
    try:
        db.delete(INVITE_FAILURES_PATH)
    except Exception:
        logging.debug("No invite failure record to reset", exc_info=True)


def _gate_reply_due(user_id: int) -> bool:
    """Whether an unknown account gets the "invite only" reply now (once
    per GATE_REPLY_INTERVAL, so a flood of messages isn't answered)."""
    now = time.time()
    if len(_gate_replied) > 1000:
        for uid in [u for u, t in _gate_replied.items() if now - t >= GATE_REPLY_INTERVAL]:
            _gate_replied.pop(uid, None)
    if now - _gate_replied.get(user_id, 0.0) < GATE_REPLY_INTERVAL:
        return False
    _gate_replied[user_id] = now
    return True


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


def private_request_is_current(user_id: Optional[int]) -> bool:
    """Check allowlist and request-generation ownership at a private-data boundary.

    Call this inside ``acting_as(user_id)`` or another user-bound private write
    context. It intentionally does not imply that a university login exists.
    """
    if user_id is None or not is_user_allowed(user_id):
        return False
    try:
        return db.private_context_is_current(user_id)
    except Exception:
        return False

# ── single-instance lock ───────────────────────────────────────────────────────
LOCK_FILE = ".bot_instance.lock"
_instance_lock_fd: Optional[int] = None


def acquire_instance_lock():
    """Atomically claim the process lock (prevents duplicate pollers/watchers).

    The lock file is intentionally persistent. The OS lock, not the PID text,
    determines ownership, so a crash or reboot releases it automatically and
    two starters cannot both remove a stale file and believe they own it.
    """
    global _instance_lock_fd
    if _instance_lock_fd is not None:
        return True
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_NBLCK"), 1)  # noqa: B009
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logging.error("Another bot instance is already running.")
        return False
    except BaseException:
        os.close(fd)
        raise
    _instance_lock_fd = fd
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
    except BaseException:
        release_instance_lock()
        raise
    atexit.register(release_instance_lock)
    return True


def release_instance_lock():
    """Release our OS lock; closing the descriptor also releases on crashes."""
    global _instance_lock_fd
    fd, _instance_lock_fd = _instance_lock_fd, None
    if fd is None:
        return
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            os.lseek(fd, 0, os.SEEK_SET)
            getattr(msvcrt, "locking")(fd, getattr(msvcrt, "LK_UNLCK"), 1)  # noqa: B009
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        logging.debug("Could not explicitly release the instance lock", exc_info=True)
    finally:
        os.close(fd)


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
    candidates = [uid for uid in _admin_candidates()
                  if uid != exclude and is_user_allowed(uid) and lifecycle_state(uid) == ACTIVE]
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
    if user_id is None:
        await _complete_proxy_session_authenticated(None)
        return
    with acting_as(user_id):
        if not db.private_context_is_current(user_id):
            return
        await _complete_proxy_session_authenticated(user_id)


async def _complete_proxy_session_authenticated(user_id: Optional[int] = None) -> None:
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
    if user_id is not None and not db.private_context_is_current(user_id):
        return
    if user_id is not None:
        # a completed interactive sign-in ends a paused (expired) state even
        # when the Stud.IP check below can't confirm it yet: paused students
        # are never checked in the background, so nothing else would. It
        # also resumes a stopped student (after /stop or deleting their
        # data, where they are told to sign in again): signing in is a
        # clear wish to use the bot again.
        _renewal_failures.pop(user_id, None)
        if lifecycle_state(user_id) in (NEEDS_REAUTH, INACTIVE) and db.set_lifecycle(user_id, ACTIVE, "signed in again"):
            logging.info("User %s signed in again: active", user_id)

    # Signed in through StuMS: sign the same session in to Stud.IP as well.
    try:
        if not await session.is_html_logged_in(use_cache=False):
            if await session.heal_session():
                logging.info("✅ Stud.IP session established during login for user %s", user_id)
    except Exception as exc:
        logging.info("Stud.IP SSO handoff after proxy login failed for user %s: %s", user_id, exc)
    if user_id is not None and not db.private_context_is_current(user_id):
        return

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
        if user_id is not None and not db.private_context_is_current(user_id):
            return
    elif is_new_session or state._studip_user_profile is None:
        try:
            profile = await get_studip_user_profile(user_id=user_id, session=session)
        except Exception as exc:
            logging.debug("Profile sync after login for user %s: %s", user_id, exc)
        if user_id is not None and not db.private_context_is_current(user_id):
            return

    if profile and user_id is not None:
        studip_user = profile.get("username")
        if studip_user:
            # only the link to the Stud.IP account: not a sign of activity (upsert_user touches last_active_at)
            db.update_user_studip(user_id, studip_user)

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
    if user_id is not None and not db.private_context_is_current(user_id):
        return

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
        if user_id is not None and state.on_signed_in is not None:
            try:
                await state.on_signed_in(state._menu_app.bot, user_id, session)
            except Exception:
                logging.warning("Post sign-in step failed for %s", user_id, exc_info=True)


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
            # start() can time out while its supervisor is still waiting for
            # cloudflared to publish a URL. Keep that live supervisor attached
            # to runtime state so it can recover and be stopped at shutdown.
            if state.quick_tunnel._task is None:
                state.quick_tunnel = None
    elif mode == "quick":
        state.quick_tunnel = QuickTunnel(server.port, on_url_change=_on_tunnel_url)
        try:
            await state.quick_tunnel.start()
        except Exception as exc:
            logging.error("❌ Cloudflare quick tunnel failed: %s", exc)
            # A URL timeout does not stop QuickTunnel's supervisor. Retain the
            # object when that task is alive so a late URL can still be used.
            if state.quick_tunnel._task is None:
                state.quick_tunnel = None

    await server.start()
    logging.info("🔐 Login page available at %s", server.base_url())
    await _clear_menu_button(app)


async def _send_login_prompt(reply_target, user_id: int) -> None:
    """Send the Stud.IP Mini App login button to a message target."""
    with acting_as(user_id):
        if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                or lifecycle_state(user_id) == INACTIVE):
            return
        server = _get_login_server()
        await server.start()
        # Starting the server may yield while the account is being deleted.
        # Do not create a one-time login link or send it after that point.
        if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                or lifecycle_state(user_id) == INACTIVE):
            return

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
            if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                    or lifecycle_state(user_id) == INACTIVE):
                return
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
        if state.login_server is not None:
            # Stop pending post-login hooks before the logout's first await;
            # otherwise a hook could restore credentials after deletion.
            state.login_server.revoke_user(user_id)
        if sess is not None:
            await sess.logout()
        # Older versions stored each student's cookies in this file. The
        # session may never have been instantiated in this process, so
        # StudIPSession.logout() cannot be relied on to remove it.
        for path in (f"studip_cookies_{int(user_id)}.json",
                     f"studip_cookies_{int(user_id)}.json.tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
                if os.path.exists(path):
                    raise OSError("file still exists after removal")
            except OSError as exc:
                logging.error("Could not remove legacy session file %s during logout: %s", path, exc)
                raise RuntimeError(f"Could not clear the saved Stud.IP session ({path})") from exc
        db.get_user_scope(user_id).delete_credentials()
        # Logout must also remove legacy OAuth token files when OAuth use is
        # currently disabled or the user's session object was not in memory.
        # clear_tokens invalidates in-flight refreshes even when no token is active.
        studip_oauth.clear_tokens(user_id=user_id)
        db.update_user_studip(user_id, "")
        state.user_courses_map.pop(user_id, None)
        if state.global_session is sess:
            # keep the watcher running on another signed-in admin, if any
            state.global_session = pick_watcher_session(exclude=user_id)
            state._studip_api_user_id = None
            state._studip_user_profile = None
            state._last_notified_studip_cookie = None
            # the shared course list was the signed-out admin's own: the next
            # watcher builds its own instead of following theirs
            state.courses_map = {}
            state._courses_map_built_with_prev = None
            state._courses_map_refreshed_at = 0.0
        if sess is not None and sess is not state.global_session:
            await sess.close()  # its HTTP connections would otherwise leak
        return

    state._studip_api_user_id = None
    state._studip_user_profile = None
    state._last_notified_studip_cookie = None
    if state.login_server is not None:
        state.login_server.revoke_all()
    if state.global_session is not None:
        await state.global_session.logout()
    studip_oauth.clear_tokens()


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
