"""StuMS exams: registration, dates, grades, documents, tuition and exam dates in course files.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from exam_reminder import (
    STUMS_DOCUMENTS,
    base_module_code,
    compute_transcript_summary,
    download_stums_document,
    filter_exams_by_module_codes,
    get_all_exam_dates,
    get_grades,
    get_open_exam_registrations,
    get_registered_exams,
    get_tuition_status,
    is_passed_exam,
    load_exam_cache,
    save_exam_cache,
    submit_exam_action,
)
from studip_utils import TZ_BERLIN, berlin_now, html_escape as _h

from unibot import db
from unibot import state
from unibot import course_registry
from unibot.monitoring_alerts import course_members
from unibot.storage import (
    load_default_semester,
    personal_record,
    recall_user_callback,
    remember_user_callback,
    save_personal_record,
    single_flight,
    student_courses,
)
from unibot.telegram_ui import _show_typing, _start_loading_animation, _stop_loading_animation, markup_for
from unibot.auth import get_user_session, is_user_admin, is_user_allowed, personal_session
from unibot.studip_api import BASE_URL, _parse_file_date_v2, list_courses
from unibot.ai import _call_openrouter, _extract_json_object
from unibot.documents import extract_text_from_file_async
from unibot.whatsapp import EXAM_DATES_FOOTER, WA_FORWARD_ASK_MARKUP, _send_text_to_whatsapp
from unibot.monitoring import _format_exam_dates_message, watched_courses


class _ExamActions(dict):
    """short_id -> { unit_id, action_type ("anmelden"|"abmelden"), title,
    user_id, ... }. An action that belongs to a student is also kept in
    their private record, so the button still works after a restart."""

    def __setitem__(self, sid, info):
        super().__setitem__(sid, info)
        if isinstance(info, dict) and info.get("user_id") is not None:
            remember_user_callback(info["user_id"], f"exam:{sid}", info)

    def pop(self, sid, *default):
        info = super().pop(sid, *default)
        if isinstance(info, dict) and info.get("user_id") is not None:
            recall_user_callback(info["user_id"], f"exam:{sid}", forget=True)
        return info


exam_action_cache: dict[str, dict] = _ExamActions()


def _exam_action(sid: str, user_id: int) -> Optional[dict]:
    """The cached exam button action, or None when it expired or belongs to
    another student (a button seen in a group chat or a forwarded message
    must not act on someone else's exam list)."""
    info = exam_action_cache.get(sid)
    if not info:
        # after a restart: only the tapping student's own record is searched
        stored = recall_user_callback(user_id, f"exam:{sid}")
        if stored:
            info = {k: v for k, v in stored.items() if k != "ts"}
            dict.__setitem__(exam_action_cache, sid, info)
    if not info:
        return None
    owner = info.get("user_id")
    if owner is not None and owner != user_id:
        logging.warning("Exam button of user %s tapped by user %s; ignored", owner, user_id)
        return None
    return info


async def send_exam_registration_menu(message, user_id: int):
    """Show currently open-for-registration and currently-registered exams on
    the StuMS/HISinOne portal, each with a Register/Deregister button."""
    session = personal_session(user_id)

    try:
        open_exams = await get_open_exam_registrations(session)
    except Exception as e:
        logging.error("exam menu: failed to fetch open exams: %s", e)
        await message.reply_text("❌ Could not reach the exam registration portal. Please try again later.")
        return

    try:
        registered_exams = await get_registered_exams(session)
    except Exception as e:
        logging.error("exam menu: failed to fetch registered exams: %s", e)
        registered_exams = []

    keyboard = []

    if open_exams:
        # A unit can have several open sittings at once; one Register button per unit is enough.
        seen_units = set()
        keyboard.append([InlineKeyboardButton("📖 Open for registration", callback_data="exam_noop")])
        for exam in sorted(open_exams, key=lambda e: e["end_date"] or ""):
            if exam["unit_id"] in seen_units:
                continue
            seen_units.add(exam["unit_id"])
            sid = str(uuid.uuid4())[:8]
            exam_action_cache[sid] = {
                "unit_id": exam["unit_id"],
                "action_type": "anmelden",
                "title": exam["title"],
                "user_id": getattr(session, "user_id", None),
            }
            label = f"📝 {exam['title']}"
            if exam.get("exam_date_text"):
                label += f" ({exam['exam_date_text']})"
            keyboard.append([InlineKeyboardButton(label[:64], callback_data=f"exam_ask|{sid}")])

    if registered_exams:
        keyboard.append([InlineKeyboardButton("📌 Currently registered", callback_data="exam_noop")])
        for exam in registered_exams:
            sid = str(uuid.uuid4())[:8]
            exam_action_cache[sid] = {
                "unit_id": exam["unit_id"],
                "action_type": "abmelden",
                "title": exam["title"],
                "user_id": getattr(session, "user_id", None),
            }
            keyboard.append([InlineKeyboardButton(f"🗑️ {exam['title']}"[:64], callback_data=f"exam_ask|{sid}")])

    if not keyboard:
        await message.reply_text("ℹ️ No open exam registrations and no active registrations found right now.")
        return

    await message.reply_text(
        "📝 <b>Exam Registration</b>\nSelect an exam to register or deregister:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_exam_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    if query.data == "exam_menu":
        status_msg = await query.message.reply_text("📝 Loading exam registration status...", disable_notification=True)
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Loading exam registration status...")
        try:
            await send_exam_registration_menu(query.message, user_id=query.from_user.id)
        finally:
            await _stop_loading_animation(stop_evt, anim_task)
        return

    if query.data == "exam_noop":
        return

    if query.data == "exam_cancel":
        await query.edit_message_text("Cancelled.")
        return

    action, sid = query.data.split("|", 1)
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.message.reply_text("⚠️ This exam action has expired. Please open the Exam Registration menu again.")
        return

    verb = "register for" if info["action_type"] == "anmelden" else "deregister from"

    if action == "exam_ask":
        keyboard = [[
            InlineKeyboardButton("✅ Yes", callback_data=f"exam_do|{sid}"),
            InlineKeyboardButton("❌ No", callback_data="exam_cancel"),
        ]]
        await query.message.reply_text(
            f"⚠️ Are you sure you want to <b>{verb}</b>:\n<b>{_h(info['title'])}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action == "exam_do":
        status_msg = await query.edit_message_text(f"⏳ Submitting: {verb} {info['title']}...")
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, f"Submitting: {verb} {info['title']}...")
        try:
            # always the tapping student's own StuMS session, never the admin's
            session = get_user_session(query.from_user.id)
            result = await submit_exam_action(session, info["unit_id"], info["action_type"])
        except Exception as e:
            logging.error("exam action failed: %s", e)
            await query.message.reply_text(f"❌ Failed: {str(e)[:200]}")
            return
        finally:
            await _stop_loading_animation(stop_evt, anim_task)
        exam_action_cache.pop(sid, None)
        title = result.get("title") or info["title"]
        if result["success"]:
            if info["action_type"] == "anmelden":
                status_line = f"\nStatus: {result['status']}" if result.get("status") else ""
                await query.message.reply_text(f"✅ Registered: {title}{status_line}")
            else:
                await query.message.reply_text(f"🗑️ Deregistered: {title}")
        else:
            failed_verb = "register for" if info["action_type"] == "anmelden" else "deregister from"
            await query.message.reply_text(f"❌ Failed to {failed_verb} {title}: {result['message']}")


async def handle_examopen_wa_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on an exam-registration notification's "Forward to WA" button
    - ask for confirmation with a new message rather than touching the
    notification's own row, so its other two buttons (Register / Don't
    remind me again) stay usable no matter what's tapped first."""
    query = update.callback_query
    await query.answer()
    sid = query.data.split("|", 1)[1]
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.message.reply_text("⚠️ This notification has expired.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, forward", callback_data=f"examopen_wa_confirm|{sid}"),
        InlineKeyboardButton("❌ Cancel", callback_data="examopen_cancel"),
    ]])
    await query.message.reply_text(
        f"📲 Forward this exam registration notice to WhatsApp?\n<b>{_h(info['title'])}</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_examopen_wa_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Sending to WhatsApp...")
    sid = query.data.split("|", 1)[1]
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.edit_message_text("⚠️ This notification has expired.")
        return
    # Mirrors the Telegram notification's own format (see exam_reminder.py's
    # check_exam_reminders) instead of just the course title - the WA
    # forward used to carry noticeably less information than what the
    # student actually saw in Telegram.
    class_line = f"👥 <b>Class:</b> {_h(info['group_label'])}\n" if info.get("group_label") else ""
    room_line = f"📍 <b>Room:</b> {_h(info['room'])}\n" if info.get("room") else ""
    exam_date_line = f"📅 <b>Exam Date:</b> {_h(info['exam_date_display'])}\n" if info.get("exam_date_display") else ""
    window_line = f"🗓 <b>Window:</b> {info['window_display']}\n" if info.get("window_display") else ""
    text = (
        "🆕 <b>EXAM REGISTRATION OPEN</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📘 <b>Course:</b> {_h(info['title'])}\n"
        f"{exam_date_line}"
        f"{class_line}"
        f"{room_line}"
        f"{window_line}"
        "━━━━━━━━━━━━━━━━━"
    )
    success, message = await _send_text_to_whatsapp(text)
    await query.edit_message_text(f"✅ {message}" if success else f"❌ {message}")


