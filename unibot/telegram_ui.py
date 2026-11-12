"""Telegram message helpers: broadcast, keyboards, typing and loading indicators.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import inspect
import io
import logging
import os
import tempfile
import uuid
from collections.abc import Mapping
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import BaseRateLimiter

from studip_utils import aread_bytes
from studip_utils import md_escape as _md
from unibot.formatting import split_html
from studip_utils import safe_filename as _safe_filename
from unibot import db
from unibot.config import ADMIN_USER_IDS, ALLOWED_USER_IDS
from unibot.storage import has_course_access

nav_names: dict[int, list[str]] = {}  # user_id -> [name1, name2, ...]  (for breadcrumb)


def user_data(context) -> dict:
    """context.user_data as a plain dict. PTB types it Optional because
    updates without a user (channel posts) have none; handlers here always
    run for a user, so the empty fallback is never actually written to."""
    data = context.user_data
    return data if data is not None else {}


def _breadcrumb(user_id: int) -> str:
    """Format the navigation breadcrumb for the user."""
    parts = nav_names.get(user_id, [])
    if not parts:
        return "📂 *Root*"
    if len(parts) == 1:
        return f"📂 *{_md(parts[0])}*"
    else:
        chain = " › ".join(_md(p) for p in parts)
        return f"📂 *{chain}*"


async def _send_file_bytes(message, content: bytes, fname: str, sid: str, caption: Optional[str] = None) -> None:
    """Send a downloaded Stud.IP file straight from memory. No temp file:
    nothing is ever written to a path derived from the server-side name,
    and nothing is left behind if the upload fails."""
    safe_name = _safe_filename(fname)
    doc_markup = None
    if os.path.splitext(safe_name)[1].lower() in (".pdf", ".pptx", ".html"):
        doc_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎧 1-Min Audio Summary", callback_data=f"file_audio_sum|{sid}")]])
    with io.BytesIO(content) as buffer:  # closed as soon as the upload is done
        await message.reply_document(
            document=buffer,
            filename=safe_name,
            caption=caption,
            reply_markup=doc_markup,
        )


# ── Telegram API safety layer ─────────────────────────────────────────────────
BLOCKED_MARKERS = ("blocked by the user", "user is deactivated")


def mark_blocked_chat(chat_id, error: Exception) -> bool:
    """A student who blocked the bot (or deleted their account) becomes
    INACTIVE, so no scan or alert runs for them; /start brings them back.
    True when a registered student was moved."""
    if not isinstance(chat_id, int) or chat_id <= 0:
        return False  # groups and channels are not students
    if not any(marker in str(error).lower() for marker in BLOCKED_MARKERS):
        return False
    try:
        if db.get_user(chat_id) is None:
            return False
        if db.set_lifecycle(chat_id, "inactive", "blocked the bot"):
            logging.info("User %s blocked the bot: inactive, background work stopped", chat_id)
            return True
    except Exception:
        logging.warning("Could not mark user %s inactive after a block", chat_id, exc_info=True)
    return False


class TelegramSafetyLimiter(BaseRateLimiter):
    """Every Bot API call of the bot goes through here (ApplicationBuilder
    .rate_limiter): calls are spaced at least MIN_INTERVAL apart (Telegram
    allows about 30 messages a second per bot), and a 429 Flood-Wait
    (RetryAfter) is waited out and retried instead of failing the send. A
    "blocked by the user" answer makes that student INACTIVE."""

    MIN_INTERVAL = 1 / 25
    MAX_RETRIES = 2

    def __init__(self):
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def _wait_turn(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = loop.time() + self.MIN_INTERVAL

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        for attempt in range(self.MAX_RETRIES + 1):
            await self._wait_turn()
            try:
                return await callback(*args, **kwargs)
            except Forbidden as e:
                # PTB passes the call's parameters as a dict (files too: they
                # become multipart only later); anything else just isn't read
                chat_id = data.get("chat_id") if isinstance(data, Mapping) else None
                try:
                    chat_id = int(chat_id) if chat_id is not None else None
                except (TypeError, ValueError):
                    chat_id = None
                mark_blocked_chat(chat_id, e)
                raise
            except RetryAfter as e:
                if attempt == self.MAX_RETRIES:
                    raise
                wait = float(e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds")
                             else e.retry_after)
                logging.warning("Telegram flood wait on %s: retrying in %.1f s", endpoint, wait)
                await asyncio.sleep(wait + 0.5)
        raise RuntimeError("unreachable")


# ── helpers ────────────────────────────────────────────────────────────────────
# Buttons that post into the administrator's WhatsApp group: the plain
# "Forward to WA" and the one on exam registration notices
WA_FORWARD_CALLBACK_PREFIX = ("forward_wa", "examopen_wa")


def is_admin_chat(uid: Optional[int]) -> bool:
    """Whether the chat is an administrator's (same rule as auth.is_user_admin)."""
    if uid is None:
        return False
    if uid in ADMIN_USER_IDS:
        return True
    try:
        return db.is_user_admin_db(uid)
    except Exception:
        logging.debug("Admin lookup failed", exc_info=True)
        return False


