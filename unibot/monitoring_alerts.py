"""Notification layer of the course watchers: who gets an alert (the
course's participants), the alert texts and buttons, and sending them."""

import asyncio
import html
import logging
import os
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from unibot import state
from unibot.config import ALLOWED_USER_IDS
from unibot.formatting import (
    _short_id,
)
from unibot.storage import link_cache
from unibot.telegram_ui import (
    broadcast,
)

task_suggestion_cache = {}  # short_id -> { text, due_time (ISO or None), course }


announcement_task_cache = {}  # short_id -> { course, subject, body } - raw announcement, converted to a task on demand when "📝 Add as Task" is tapped


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


async def _send_file_alerts(bot, events: list) -> None:
    """Send file notifications in chronological order, each with download buttons."""
    for it in sorted(events, key=lambda x: (x["dt"], x["course"], x["name"])):
        try:
            sid = _short_id()
            link_cache[sid] = {
                "cid": it["cid"],
                "name": it["name"],
                "course": it["course"],
                "url": it.get("url"),
                "ts": time.time(),
                # a participant's session can download it (an admin's may not be enrolled)
                "user_id": next(iter(sorted(course_members(it.get("cid")) or ())), None)
                or next(iter(ALLOWED_USER_IDS), None),
            }
            text, keyboard = _file_alert(it, sid)
            await broadcast(bot, text, parse_mode="HTML", reply_markup=keyboard, cid=it.get("cid"),
                            members=course_members(it.get("cid")))
            await asyncio.sleep(0.3)  # Rate limiting
        except Exception as e:
            logging.error("Error sending notification for file %s: %s", it.get('name'), e)


def _forum_alert_text(p: dict) -> str:
    return (
        "🔥 💬 <b>NEW FORUM POST</b> 🔥\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🏫 <b>Course:</b> {html.escape(p.get('course', ''))}\n"
        f"📂 <b>Thread:</b> {html.escape(p.get('thread', ''))}\n"
        f"👤 <b>By:</b> {html.escape(p.get('author', ''))}\n"
        f"🕒 <b>Date:</b> {html.escape(p.get('date', '-'))}\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"{p['body']}\n"
        "━━━━━━━━━━━━━━━━━"
    )


async def _forum_alert_markup(p: dict) -> InlineKeyboardMarkup:
    """Forward button, plus an "Add as task" button when the AI finds a task."""
    from unibot.tasks import extract_task_suggestion
    buttons = [[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]]
    if p.get("last_message_plain"):
        try:
            suggestion = await extract_task_suggestion(p.get('course', ''), p.get('thread'), p['last_message_plain'])
        except Exception as e:
            logging.warning("Task suggestion extraction failed for forum post: %s", e)
            suggestion = None
        if suggestion:
            sid = _short_id()
            task_suggestion_cache[sid] = {**suggestion, "course": p.get('course')}
            buttons.append([InlineKeyboardButton(f"📝 Add as task: {suggestion['text'][:40]}", callback_data=f"tasksug_add|{sid}")])
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
        lines.append(f"📘 <b>{title}</b>")
        lines.append(f"📅 {e['exam_date_display']}")
        if e.get("room"):
            lines.append(f"📍 {e['room']}")
        period_line = _registration_period_line(e)
        if period_line:
            lines.append(period_line)
        if show_status:
            lines.append(_exam_registration_status_line(e))
        lines.append("━━━━━━━━━━━━━━━━━")
    return lines