async def handle_examopen_mute_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on an exam-registration notification's "Don't remind me
    again" button - ask for confirmation with a new message, same reasoning
    as handle_examopen_wa_ask."""
    query = update.callback_query
    await query.answer()
    sid = query.data.split("|", 1)[1]
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.message.reply_text("⚠️ This notification has expired.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, mute", callback_data=f"examopen_mute_confirm|{sid}"),
        InlineKeyboardButton("❌ Cancel", callback_data="examopen_cancel"),
    ]])
    await query.message.reply_text(
        f"🔕 Stop notifying you about this exam's registration open/closing-soon reminders?\n<b>{_h(info['title'])}</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_examopen_mute_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add this exam's module code to exam_reminder_cache.json's muted_codes
    list - check_exam_reminders skips building/sending both the "open" and
    "closing soon" notifications for any code in that list, but keeps
    tracking it internally so it isn't mistaken for closed."""
    query = update.callback_query
    await query.answer()
    sid = query.data.split("|", 1)[1]
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.edit_message_text("⚠️ This notification has expired.")
        return
    uid = query.from_user.id  # each student mutes only their own reminders
    cache = load_exam_cache(user_id=uid)
    muted = set(cache.get("muted_codes", []))
    muted.add(info["code"])
    cache["muted_codes"] = sorted(muted)
    save_exam_cache(cache, user_id=uid)
    await query.edit_message_text(f"🔕 Won't remind you about this exam's registration anymore:\n{info['title']}")


async def handle_examopen_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    await query.edit_message_text("Cancelled.")


# ── Feature 1 & 2: Official StuMS PDF Documents (`/documents`) & Tuition (`/tuition`) ─

def _stums_documents_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, spec in STUMS_DOCUMENTS.items():
        rows.append([InlineKeyboardButton(spec["label"], callback_data=f"stums_doc|{key}")])
    rows.append([InlineKeyboardButton("💶 Tuition & Fees (Rückmeldung)", callback_data="tuition_status")])
    return InlineKeyboardMarkup(rows)