def markup_for(uid: Optional[int], markup):
    """The reply markup as this chat may see it: the "Forward to WA" button
    posts into the administrator's WhatsApp group, so only admins get it."""
    if markup is None or is_admin_chat(uid):
        return markup
    rows = getattr(markup, "inline_keyboard", None)
    if rows is None:
        return markup
    kept = [[b for b in row if not str(getattr(b, "callback_data", "") or "").startswith(WA_FORWARD_CALLBACK_PREFIX)]
            for row in rows]
    kept = [row for row in kept if row]
    return InlineKeyboardMarkup(kept) if kept else None


# ── broadcast helper ──────────────────────────────────────────────────────────
def broadcast_targets(cid: Optional[str] = None, members=None) -> set:
    """Who a broadcast reaches. Inactive students are never included.

    Without `cid` and `members`: every allowed and active user (an admin
    announcement). `members` without `cid` is exactly the audience (the
    admins, one student). With `cid`: only students who take that course
    verified by their course list or course registry. Explicit members are
    candidates, never a substitute for authorization, including admins."""
    if cid is None:
        if members is not None:
            targets = set(members)
        else:
            targets = set(ALLOWED_USER_IDS)
            try:
                targets |= {u["telegram_id"] for u in db.get_all_users() if u.get("is_active", 1)}
            except Exception:
                logging.debug("User list unavailable", exc_info=True)
        return {uid for uid in targets if not _is_inactive(uid)}

    targets = {uid for uid in members if _takes_course(uid, cid)} if members is not None else set()
    try:
        for u in db.get_all_users():
            uid = u["telegram_id"]
            if not u.get("is_active", 1) or uid in targets:
                continue
            if _takes_course(uid, cid):
                targets.add(uid)
    except Exception:
        logging.debug("Ignored a non-critical error", exc_info=True)
    return {uid for uid in targets if not _is_inactive(uid)}


def _takes_course(uid: int, cid: str) -> bool:
    """Whether trusted enrollment data permits this student's course access."""
    return has_course_access(uid, cid)


def _is_inactive(uid: int) -> bool:
    """A student who used /stop, deleted their data or blocked the bot gets
    nothing until they come back with /start. An account an administrator
    blocked is out too, at once (not only when the next scan rebuilds the
    course members)."""
    try:
        if db.get_lifecycle(uid)["state"] == "inactive":
            return True
        user = db.get_user(uid)
        return user is not None and not user.get("is_active")
    except Exception:
        logging.debug("Lifecycle lookup failed", exc_info=True)
        # When the database cannot confirm that a user is active, do not
        # include them in a broadcast. This keeps revoked or stopped users
        # from receiving private alerts during a database outage.
        return True


async def broadcast(
    bot,
    text,
    parse_mode="HTML",
    disable_notification=True,
    reply_markup=None,
    cid: Optional[str] = None,
    members=None,
):
    """Send a message with optional reply markup; return delivery counts.

    HTML announcements may exceed Telegram's per-message limit after escaping
    user text. Split them into valid Telegram messages and count a recipient as
    delivered only after every part succeeds.
    """
    sent_ids = []
    failed_ids = []
    messages = split_html(text) if parse_mode == "HTML" else [text]
    for uid in broadcast_targets(cid, members):
        try:
            for index, message in enumerate(messages):
                await bot.send_message(
                    chat_id=uid,
                    text=message,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                    reply_markup=(markup_for(uid, reply_markup)
                                  if index == len(messages) - 1 else None),
                )
                if index < len(messages) - 1:
                    await asyncio.sleep(0.3)
            sent_ids.append(uid)
            await asyncio.sleep(0.3)  # Telegram rate limit safety
        except Exception as e:
            failed_ids.append(uid)
            logging.warning("Failed to send broadcast to %s: %s", uid, e)
    return {"sent": sent_ids, "failed": failed_ids}


async def _show_typing(sender, action=ChatAction.TYPING):
    """Show one of Telegram's native chat-action animations in the relevant
    chat before a slow operation - "typing..." by default (a network fetch,
    a page scrape, ...), or a more specific one like ChatAction.UPLOAD_DOCUMENT
    where that's what's actually about to happen (a file/ZIP is being sent).
    Works with either a Message or a CallbackQuery - both expose get_bot()
    and a way to reach the chat (.chat / .message.chat) - so callers don't
    need to know which one they were handed. Best-effort: never raises,
    since this is a cosmetic nicety and must not block the actual
    operation."""
    try:
        chat = sender.chat if hasattr(sender, "chat") else sender.message.chat
        await sender.get_bot().send_chat_action(chat_id=chat.id, action=action)
    except (TelegramError, AttributeError):
        pass


