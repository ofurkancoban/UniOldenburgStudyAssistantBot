"""Notification layer of the course watchers: who gets an alert (the
course's participants), the alert texts and buttons, and sending them."""

import asyncio
import html
import io
import logging
import os
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from studip_utils import safe_filename
from unibot import course_registry, db, state
from unibot.auth import ACTIVE, get_user_session, is_user_allowed, lifecycle_state
from unibot.config import PRIMARY_ADMIN_ID
from unibot.formatting import (
    TELEGRAM_HTML_LIMIT,
    _short_id,
    _telegram_length,
    truncate_html,
)
from unibot.monitoring_fetch import FORUM_OMITTED
from unibot.storage import has_course_access, link_cache, persistent_callback_cache
from unibot.studip_api import _normalize_url, file_date_sort_key, read_response_limited
from unibot.telegram_ui import (  # noqa: F401 - broadcast is re-exported for callers
    broadcast,
    markup_for,
)

# short_id -> { course, thread, body }; persisted so the per-user AI task
# suggestion button can find its source forum post after a restart.
task_suggestion_cache = persistent_callback_cache("task_suggestions.json")
# short_id -> { course, subject, body } - raw announcement, converted to a
# task on demand when "📝 Add as Task" is tapped
announcement_task_cache = persistent_callback_cache("announcement_tasks.json")


def course_members(cid: Optional[str]):
    """Participants of a watched course, or None when unknown (then broadcast
    falls back to its old recipient rules)."""
    if cid is None:
        return None
    return (getattr(state, "course_members", None) or {}).get(cid)


def _file_alert(it: dict, sid: str) -> tuple[str, InlineKeyboardMarkup]:
    """Notification text and buttons (download, plus an audio summary for
    PDF/PPTX/HTML) for one new or updated file."""
    title = "🔥 🆕 <b>NEW FILE ADDED</b> 🔥" if it["type"] == "new" else "🔥 ♻️ <b>FILE UPDATED</b> 🔥"
    text = (
        f"{title}\n"
        "                 \n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📚 <b>Course:</b> {html.escape(str(it['course']))}\n"
        f"📄 <b>File:</b> {html.escape(str(it['name']))}\n"
        f"📅 <b>Date:</b> {html.escape(str(it['modified']))}\n"
        f"💾 <b>Size:</b> {html.escape(str(it['size']))}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    btn_rows = [[InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")]]
    if os.path.splitext(it.get("name") or "")[1].lower() in (".pdf", ".pptx", ".html"):
        btn_rows.append([InlineKeyboardButton("🎧 1-Min Audio Summary", callback_data=f"file_audio_sum|{sid}")])
    return text, InlineKeyboardMarkup(btn_rows)


FULL_FILE_MAX_BYTES = 20 * 1024 * 1024


def _title_line(icon: str, course, what) -> str:
    """The one-line "title only" version of an alert."""
    return f"{icon} <b>{html.escape(str(course))}</b>: {html.escape(str(what))}"


def file_title_text(it: dict) -> str:
    label = "new file" if it.get("type") == "new" else "updated file"
    return _title_line("📄", it.get("course", ""), f"{it.get('name', '')} ({label})")


def announcement_title_text(ann: dict) -> str:
    return _title_line("📢", ann.get("course", ""), ann.get("subject", ""))


def forum_title_text(p: dict) -> str:
    return _title_line("💬", p.get("course", ""), f"{p.get('thread', '')} ({p.get('author', '')})")


def _full_file_sender(it: dict, text: str, keyboard, downloader_uid: Optional[int]):
    """send_full for fan_out: the file itself with the alert as its caption.
    Downloaded once with a member's session; later followers get the
    Telegram file_id. Returns False (fall back to the card) when the file
    has no URL, is too big or cannot be fetched."""
    shared: dict = {}

    async def send(bot, uid) -> bool:
        if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                or lifecycle_state(uid) != ACTIVE or not has_course_access(uid, it.get("cid"))):
            return False
        if shared.get("failed") or not it.get("url"):
            return False
        document = shared.get("file_id")
        if document is None:
            try:
                if not is_user_allowed(downloader_uid) or lifecycle_state(downloader_uid) != ACTIVE:
                    raise RuntimeError("designated downloader is no longer active")
                session = get_user_session(downloader_uid) or state.global_session
                if session is None:
                    raise RuntimeError("no session")
                file_url = _normalize_url(it["url"])
                if not file_url:
                    raise RuntimeError("file URL was not a trusted university URL")
                async with await session.get(file_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content = await read_response_limited(resp, FULL_FILE_MAX_BYTES)
                if len(content) > FULL_FILE_MAX_BYTES:
                    raise RuntimeError(f"{len(content)} bytes")
                if not is_user_allowed(downloader_uid) or lifecycle_state(downloader_uid) != ACTIVE:
                    raise RuntimeError("designated downloader was blocked during the download")
            except Exception as e:
                logging.info("Full file alert for %s falls back to the card: %s", it.get("name"), e)
                shared["failed"] = True
                return False
            document = io.BytesIO(content)
        if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                or lifecycle_state(uid) != ACTIVE or not has_course_access(uid, it.get("cid"))):
            return False
        try:
            msg = await bot.send_document(
                chat_id=uid, document=document, filename=safe_filename(str(it.get("name") or "file")),
                caption=truncate_html(text, 1024), parse_mode="HTML", reply_markup=markup_for(uid, keyboard),
            )
        except Exception as e:
            logging.info("Full file alert for %s failed for user %s; falling back to its card: %s",
                         it.get("name"), uid, e)
            if isinstance(document, io.BytesIO):
                document.seek(0)
            return False
        file_id = getattr(getattr(msg, "document", None), "file_id", None)
        if file_id:
            shared["file_id"] = file_id
            if isinstance(document, io.BytesIO):
                document.close()  # later followers get the file_id, not the bytes
        elif isinstance(document, io.BytesIO):
            # A successful API call should include the uploaded document, but
            # keep the bytes reusable if a Telegram response omitted its id.
            document.seek(0)
        return True

    return send