async def handle_stums_documents_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the official StuMS PDF certificates download menu."""
    query = update.callback_query
    await query.answer()
    if not is_user_allowed(query.from_user.id):
        return
    text = (
        "📄 <b>Official StuMS Documents (PDF)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select any official certificate below to generate and download it directly from StuMS:"
    )
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=_stums_documents_keyboard())


async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/documents - open the official StuMS PDF certificates menu."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    text = (
        "📄 <b>Official StuMS Documents (PDF)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select any official certificate below to generate and download it directly from StuMS:"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_stums_documents_keyboard())


async def handle_stums_doc_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `stums_doc|<doc_key>` callback: fetch official PDF from StuMS and send to Telegram."""
    query = update.callback_query
    await query.answer("Fetching document from StuMS...")
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    parts = (query.data or "").split("|", 1)
    doc_key = parts[1].strip() if len(parts) > 1 else ""
    doc_aliases = {
        "immatrikulation": "imma",
        "studienbescheinigung": "imma",
        "bafoeg": "imma",
        "studienverlauf": "verlauf",
        "transcript": "transcript_en",
        "grades": "grades_en",
    }
    doc_key = doc_aliases.get(doc_key, doc_key)
    spec = STUMS_DOCUMENTS.get(doc_key)
    if not spec:
        await query.message.reply_text("⚠️ Unknown document type.")
        return

    status_msg = await query.message.reply_text(
        f"⏳ Generating <b>{html.escape(spec['label'])}</b> from StuMS...",
        parse_mode="HTML",
    )
    stop_evt, anim_task = _start_loading_animation(status_msg, f"Generating {spec['label']}...")
    try:
        session = personal_session(user_id)
        pdf_bytes, filename = await download_stums_document(session, doc_key)
    except Exception as exc:
        logging.error("StuMS document download failed for %s: %s", doc_key, exc)
        await _stop_loading_animation(stop_evt, anim_task)
        server = state.login_server
        kb_rows = []
        if server:
            kb_rows.append([InlineKeyboardButton("🎓 Reconnect StuMS", web_app=WebAppInfo(url=server.app_url("stums")))])
        kb_rows.append([InlineKeyboardButton("🔄 Retry", callback_data=f"stums_doc|{doc_key}")])
        await status_msg.edit_text(
            f"❌ Unable to download <b>{html.escape(spec['label'])}</b> via StuMS:\n"
            "<i>Official documents require an active StuMS student session. Tap below to sign in or retry.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    try:
        await status_msg.delete()
    except TelegramError:
        pass

    # Zero-disk: the official PDF goes from StuMS to Telegram through a
    # memory buffer only, which is closed (and the bytes dropped) once sent.
    with io.BytesIO(pdf_bytes) as buffer:
        del pdf_bytes
        await query.message.reply_document(
            document=buffer,
            filename=filename,
            caption=f"✅ <b>{html.escape(spec['label'])}</b>",
            parse_mode="HTML",
        )


async def build_tuition_status_text(session=None, user_id=None) -> tuple[str, InlineKeyboardMarkup]:
    """Fetch and format the StuMS tuition / semester fee (*Rückmeldung*) status."""
    sess = session or personal_session(user_id)
    try:
        info = await get_tuition_status(sess)
    except Exception as exc:
        logging.warning("StuMS tuition status fetch failed: %s", exc)
        lines = [
            "💶 <b>Tuition &amp; Semester Fees (Rückmeldung)</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Tuition data could not be retrieved from StuMS at this time.</i>\n"
            "If your StuMS session expired, please tap below to sign in or retry.",
        ]
        server = state.login_server
        kb_rows = []
        if server:
            kb_rows.append([InlineKeyboardButton("🎓 Reconnect StuMS", web_app=WebAppInfo(url=server.app_url("stums")))])
        kb_rows.append([InlineKeyboardButton("🔄 Retry", callback_data="tuition_status")])
        kb = InlineKeyboardMarkup(kb_rows)
        return "\n".join(lines), kb

    lines = [
        "💶 <b>Tuition &amp; Semester Fees (Rückmeldung)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if info.get("has_open_payments") and info.get("open_invoices"):
        lines.append("⚠️ <b>OPEN PAYMENT DUE</b>")
        for inv in info["open_invoices"]:
            lines.append(
                f"🗓️ <b>Semester:</b> {html.escape(inv['period'])}\n"
                f"💰 <b>Amount Due:</b> <b>{html.escape(inv['to_pay'])}</b> (Paid: {html.escape(inv['credit'])})\n"
                f"⏰ <b>Due Date:</b> <code>{html.escape(inv['due_date'])}</code>\n"
                f"🔖 <b>Purpose (Verwendungszweck):</b> <code>{html.escape(inv['purpose'])}</code>"
            )
            lines.append("━━━━━━━━━━━━━━━━━━")
    else:
        lines.append("✅ <b>All semester fees are paid!</b> (No open payments)")
        lines.append("━━━━━━━━━━━━━━━━━━")

    paid = info.get("paid_invoices") or []
    if paid:
        lines.append("\n🧾 <b>Recent Semester Fee History</b>")
        for inv in list(reversed(paid))[:4]:
            lines.append(
                f"• <b>{html.escape(inv['period'])}</b>\n"
                f"  💳 Paid: <code>{html.escape(inv['credit'])}</code> / Due: <code>{html.escape(inv['to_pay'])}</code>\n"
                f"  📅 Due Date: <code>{html.escape(inv['due_date'])}</code> | Ref: <code>{html.escape(inv['purpose'])}</code>"
            )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Download Enrollment Certificate (PDF)", callback_data="stums_doc|imma")],
        [InlineKeyboardButton("📄 All Official Documents", callback_data="stums_docs_menu")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="tuition_status")],
    ])
    return "\n".join(lines), kb


async def handle_tuition_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `tuition_status` callback."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    status_msg = await query.message.reply_text("💶 Checking StuMS tuition & fee account...")
    stop_evt, anim_task = _start_loading_animation(status_msg, "Checking StuMS tuition & fee account...")
    try:
        user_sess = get_user_session(user_id)
        text, kb = await build_tuition_status_text(session=user_sess, user_id=user_id)
    except Exception as exc:
        logging.error("Tuition status error: %s", exc)
        await _stop_loading_animation(stop_evt, anim_task)
        await status_msg.edit_text(f"❌ Error checking tuition status:\n<code>{html.escape(str(exc)[:220])}</code>", parse_mode="HTML")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)


async def tuition_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tuition - check open and paid semester fees (*Semesterbeitrag* / *Rückmeldung*) on StuMS."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    status_msg = await update.message.reply_text("💶 Checking StuMS tuition & fee account...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Checking StuMS tuition & fee account...")
    try:
        text, kb = await build_tuition_status_text(session=get_user_session(user.id), user_id=user.id)
    except Exception as exc:
        logging.error("tuition_command error: %s", exc)
        await status_msg.edit_text(f"❌ Error checking tuition status:\n<code>{html.escape(str(exc)[:220])}</code>", parse_mode="HTML")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)


def _exam_registration_status_line(exam: dict) -> str:
    """Render a short registration-status line for an exam entry from
    exam_reminder.get_all_exam_dates (action_type/status come from the study
    planner tree's row for that unit)."""
    if exam.get("action_type") == "deregister":
        return "📝 ✅ Registered"
    status = exam.get("status") or ""
    if status == "Passed":
        return "📝 🏁 Passed"
    if status == "Failed":
        return "📝 ❌ Failed"
    return "📝 ⬜ Not registered"


def _registration_period_line(exam: dict) -> Optional[str]:
    """Render the exam's registration (enrollment) window, if known, from
    exam_reminder.get_all_exam_dates's start_date/end_date/is_open fields."""
    start = exam.get("start_date")
    end = exam.get("end_date")
    if not start or not end:
        return None
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    state = "🟢 open" if exam.get("is_open") else "🔴 closed"
    return f"🗓 Registration: {start_dt.strftime('%d.%m.%y')}–{end_dt.strftime('%d.%m.%y')} ({state})"


async def _send_exam_dates_chunks(message, lines: list[str], final_reply_markup=None):
    """Telegram messages are capped at 4096 chars; split into chunks if needed and
    attach `final_reply_markup` (e.g. a WhatsApp-forward or navigation button) to
    only the last chunk."""
    chunks = []
    chunk = ""
    for line in lines:
        candidate = chunk + line + "\n"
        if len(candidate) > 3800:
            chunks.append(chunk)
            chunk = line + "\n"
        else:
            chunk = candidate
    if chunk:
        chunks.append(chunk)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.reply_text(chunk, parse_mode="HTML", reply_markup=final_reply_markup if is_last else None)


MODULE_CODE_RE = re.compile(r'title="((?:wir|inf|ma)[a-z]*\d+)\s*-\s*[^"]+"', re.IGNORECASE)