LOADING_ANIMATION_FRAMES = ("⏳", "⌛")


def _start_loading_animation(status_message, base_text: str, frames=LOADING_ANIMATION_FRAMES, interval: float = 1.5):
    """Start a background task that cycles an emoji prefix on
    `status_message`'s text (a real telegram.Message, e.g. whatever
    `await ...reply_text(...)` or `await query.edit_message_text(...)`
    returned) every `interval` seconds, so a "please wait" bubble visibly
    animates instead of sitting static.

    Returns (stop_event, task). MUST be paired with a matching
    `await _stop_loading_animation(stop_event, task)` in a `finally` block
    BEFORE the caller sends/edits that message with the real result -
    otherwise a late animation tick can overwrite real content with a
    stale loading frame. Keep the wrapped span to just the actual slow
    call(s); don't wrap unrelated code after the result is already decided.
    """
    stop_event = asyncio.Event()

    async def _loop():
        i = 0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event was set while waiting
            except asyncio.TimeoutError:
                pass
            i += 1
            try:
                await status_message.edit_text(f"{frames[i % len(frames)]} {base_text}")
            except TelegramError:
                # Message deleted/unchanged/whatever - stop rather than spam retries.
                break

    return stop_event, asyncio.create_task(_loop())


async def _stop_loading_animation(stop_event: asyncio.Event, task: "asyncio.Task"):
    stop_event.set()
    try:
        await task
    except Exception:
        logging.debug("Loading animation task ended with an error", exc_info=True)


async def _delete_status_message(status_message) -> None:
    """Best-effort cleanup of a temporary 'Loading...' message."""
    delete = getattr(status_message, "delete", None)
    if not callable(delete):
        return
    try:
        res = delete()
        if inspect.isawaitable(res):
            await res
    except Exception:
        logging.debug("Status message not deleted", exc_info=True)


async def _reply_or_edit(sender, text, reply_markup=None, parse_mode=None):
    """Send text as a new message, or edit sender's message in place when
    sender is a CallbackQuery (has edit_message_text). Stepping through a
    semester picker and its resulting list this way reuses one message
    instead of stacking new ones - stacked messages made it easy to mis-tap a
    stale button left over from an earlier list (e.g. picking a new semester
    while the old course list's "Enroll" buttons were still live underneath,
    accidentally enrolling from the old list instead).

    Returns the resulting telegram.Message (useful e.g. to animate it via
    _start_loading_animation while the next step is slow)."""
    kwargs = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if hasattr(sender, "edit_message_text"):
        try:
            return await sender.edit_message_text(text, **kwargs)
        except Exception as e:
            logging.debug("_reply_or_edit: edit failed, falling back to reply: %s", e)
    return await sender.reply_text(text, **kwargs)


MAIN_KEYBOARD_ROWS = (
    ("📁 Files", "📅 Schedule", "✅ Tasks"),
    ("🍽️ Mensa", "📘 Study Plan", "📊 Progress"),
    ("📝 Exams", "🔄 Check", "⚙️ Status"),
)
MAIN_KEYBOARD_LABELS = frozenset(label for row in MAIN_KEYBOARD_ROWS for label in row)


def get_main_keyboard():
    """Main persistent reply keyboard with 3 rows of 3 buttons (9 buttons)"""
    return ReplyKeyboardMarkup(
        [list(row) for row in MAIN_KEYBOARD_ROWS],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True
    )


MAX_TELEGRAM_IMAGE_BYTES = 10 * 1024 * 1024


async def _download_telegram_file(bot, file_id: str) -> Optional[bytes]:
    """Download a Telegram-hosted file (photo) to a transient temp path, read
    it into memory, then remove the temp file - same disposable-temp-file
    convention as _pcm_to_ogg_voice's PCM intermediate. Returns None on any
    failure (expired file_id, network error) so callers degrade gracefully."""
    path = os.path.join(tempfile.gettempdir(), f"wardrobe_{uuid.uuid4().hex}.jpg")
    try:
        file = await bot.get_file(file_id)
        if file.file_size is not None and file.file_size > MAX_TELEGRAM_IMAGE_BYTES:
            logging.warning("Telegram image exceeds the %s byte limit", MAX_TELEGRAM_IMAGE_BYTES)
            return None
        await file.download_to_drive(path)
        if os.path.getsize(path) > MAX_TELEGRAM_IMAGE_BYTES:
            logging.warning("Downloaded Telegram image exceeds the %s byte limit", MAX_TELEGRAM_IMAGE_BYTES)
            return None
        return await aread_bytes(path)
    except Exception as e:
        logging.warning("_download_telegram_file failed: %s", e)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _split_message(text: str, limit: int = 3800) -> list:
    """Telegram caps messages at 4096 chars; cut HTML text into parts that are
    each valid HTML (see formatting.split_html)."""
    return split_html(text, limit)
