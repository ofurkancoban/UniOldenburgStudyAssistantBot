"""Telegram message helpers: broadcast, keyboards, typing and loading indicators.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import io
import logging
import os
import tempfile
import uuid
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from studip_utils import md_escape as _md, safe_filename as _safe_filename

from unibot.config import ALLOWED_USER_IDS


nav_names = {}  # user_id -> [name1, name2, ...]  (for breadcrumb)


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
    await message.reply_document(
        document=io.BytesIO(content),
        filename=safe_name,
        caption=caption,
        reply_markup=doc_markup,
    )


# ── helpers ────────────────────────────────────────────────────────────────────
# ── broadcast helper ──────────────────────────────────────────────────────────
async def broadcast(bot, text, parse_mode="HTML", disable_notification=True, reply_markup=None):
    """Send a message to all allowed users with optional reply markup."""
    for uid in ALLOWED_USER_IDS:
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                reply_markup=reply_markup
            )
            await asyncio.sleep(0.3)  # Telegram rate limit safety
        except Exception as e:
            logging.warning("Failed to send broadcast to %s: %s", uid, e)


async def _show_typing(sender, action=ChatAction.TYPING):
    """Show one of Telegram's native chat-action animations in the relevant
    chat before a slow operation — "typing..." by default (a network fetch,
    a page scrape, ...), or a more specific one like ChatAction.UPLOAD_DOCUMENT
    where that's what's actually about to happen (a file/ZIP is being sent).
    Works with either a Message or a CallbackQuery — both expose get_bot()
    and a way to reach the chat (.chat / .message.chat) — so callers don't
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
    BEFORE the caller sends/edits that message with the real result —
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
                # Message deleted/unchanged/whatever — stop rather than spam retries.
                break

    return stop_event, asyncio.create_task(_loop())


async def _stop_loading_animation(stop_event: asyncio.Event, task: "asyncio.Task"):
    stop_event.set()
    try:
        await task
    except Exception:
        logging.debug("Loading animation task ended with an error", exc_info=True)


async def _reply_or_edit(sender, text, reply_markup=None, parse_mode=None):
    """Send text as a new message, or edit sender's message in place when
    sender is a CallbackQuery (has edit_message_text). Stepping through a
    semester picker and its resulting list this way reuses one message
    instead of stacking new ones — stacked messages made it easy to mis-tap a
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


def _normalize_button_text(s: str) -> str:
    # Clean leading/trailing spaces and repeated whitespace
    return " ".join((s or "").strip().split())


async def delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete any free text that isn't a valid keyboard command."""
    try:
        if not update.message or not update.message.text:
            return
        text = _normalize_button_text(update.message.text)
        if text.startswith(("📁", "⬇️", "📚", "📅", "✅", "🍽️", "📘", "📊", "📝", "🔄", "⚙️", "🔁", "ℹ️", "🎯")):
            return
        await update.message.delete()
    except TelegramError:
        pass


def get_main_keyboard():
    """Main persistent reply keyboard with 3 rows of 3 buttons (9 buttons)"""
    return ReplyKeyboardMarkup(
        [
            ["📁 Files", "📅 Schedule", "✅ Tasks"],
            ["🍽️ Mensa", "📘 Study Plan", "📊 Progress"],
            ["📝 Exams", "🔄 Check", "⚙️ Status"]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True
    )


async def _download_telegram_file(bot, file_id: str) -> Optional[bytes]:
    """Download a Telegram-hosted file (photo) to a transient temp path, read
    it into memory, then remove the temp file — same disposable-temp-file
    convention as _pcm_to_ogg_voice's PCM intermediate. Returns None on any
    failure (expired file_id, network error) so callers degrade gracefully."""
    path = os.path.join(tempfile.gettempdir(), f"wardrobe_{uuid.uuid4().hex}.jpg")
    try:
        file = await bot.get_file(file_id)
        await file.download_to_drive(path)
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        logging.warning("_download_telegram_file failed: %s", e)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _split_message(text: str, limit: int = 3800) -> list:
    """Telegram caps messages at 4096 chars; split on line boundaries."""
    chunks, chunk = [], ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit and chunk:
            chunks.append(chunk.rstrip("\n"))
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        chunks.append(chunk.rstrip("\n"))
    return chunks