async def get_my_module_codes(session, courses) -> set:
    """Scrape each Stud.IP course's details page for the StuMS module code(s) it
    belongs to (e.g. 'wir893' for the course "Development Economics"). Stud.IP
    course names and StuMS exam titles frequently do not match at all, but both
    systems reference the same underlying module code, so this is the reliable
    way to link a Stud.IP course to its StuMS exam(s).
    """
    by_course = await get_course_module_codes(session, [cid for _, cid in courses])
    return set().union(*by_course.values()) if by_course else set()


async def get_course_module_codes(session, cids: list) -> dict:
    """{cid: {module_code, ...}}, the per-course form of get_my_module_codes
    (needed to tie a StuMS sitting back to one specific Stud.IP course)."""
    result: dict = {}
    semaphore = asyncio.Semaphore(3)

    async def fetch_one(cid):
        async with semaphore:
            try:
                url = f"{BASE_URL}/dispatch.php/course/details?cid={cid}"
                async with await session.get(url) as r:
                    html_text = await r.text()
            except Exception as e:
                logging.warning("get_course_module_codes: failed to fetch course %s: %s", cid, e)
                return
        result[cid] = {m.group(1).lower() for m in MODULE_CODE_RE.finditer(html_text)}

    await asyncio.gather(*(fetch_one(cid) for cid in cids))
    return result


async def handle_exam_dates_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle exam_dates_list callback - shows upcoming exam dates for the courses
    the student is currently enrolled in on Stud.IP, with each exam's registration
    status, skipping any dates that have already passed."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    status_msg = await query.message.reply_text("📚 Fetching your exam dates... this may take a moment.", disable_notification=True)
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching your exam dates... this may take a moment.")

    try:
        session = personal_session(user_id)
        courses = await list_courses(user_id=user_id)
        module_codes = await get_my_module_codes(session, courses)
        exams = await get_all_exam_dates(session)
    except Exception as e:
        logging.error("My exam dates error: %s", e)
        await query.message.reply_text(f"❌ Error loading exam dates: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    matched = filter_exams_by_module_codes(exams, module_codes)

    today = berlin_now().date()
    upcoming = [e for e in matched if datetime.fromisoformat(e["exam_date"]).date() >= today]
    upcoming.sort(key=lambda e: (e["exam_date"], e["title"]))

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 All Exams", callback_data="exam_dates_all")],
        [InlineKeyboardButton("📄 Scan Course Files Now", callback_data="exam_files_scan")],
    ])

    # Dates instructors announced in course files (see
    # scan_course_files_for_exams), from the daily scan's cache. StuMS
    # dates override them, and those are already listed above.
    try:
        await refresh_stums_exam_sittings(session, all_exams=exams, user_id=user_id)
    except Exception as e:
        logging.warning("handle_exam_dates_list: StuMS override refresh failed: %s", e)
    file_events = [
        e for e in get_file_exam_events(user_id)
        if e.get("source") != "StuMS" and (not e["date"] or e["date"] >= today.isoformat())
    ]
    file_lines = render_file_exam_events(file_events, "📄 <b>From Course Files</b>") if file_events else []

    if not upcoming:
        if file_lines:
            await _send_exam_dates_chunks(query.message, ["ℹ️ No upcoming exam dates in StuMS for your enrolled courses.", ""] + file_lines, final_reply_markup=keyboard)
        else:
            await query.message.reply_text("ℹ️ No upcoming exam dates found for your enrolled courses.", reply_markup=keyboard)
        return

    lines = _format_exam_dates_message("📚 <b>My Upcoming Exam Dates</b>", upcoming)
    if file_lines:
        lines += [""] + file_lines
    await _send_exam_dates_chunks(query.message, lines, final_reply_markup=keyboard)


async def handle_all_exam_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle exam_dates_all callback - shows every upcoming exam date across the
    whole curriculum (registered or not), with each exam's registration status,
    skipping any dates that have already passed."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    status_msg = await query.message.reply_text("📚 Fetching all exam dates... this may take a moment.", disable_notification=True)
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching all exam dates... this may take a moment.")

    try:
        session = personal_session(user_id)
        exams = await get_all_exam_dates(session)
    except Exception as e:
        logging.error("All exam dates error: %s", e)
        await query.message.reply_text(f"❌ Error loading exam dates: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    today = berlin_now().date()
    upcoming = [e for e in exams if datetime.fromisoformat(e["exam_date"]).date() >= today]
    upcoming.sort(key=lambda e: (e["exam_date"], e["title"]))

    if not upcoming:
        await query.message.reply_text("ℹ️ No upcoming exam dates found across the curriculum.")
        return

    lines = _format_exam_dates_message("📋 <b>All Upcoming Exam Dates</b>", upcoming, show_status=False)
    wa_markup = markup_for(user_id, InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]]))
    await _send_exam_dates_chunks(query.message, lines, final_reply_markup=wa_markup)


async def handle_transcript_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle transcript_summary callback - shows a weighted-average grade and
    credit total computed from "My achievements" (Notenspiegel). No WA-forward
    button: this is personal grade data."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    status_msg = await query.message.reply_text("📜 Fetching your transcript...", disable_notification=True)
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching your transcript...")

    try:
        session = personal_session(user_id)
        grades = await get_grades(session)
    except Exception as e:
        logging.error("Transcript summary error: %s", e)
        await query.message.reply_text(
            "❌ Unable to load transcript from StuMS.\n"
            "<i>Your account session may need to be refreshed or StuMS may be temporarily unreachable.</i>"
        )
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    summary = compute_transcript_summary(grades)

    lines = ["📜 <b>Transcript Summary</b>", "━━━━━━━━━━━━━━━━━"]
    gpa_text = summary["gpa"] if summary["gpa"] is not None else "-"
    lines.append(f"📊 <b>Weighted Average Grade:</b> {gpa_text}")
    lines.append(f"🎓 <b>Total Credits Earned:</b> {summary['total_credits']}")
    lines.append(f"✅ <b>Passed:</b> {len(summary['passed'])} module(s)")
    lines.append(f"❌ <b>Failed:</b> {len(summary['failed'])} module(s)")
    lines.append("━━━━━━━━━━━━━━━━━")

    for item in summary["passed"]:
        lines.append(f"✅ {item['title']} - {item['grade']} ({item['credits']} ECTS)")
    for item in summary["failed"]:
        grade_part = f" - {item['grade']}" if item.get("grade") else ""
        lines.append(f"❌ {item['title']}{grade_part}")

    if not summary["passed"] and not summary["failed"]:
        lines.append("ℹ️ No finalized results yet.")

    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("<i>Approximate - not an official transcript.</i>")

    await _send_exam_dates_chunks(query.message, lines)