async def _send_file_alerts(bot, events: list) -> None:
    """Send file notifications in chronological order, each with download
    buttons, to every follower in their own format."""
    for it in sorted(events, key=lambda x: (file_date_sort_key(x["dt"]), x["course"], x["name"])):
        try:
            sid = _short_id()
            downloader = next(iter(sorted(course_members(it.get("cid")) or ())), None) \
                or PRIMARY_ADMIN_ID
            link_cache[sid] = {
                "cid": it["cid"],
                "name": it["name"],
                "course": it["course"],
                "url": it.get("url"),
                "ts": time.time(),
                "shared": True,
            }
            text, keyboard = _file_alert(it, sid)
            await course_registry.fan_out(
                bot, it.get("cid"), "files", text, file_title_text(it), reply_markup=keyboard,
                members=course_members(it.get("cid")),
                send_full=_full_file_sender(it, text, keyboard, downloader),
            )
            await asyncio.sleep(0.3)  # Rate limiting
        except Exception as e:
            logging.error("Error sending notification for file %s: %s", it.get('name'), e)


def _forum_alert_text(p: dict, limit: int = TELEGRAM_HTML_LIMIT) -> str:
    """The forum alert, within Telegram's limit: a long conversation loses
    its oldest messages first (never the newest one), and whatever is still
    too long is cut without breaking the HTML."""
    head = (
        "🔥 💬 <b>NEW FORUM POST</b> 🔥\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🏫 <b>Course:</b> {html.escape(p.get('course', ''))}\n"
        f"📂 <b>Thread:</b> {html.escape(p.get('thread', ''))}\n"
        f"👤 <b>By:</b> {html.escape(p.get('author', ''))}\n"
        f"🕒 <b>Date:</b> {html.escape(p.get('date', '-'))}\n"
        "━━━━━━━━━━━━━━━━━\n"
    )
    tail = "\n━━━━━━━━━━━━━━━━━"
    body = p["body"]
    # a conversation is its heading, then one block per message (oldest
    # first), "\n\n" apart; a single message is one block
    heading, *rest = body.split("\n\n")
    entries = [block for block in rest if block != FORUM_OMITTED]
    omitted = len(entries) != len(rest)
    while (_telegram_length(head) + _telegram_length(body) + _telegram_length(tail) > limit
           and len(entries) > 1):
        entries.pop(0)
        omitted = True
        body = "\n\n".join([heading] + ([FORUM_OMITTED] if omitted else []) + entries)
    body_limit = limit - _telegram_length(head) - _telegram_length(tail)
    return head + truncate_html(body, body_limit) + tail


def _forum_alert_markup(p: dict) -> InlineKeyboardMarkup:
    """Forward button and a user-initiated task-suggestion button.

    Forum text is sent to the AI only after an individual recipient taps the
    button and their current personal-AI permission is checked.
    """
    buttons = [[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]]
    if p.get("last_message_plain"):
        sid = _short_id()
        task_suggestion_cache[sid] = {
            "cid": p.get("cid"),
            "course": p.get("course"),
            "thread": p.get("thread"),
            "body": p.get("last_message_plain"),
        }
        buttons.append([InlineKeyboardButton("📝 Suggest task", callback_data=f"tasksug_add|{sid}")])
    return InlineKeyboardMarkup(buttons)


def _format_exam_dates_message(header: str, exams: list[dict], show_status: bool = True) -> list[str]:
    """`show_status` controls the per-exam registration-status line (Registered /
    Not registered / Passed / Failed). Off for the full-curriculum list, where
    most entries are irrelevant to the student and that line is just noise."""
    from unibot.exams import _exam_registration_status_line, _registration_period_line
    lines = [header, "━━━━━━━━━━━━━━━━━"]
    for e in exams:
        title = e["title"]
        if e.get("group_label"):
            title += f" ({e['group_label']})"
        lines.append(f"📘 <b>{html.escape(title)}</b>")
        lines.append(f"📅 {html.escape(str(e['exam_date_display']))}")
        if e.get("room"):
            lines.append(f"📍 {html.escape(str(e['room']))}")
        period_line = _registration_period_line(e)
        if period_line:
            lines.append(period_line)
        if show_status:
            lines.append(_exam_registration_status_line(e))
        lines.append("━━━━━━━━━━━━━━━━━")
    return lines