# ── /exams command ───────────────────────────────────────────────────────────
async def exams_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 📝 Exams button and /exams command."""
    message = update.effective_message
    if not message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await message.reply_text("Not authorized to use this bot.")
        return
    status_msg = await message.reply_text("📝 Loading exams from StuMS...", disable_notification=True)
    await _show_typing(message)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading exams...")
    try:
        await send_exam_registration_menu(message, user_id=user_id)
    finally:
        await _stop_loading_animation(stop_evt, anim_task)
    try:
        await status_msg.delete()
    except TelegramError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
#  WEEKLY CONTENT-AWARE STUDY PLAN
# ──────────────────────────────────────────────────────────────────────────────
# Distinct from build_ai_weekly_plan_narration above (which narrates the
# week's EXISTING schedule as a spoken briefing) - this plans NEW study
# sessions into the free gaps, aware of what each course's lecture files
# actually cover. Deliberately not a RAG/vector-search system: a lightweight
# per-file topic-label cache plus the existing free-time/exam-date
# infrastructure is enough for "what, when, how urgent" without the cost
# or maintenance of full-text semantic search.

STUDY_PLAN_SUPPORTED_EXTENSIONS = (".pdf", ".pptx", ".html")

STUDY_PLAN_MAX_FILE_MB = 20  # whole textbooks dropped into a course folder aren't lecture material and are slow to fetch


# ──────────────────────────────────────────────────────────────────────────────
#  EXAM DATES FROM COURSE FILES
# ──────────────────────────────────────────────────────────────────────────────
# Many instructors only announce the exam and retake dates in a syllabus,
# info sheet, README or the first lecture's slides, long before (or
# instead of) StuMS. This scans those files once a day, sends only the
# passages that mention an exam next to a date to the LLM for structured
# extraction, and caches the result per file (only new/changed files are
# re-processed, like the study plan's topic cache).

EXAM_FILE_CACHE_PATH = "exam_file_cache.json"
EXAM_FILE_REMINDER_DAYS = [7, 3, 1, 0]

# Unlike the study plan, syllabus folders matter here (that's where exam
# dates usually are), so only side material is skipped.
_EXAM_SCAN_SKIPPED_FOLDER_RE = re.compile(r"^(books?|literat\w*|readings?|r ?markdown|src|code|data|labs?|ps|ep|solutions?|videos?|pyblp)$", re.IGNORECASE)
_EXAM_KEYWORD_RE = re.compile(
    r"exam(?!ple)|klausur|prüfung|pruefung|retake|re-?sit|wiederhol|nachklausur|nachschreib|midterm|mid-term|final test|oral test|mündlich|presentation|präsentation|deadline|abgabe|submission",
    re.IGNORECASE,
)
_DATE_HINT_RE = re.compile(
    r"\d{1,2}\s?[./]\s?\d{1,2}\s?[./]|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\.?\s+(?:jan|feb|m[aä]r|apr|ma[iy]|jun|jul|aug|sep|o[ck]t|nov|de[cz])"
    r"|(?:jan|feb|m[aä]r|apr|ma[iy]|jun|jul|aug|sep|o[ck]t|nov|de[cz])[a-zä]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
}
_EXAM_EVENT_KINDS = ("exam", "retake", "midterm", "presentation", "submission")
_EXAM_KIND_LABELS = {
    "exam": "📝 Exam",
    "retake": "🔁 Retake",
    "midterm": "📝 Midterm",
    "presentation": "🎤 Presentation",
    "submission": "📤 Submission",
}


def load_exam_file_cache() -> dict:
    """{"files": {cid: {rel_path: {"size", "modified_ts", "events": [...]}}},
    "notified": {event_key: event}, "reminded": {event_key: [days, ...]}}."""
    if db.exists(EXAM_FILE_CACHE_PATH):
        try:
            return db.load(EXAM_FILE_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load exam file cache: %s", e)
    return {}


def save_exam_file_cache(data: dict) -> None:
    try:
        db.save(EXAM_FILE_CACHE_PATH, data)
    except Exception as e:
        logging.error("Failed to save exam file cache: %s", e)


def _exam_passages(text: str, max_chars: int = 5000) -> str:
    """The parts of a file worth sending to the LLM: a few lines around
    every line that mentions an exam keyword, kept only if that window
    also contains something date-like (or the keyword is about the exam
    format, so "oral exam at the end of the course" isn't lost). Returns
    "" when nothing qualifies, so most lecture slides cost no LLM call."""
    lines = [l.strip() for l in text.splitlines()]
    keep: set = set()
    for i, line in enumerate(lines):
        if not _EXAM_KEYWORD_RE.search(line):
            continue
        window = range(max(0, i - 2), min(len(lines), i + 3))
        window_text = " ".join(lines[j] for j in window)
        if _DATE_HINT_RE.search(window_text) or re.search(r"oral|written|mündlich|schriftlich|grading|evaluation|assessment", window_text, re.IGNORECASE):
            keep.update(window)
    passages, last = [], None
    for j in sorted(keep):
        if last is not None and j != last + 1:
            passages.append("...")
        passages.append(lines[j])
        last = j
    return "\n".join(passages)[:max_chars]


EXAM_EXTRACTION_SYSTEM_PROMPT = """You extract exam-related dates for one university course from passages of a course file (syllabus, info sheet, README or lecture slides). Return every event of these kinds that the text states: "exam" (main/first exam, written or oral), "retake" (second attempt, resit, Wiederholungsprüfung, Nachklausur), "midterm", "presentation" (graded presentation), "submission" (graded paper/project deadline). Ignore lecture dates, registration periods, office hours and dates of old past papers.

Rules:
- "date" is YYYY-MM-DD, or null if the text gives no concrete date (e.g. "oral exam at the end of the course", "date set in the first session") - still include such events so the format is known.
- When the year is missing, use the semester given below. If a stated weekday contradicts the stated year (e.g. "Friday, 12 February 2026" in Winter semester 2026/27, while 12 Feb 2026 is a Thursday), use the year in which the weekday matches and that fits the semester.
- "start"/"end" are HH:MM 24h or null. "format" is e.g. "written", "oral", "presentation" or null. "location" is a room/building or null.
- "quote" is the exact source line(s), max 200 chars.
Respond with strict JSON only: {"events": [{"kind": "...", "date": "...", "start": "...", "end": "...", "format": "...", "location": "...", "note": "<short extra info or null>", "quote": "..."}]}. Return {"events": []} if there is nothing."""


def _fix_exam_year_by_weekday(date_obj, quote: str):
    """Instructors often reuse last year's info sheet and forget to bump the
    year ("Friday, 12 February 2026" in WS 26/27). If the quote names a
    weekday that doesn't match the date, shift by a year when that makes
    it match. Returns (date, corrected)."""
    named = [wd for name, wd in _WEEKDAY_NAMES.items() if re.search(rf"\b{name}\b", quote or "", re.IGNORECASE)]
    if len(set(named)) != 1 or date_obj.weekday() == named[0]:
        return date_obj, False
    for delta in (1, -1):
        try:
            candidate = date_obj.replace(year=date_obj.year + delta)
        except ValueError:
            continue
        if candidate.weekday() == named[0]:
            return candidate, True
    return date_obj, False


def _fix_exam_year_by_semester(date_obj, quote: str):
    """A date written without a year ("31.03.") that the model placed
    before the current semester started belongs to the semester's second
    calendar year (WS 26/27 ends in 2027). Dates with an explicit year in
    the quote are left alone. Returns (date, corrected)."""
    from unibot.study_plan import _semester_lecture_start
    semester_start = _semester_lecture_start(datetime.now(TZ_BERLIN).date())
    if date_obj >= semester_start or str(date_obj.year) in (quote or ""):
        return date_obj, False
    try:
        return date_obj.replace(year=date_obj.year + 1), True
    except ValueError:
        return date_obj, False


def _normalize_exam_events(raw_events: list, source: str) -> list:
    events = []
    for e in raw_events or []:
        if not isinstance(e, dict) or e.get("kind") not in _EXAM_EVENT_KINDS:
            continue
        date_iso, corrected = None, False
        if e.get("date"):
            try:
                d = datetime.fromisoformat(str(e["date"])[:10]).date()
            except ValueError:
                continue
            d, corrected = _fix_exam_year_by_weekday(d, e.get("quote", ""))
            if not corrected:
                d, corrected = _fix_exam_year_by_semester(d, e.get("quote", ""))
            date_iso = d.isoformat()
        events.append({
            "kind": e["kind"],
            "date": date_iso,
            "start": e.get("start") or None,
            "end": e.get("end") or None,
            "format": e.get("format") or None,
            "location": e.get("location") or None,
            "note": e.get("note") or None,
            "quote": (e.get("quote") or "")[:200],
            "year_corrected": corrected,
            "source": source,
        })
    return events


async def extract_exam_events(course_name: str, filename: str, passages: str) -> Optional[list]:
    """LLM extraction for one file's passages. None means the call failed
    (retry next scan), [] means the file genuinely has no exam info."""
    semester = load_default_semester().get("label") or "current semester"
    today = datetime.now(TZ_BERLIN).date()
    # The free model sometimes needs well over a minute; a few attempts
    # here beat waiting for tomorrow's scan to retry.
    parsed = None
    for _ in range(3):
        raw = await _call_openrouter(
            EXAM_EXTRACTION_SYSTEM_PROMPT,
            f"Course: {course_name}\nSemester: {semester}\nToday: {today.isoformat()}\nFile: {filename}\n\nPassages:\n{passages}",
            temperature=0,
            timeout=90,
        )
        parsed = _extract_json_object(raw)
        if parsed is not None:
            break
    if parsed is None or not isinstance(parsed.get("events"), list):
        return None
    return _normalize_exam_events(parsed["events"], filename)


def _exam_event_key(cid: str, e: dict) -> str:
    return f"{cid}|{e['kind']}|{e['date']}|{e.get('start') or ''}"


STUMS_SITTINGS_RECORD = "stums_sittings"


def _student_tracked(user_id: int) -> Optional[set]:
    """The courses the student follows on /progress (None: all of them)."""
    from unibot.study_plan import _auto_tracked_cids, load_study_progress
    progress = load_study_progress(user_id=user_id)
    tracked = progress.get("tracked")
    if tracked is None and progress.get("held"):
        tracked = _auto_tracked_cids(progress, user_id=user_id)
    return None if tracked is None else set(tracked)


def course_file_exam_events(cids=None) -> list:
    """Exam info found in the course files (course facts, the same for
    every student), de-duplicated (the same date often appears in both the
    syllabus and the first slides), each with "cid" and "course"."""
    cache = load_exam_file_cache()
    seen, out = set(), []
    for cid, files in cache.get("files", {}).items():
        if cids is not None and cid not in cids:
            continue
        for entry in files.values():
            for e in entry.get("events", []):
                key = _exam_event_key(cid, e) if e["date"] else f"{cid}|{e['kind']}|tbd"
                if key in seen:
                    continue
                seen.add(key)
                out.append({**e, "cid": cid, "course": course_registry.course_title(cid)})
    return out


def _drop_superseded(out: list) -> list:
    # A course with an upcoming dated event of some kind doesn't also need
    # its "date not announced" entry of the same kind. Past dates don't
    # count: reused slides often still carry last year's deadline.
    today = datetime.now(TZ_BERLIN).date().isoformat()
    dated_kinds = {(e["cid"], e["kind"]) for e in out if e["date"] and e["date"] >= today}
    out = [e for e in out if e["date"] or (e["cid"], e["kind"]) not in dated_kinds]
    out.sort(key=lambda e: (e["date"] or "9999", e["course"]))
    return out


def get_file_exam_events(user_id: Optional[int] = None) -> list:
    """The student's view: course file events of their own (tracked)
    courses, with the dates from their own StuMS sittings taking over the
    file dates. Without a student: every course, files only."""
    if user_id is None:
        return _drop_superseded(course_file_exam_events())
    names = student_courses(user_id)
    tracked = _student_tracked(user_id)
    cids = {cid for cid in names if tracked is None or cid in tracked}
    out = course_file_exam_events(cids)
    for e in out:
        e["course"] = names.get(e["cid"], e["course"])
    # StuMS is authoritative: for a course with StuMS sittings, those
    # replace whatever exam/retake/midterm dates the course files gave
    # (see refresh_stums_exam_sittings). Presentations and submissions
    # aren't in StuMS, so the files stay the source for those.
    stums = personal_record(user_id, STUMS_SITTINGS_RECORD, {}) or {}
    overridden_kinds = ("exam", "retake", "midterm")
    for cid, sittings in stums.items():
        if not sittings or cid not in cids:
            continue
        file_dates = {e["kind"]: e["date"] for e in out if e["cid"] == cid and e["kind"] in overridden_kinds and e["date"]}
        out = [e for e in out if not (e["cid"] == cid and e["kind"] in overridden_kinds)]
        for i, sitting in enumerate(sorted(sittings, key=lambda x: x["date"])):
            kind = "exam" if i == 0 else "retake"
            file_date = file_dates.get(kind)
            out.append({
                "kind": kind, "date": sitting["date"], "start": sitting.get("start"), "end": sitting.get("end"),
                "format": None, "location": sitting.get("room"), "note": None, "quote": "",
                "year_corrected": False, "source": "StuMS",
                "file_date": file_date if file_date and file_date != sitting["date"] else None,
                "cid": cid, "course": names.get(cid, cid),
            })
    return _drop_superseded(out)


async def refresh_stums_exam_sittings(session, all_exams: Optional[list] = None,
                                      user_id: Optional[int] = None) -> None:
    """Store the student's StuMS exam sittings per course (matched via
    module codes, which are course facts cached for everyone) in their own
    record, where get_file_exam_events uses them to override dates found
    in course files. all_exams (the student's own StuMS list) can be passed
    in by a caller that already fetched get_all_exam_dates."""
    if user_id is None:
        return  # sittings and passed modules are personal
    cache = load_exam_file_cache()
    codes = cache.get("module_codes", {})
    cids = list(student_courses(user_id))
    missing = [cid for cid in cids if cid not in codes]
    if missing:
        fetched = await get_course_module_codes(session, missing)
        codes.update({cid: sorted(c) for cid, c in fetched.items()})
    if all_exams is None:
        all_exams = await get_all_exam_dates(session)

    sittings: dict = {}
    for cid in cids:
        course_codes = set(codes.get(cid, []))
        if not course_codes:
            continue
        seen: dict[str, Any] = {}
        for e in all_exams:
            # A course listed under several modules (e.g. a core elective
            # pool) must not take the date of one the student already passed.
            if base_module_code(e.get("code", "")) not in course_codes or not e.get("exam_date") or is_passed_exam(e):
                continue
            date_iso = str(e["exam_date"])[:10]
            times = re.findall(r"\b(\d{1,2}:\d{2})", e.get("exam_date_display") or "")
            seen.setdefault(date_iso, {
                "date": date_iso,
                "start": times[0] if times else None,
                "end": times[1] if len(times) > 1 else None,
                "room": e.get("room"),
            })
        if seen:
            sittings[cid] = list(seen.values())

    save_personal_record(user_id, STUMS_SITTINGS_RECORD, sittings)
    cache = load_exam_file_cache()
    cache["module_codes"] = codes
    cache.pop("stums_sittings", None)  # the old single-account sittings
    save_exam_file_cache(cache)


def _followed_by_someone(cid: str) -> bool:
    """Whether any of the course's students follows it on /progress (all
    their courses while their selection is automatic)."""
    members = (getattr(state, "course_members", None) or {}).get(cid) or set()
    for uid in members:
        try:
            tracked = _student_tracked(uid)
        except Exception:
            logging.debug("Study progress unavailable", exc_info=True)
            return True
        if tracked is None or cid in tracked:
            return True
    return not members


EXAM_SCAN_STUDENT_MIN_SECONDS = 6 * 3600
_last_exam_file_scan = 0.0


@single_flight
async def scan_course_files_for_exams(session=None) -> list:
    """Scan the files of every watched course someone follows for exam
    dates, each course once with its registry scanner (a member's own
    session, handing over to another member when it fails). Returns the
    dated, still-upcoming course events that are new since the last scan
    (each with "previous_date" set when it replaced an older date of the
    same kind), for notify_new_file_exam_events. `session` is unused (kept
    for callers)."""
    from unibot.study_plan import _file_size_mb, _list_course_files, classify_lecture_file
    global _last_exam_file_scan
    _last_exam_file_scan = time.time()
    plan = [(name, cid, scanner) for name, cid, scanner in await watched_courses() if _followed_by_someone(cid)]
    cache = load_exam_file_cache()
    files_cache = cache.setdefault("files", {})
    semaphore = asyncio.Semaphore(3)

    async def process_course(course_name, cid, scanner):
        async with semaphore:
            try:
                files, http = await course_registry.scan_with_failover(
                    cid, scanner, lambda sess: _list_course_files(cid, _EXAM_SCAN_SKIPPED_FOLDER_RE, session=sess))
            except Exception as e:
                logging.warning("scan_course_files_for_exams: listing failed for %s: %s", cid, e)
                return
            course_cache = files_cache.setdefault(cid, {})
            for folder, f in files:
                name, url = f.get("name"), f.get("url")
                if not name or not url or os.path.splitext(name)[1].lower() not in STUDY_PLAN_SUPPORTED_EXTENSIONS:
                    continue
                # Past papers carry the dates of old exams; exercises never
                # announce the exam.
                if classify_lecture_file(name, folder)[0] in ("exam", "exercise"):
                    continue
                if _file_size_mb(f.get("size")) > STUDY_PLAN_MAX_FILE_MB:
                    continue
                key = f"{folder}/{name}" if folder else name
                dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
                modified_ts = None if dt == datetime.min else int(dt.timestamp())
                cur_size = str(f.get("size", "-")).strip()
                old = course_cache.get(key)
                if old is not None and str(old.get("size", "-")).strip() == cur_size and not (
                    old.get("modified_ts") and modified_ts and abs(modified_ts - old["modified_ts"]) > 86400
                ):
                    continue

                try:
                    async with await http.get(url) as resp:
                        if resp.status != 200:
                            continue
                        content = await resp.read()
                except Exception as e:
                    logging.warning("scan_course_files_for_exams: download failed for %s: %s", key, e)
                    continue
                passages = _exam_passages(await extract_text_from_file_async(name, content) or "")
                if passages:
                    events = await extract_exam_events(course_name, name, passages)
                    if events is None:
                        continue  # LLM failed: leave uncached so the next scan retries
                else:
                    events = []
                course_cache[key] = {"size": cur_size, "modified_ts": modified_ts, "events": events}
                if events:
                    logging.info("📄 Exam info in %s / %s: %s", course_name, key, [(e['kind'], e['date']) for e in events])

    await asyncio.gather(*(process_course(name, cid, scanner) for name, cid, scanner in plan))
    # The scan awaited for minutes; only write its own key onto a fresh copy
    # so module codes / notified flags written meanwhile aren't undone.
    fresh = load_exam_file_cache()
    fresh["files"] = files_cache
    save_exam_file_cache(fresh)

    today = datetime.now(TZ_BERLIN).date().isoformat()
    notified = cache.get("notified", {})
    current = {
        _exam_event_key(e["cid"], e): e
        for e in course_file_exam_events()
        if e["date"] and e["date"] >= today
    }
    new_events = []
    for key, exam in current.items():
        if key in notified:
            continue
        replaced = next(
            (old for old_key, old in notified.items()
             if old_key not in current and old.get("cid") == exam["cid"] and old.get("kind") == exam["kind"] and old.get("date", "") >= today),
            None,
        )
        new_events.append({**exam, "previous_date": replaced["date"] if replaced else None})
    cache = load_exam_file_cache()
    cache["notified"] = {k: v for k, v in current.items()}
    save_exam_file_cache(cache)
    return new_events


def _format_exam_event_line(e: dict) -> str:
    label = _EXAM_KIND_LABELS.get(e["kind"], e["kind"])
    if e["date"]:
        d = datetime.fromisoformat(e["date"]).date()
        when = f"{d:%a %d.%m.%Y}"
        if e.get("start"):
            when += f", {e['start']}" + (f"–{e['end']}" if e.get("end") else "")
    else:
        when = "date not announced yet"
    extras = [x for x in (e.get("format"), e.get("location")) if x]
    line = f"{label}: {when}" + (f" ({html.escape(', '.join(extras))})" if extras else "")
    if e.get("source") == "StuMS":
        line += " ✅ StuMS"
    if e.get("file_date"):
        line += f"\n   ℹ️ course file said {datetime.fromisoformat(e['file_date']).date():%d.%m.%Y}, StuMS date used"
    if e.get("previous_date"):
        line += f"\n   ⚠️ changed from {datetime.fromisoformat(e['previous_date']).date():%d.%m.%Y}"
    if e.get("year_corrected"):
        line += "\n   ⚠️ year corrected: the file's year was missing or didn't match the weekday/semester"
    return line


def render_file_exam_events(events: list, header: str) -> list:
    """Lines grouped by course, each with its source file."""
    lines = [header, "━━━━━━━━━━━━━━━━━"]
    by_course: dict = {}
    for e in events:
        by_course.setdefault(e["course"], []).append(e)
    for course, course_events in by_course.items():
        lines.append(f"📘 <b>{html.escape(course)}</b>")
        for e in course_events:
            lines.append(_format_exam_event_line(e))
        sources = sorted({e["source"] for e in course_events})
        lines.append(f"📎 <i>{html.escape(', '.join(sources))}</i>")
        lines.append("━━━━━━━━━━━━━━━━━")
    return lines


async def notify_new_file_exam_events(bot, new_events: list) -> None:
    """New exam dates from course files, one message per course, only to
    that course's students (in their own course alert settings)."""
    by_course: dict = {}
    for e in new_events:
        by_course.setdefault(e["cid"], []).append(e)
    for cid, events in by_course.items():
        members = course_members(cid)
        if members is None:
            logging.info("New exam dates of %s not sent: its students aren't known yet", cid)
            continue
        lines = render_file_exam_events(events, "📄 <b>New exam dates</b> <i>(course files)</i>")
        lines.append(EXAM_DATES_FOOTER)
        title = f"📄 New exam date: {html.escape(events[0]['course'])}"
        await course_registry.fan_out(bot, cid, "exams", "\n".join(lines), title,
                                      reply_markup=WA_FORWARD_ASK_MARKUP, members=members)


def _registered_dates(user_id: int) -> set:
    """Dates of the StuMS sittings the student is registered for (recorded
    by their own exam date check)."""
    try:
        return set(load_exam_cache(user_id).get("registered_dates") or [])
    except Exception:
        logging.debug("Exam cache unreadable", exc_info=True)
        return set()


async def check_file_exam_reminders(bot, registered: Optional[list] = None) -> None:
    """Reminders for exam dates found in course files, EXAM_FILE_REMINDER_DAYS
    days before, to the course's students only. A student registered in
    StuMS for a sitting on that date is skipped (check_exam_date_reminders
    covers it for them). A retake is only reminded once the course's main
    exam date has passed, since before that it isn't the next relevant
    date. `registered` is unused (kept for callers)."""
    today = datetime.now(TZ_BERLIN).date()
    cache = load_exam_file_cache()
    reminded = cache.get("reminded", {})
    events = [e for e in course_file_exam_events() if e["date"]]
    main_exam_passed = {e["cid"] for e in events if e["kind"] == "exam" and e["date"] < today.isoformat()}

    for e in events:
        days_left = (datetime.fromisoformat(e["date"]).date() - today).days
        if days_left < 0:
            continue
        if e["kind"] == "retake" and e["cid"] not in main_exam_passed:
            continue
        key = _exam_event_key(e["cid"], e)
        sent = set(reminded.get(key, []))
        due = [d for d in EXAM_FILE_REMINDER_DAYS if days_left <= d and d not in sent]
        if not due:
            continue
        members = course_members(e["cid"])
        if members is None:
            continue  # students not known yet (just restarted): next round, not marked as sent
        when_line = "🔴 <b>TODAY!</b>" if days_left == 0 else f"⏳ <b>{days_left} day(s) left</b>"
        text = "\n".join([
            "📚 <b>UPCOMING EXAM</b>" + ("" if e.get("source") == "StuMS" else " <i>(from course files)</i>"),
            "━━━━━━━━━━━━━━━━━",
            f"📘 <b>{html.escape(e['course'])}</b>",
            _format_exam_event_line(e),
            when_line,
            f"📎 <i>{html.escape(e['source'])}</i>",
            "━━━━━━━━━━━━━━━━━",
        ])
        recipients = {uid for uid in course_registry.broadcast_targets(e["cid"], members)
                      if e["date"] not in _registered_dates(uid)}
        await course_registry.fan_out(bot, e["cid"], "exams", text, f"📚 {html.escape(e['course'])}: {when_line}",
                                      reply_markup=WA_FORWARD_ASK_MARKUP, recipients=recipients)
        # Mark every threshold already passed too, so a reminder that was
        # missed (bot down) isn't followed by a burst of older ones.
        reminded[key] = sorted(sent | set(due))
    cache = load_exam_file_cache()
    cache["reminded"] = reminded
    save_exam_file_cache(cache)


async def handle_exam_files_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exam_files_scan button: scan course files now and show everything
    found (new dates are also announced like in the daily scan)."""
    query = update.callback_query
    await query.answer()
    if not is_user_allowed(query.from_user.id):
        return
    # a scan reads every watched course and asks the AI about new files:
    # students may start one when the last is EXAM_SCAN_STUDENT_MIN_SECONDS
    # old, otherwise they see what the last scan found
    fresh = time.time() - _last_exam_file_scan < EXAM_SCAN_STUDENT_MIN_SECONDS
    scan_now = is_user_admin(query.from_user.id) or not fresh
    status_msg = await query.message.reply_text(
        "📄 Scanning course files for exam dates..." if scan_now else "📄 Showing the latest scan of your course files...",
        disable_notification=True)
    try:
        if scan_now:
            new_events = await scan_course_files_for_exams()
            await notify_new_file_exam_events(context.bot, new_events)
        events = get_file_exam_events(query.from_user.id)
        today = datetime.now(TZ_BERLIN).date().isoformat()
        events = [e for e in events if not e["date"] or e["date"] >= today]
        if not events:
            await status_msg.edit_text("📄 No exam dates found in your course files.")
            return
        await status_msg.delete()
        await _send_exam_dates_chunks(query.message, render_file_exam_events(events, "📄 <b>Exam Info From Course Files</b>"))
    except Exception as e:
        logging.exception("handle_exam_files_scan failed")
        await status_msg.edit_text(f"❌ Scan failed: {e}")
