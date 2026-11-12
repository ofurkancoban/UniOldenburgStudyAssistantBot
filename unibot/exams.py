"""StuMS exams: registration, dates, grades, documents and tuition.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import math
import re
import time
import uuid
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from exam_reminder import (
    STUMS_DOCUMENTS,
    compute_transcript_summary,
    download_stums_document,
    filter_exams_by_module_codes,
    get_all_exam_dates,
    get_grades,
    get_open_exam_registrations,
    get_registered_exams,
    get_tuition_status,
    load_exam_cache,
    save_exam_cache,
    submit_exam_action,
)
from studip_utils import berlin_now, html_escape as _h

from unibot import db
from unibot import state
from unibot.storage import (
    CALLBACK_TTL_SECONDS,
    personal_record,
    recall_user_callback,
    remember_user_callback,
    save_personal_record,
)
from unibot.telegram_ui import (
    _delete_status_message,
    _show_typing,
    _split_message,
    _start_loading_animation,
    _stop_loading_animation,
    markup_for,
)
from unibot.auth import get_user_session, is_user_admin, is_user_allowed, personal_session
from unibot.studip_api import BASE_URL, list_courses
from unibot.whatsapp import WA_ADMIN_ONLY, _send_text_to_whatsapp, whatsapp_forward_in_flight
from unibot.monitoring import _format_exam_dates_message


class _ExamActions(dict):
    """short_id -> { unit_id, action_type ("anmelden"|"abmelden"), title,
    user_id, ... }. An action that belongs to a student is also kept in
    their private record, so the button still works after a restart."""

    MAX_ITEMS = 2000

    @staticmethod
    def _created_at(info) -> Optional[float]:
        if not isinstance(info, dict):
            return None
        value = info.get("_cache_ts")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            stamp = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return stamp if math.isfinite(stamp) else None

    def _drop(self, sid: str, info: dict) -> None:
        dict.pop(self, sid, None)
        user_id = info.get("user_id")
        if user_id is not None:
            recall_user_callback(user_id, f"exam:{sid}", forget=True)

    def _prune(self) -> None:
        now = time.time()
        entries = list(dict.items(self))
        for sid, info in entries:
            created = self._created_at(info)
            if created is None or not 0 <= now - created <= CALLBACK_TTL_SECONDS:
                self._drop(sid, info)
        remaining = list(dict.items(self))
        if len(remaining) > self.MAX_ITEMS:
            oldest = sorted(remaining, key=lambda row: self._created_at(row[1]) or 0)
            for sid, info in oldest[:len(remaining) - self.MAX_ITEMS]:
                self._drop(sid, info)

    def __setitem__(self, sid, info):
        self._prune()
        if isinstance(info, dict) and info.get("user_id") is not None:
            if not remember_user_callback(info["user_id"], f"exam:{sid}", info):
                raise RuntimeError("Could not persist the exam button action")
        cached = {**info, "_cache_ts": time.time()} if isinstance(info, dict) else info
        super().__setitem__(sid, cached)
        self._prune()

    def get(self, sid, default=None):
        self._prune()
        return super().get(sid, default)

    def items(self):
        self._prune()
        return super().items()

    def restore(self, sid: str, info: dict) -> None:
        """Load a persisted action without extending its original expiry."""
        dict.__setitem__(self, sid, info)
        self._prune()

    def pop(self, sid, *default):
        self._prune()
        info = super().pop(sid, *default)
        if isinstance(info, dict) and info.get("user_id") is not None:
            recall_user_callback(info["user_id"], f"exam:{sid}", forget=True)
        return info


exam_action_cache: _ExamActions = _ExamActions()
_pending_exam_actions: set[tuple[int, str]] = set()


def _exam_action(sid: str, user_id: int) -> Optional[dict]:
    """The cached exam button action, or None when it expired or belongs to
    another student (a button seen in a group chat or a forwarded message
    must not act on someone else's exam list)."""
    info = exam_action_cache.get(sid)
    if not info:
        # after a restart: only the tapping student's own record is searched
        stored = recall_user_callback(user_id, f"exam:{sid}")
        if stored:
            created = _ExamActions._created_at({"_cache_ts": stored.get("ts")})
            age = time.time() - created if created is not None else None
            if age is None or not 0 <= age <= CALLBACK_TTL_SECONDS:
                recall_user_callback(user_id, f"exam:{sid}", forget=True)
                return None
            info = {k: v for k, v in stored.items() if k != "ts"}
            info["_cache_ts"] = created
            exam_action_cache.restore(sid, info)
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
    warnings = []
    failed_codes: set = set()

    try:
        open_exams = await get_open_exam_registrations(session, failed_codes=failed_codes)
    except Exception as e:
        logging.error("exam menu: failed to fetch open exams: %s", e)
        await message.reply_text("❌ Could not reach the exam registration portal. Please try again later.")
        return
    if failed_codes:
        warnings.append(f"⚠️ Could not check {len(failed_codes)} exam unit(s); the open list may be incomplete.")

    try:
        registered_exams = await get_registered_exams(session)
    except Exception as e:
        logging.error("exam menu: failed to fetch registered exams: %s", e)
        registered_exams = []
        warnings.append("⚠️ Could not check your current registrations; the list may be incomplete.")

    if not db.private_context_is_current(user_id):
        return

    keyboard = []
    action_store_failed = False

    if open_exams:
        # A unit can have several open sittings at once; one Register button per unit is enough.
        seen_units = set()
        open_buttons = []
        for exam in sorted(open_exams, key=lambda e: e["end_date"] or ""):
            if exam["unit_id"] in seen_units:
                continue
            seen_units.add(exam["unit_id"])
            sid = str(uuid.uuid4())[:8]
            try:
                exam_action_cache[sid] = {
                    "unit_id": exam["unit_id"],
                    "action_type": "anmelden",
                    "title": exam["title"],
                    "user_id": getattr(session, "user_id", None),
                }
            except RuntimeError:
                logging.warning("Could not persist an exam registration button for user %s", user_id)
                action_store_failed = True
                continue
            label = f"📝 {exam['title']}"
            if exam.get("exam_date_text"):
                label += f" ({exam['exam_date_text']})"
            open_buttons.append([InlineKeyboardButton(label[:64], callback_data=f"exam_ask|{sid}")])
        if open_buttons:
            keyboard.append([InlineKeyboardButton("📖 Open for registration", callback_data="exam_noop")])
            keyboard.extend(open_buttons)

    if registered_exams:
        registered_buttons = []
        for exam in registered_exams:
            sid = str(uuid.uuid4())[:8]
            try:
                exam_action_cache[sid] = {
                    "unit_id": exam["unit_id"],
                    "action_type": "abmelden",
                    "title": exam["title"],
                    "user_id": getattr(session, "user_id", None),
                }
            except RuntimeError:
                logging.warning("Could not persist an exam registration button for user %s", user_id)
                action_store_failed = True
                continue
            registered_buttons.append([InlineKeyboardButton(f"🗑️ {exam['title']}"[:64], callback_data=f"exam_ask|{sid}")])
        if registered_buttons:
            keyboard.append([InlineKeyboardButton("📌 Currently registered", callback_data="exam_noop")])
            keyboard.extend(registered_buttons)

    if action_store_failed:
        warnings.append("⚠️ Some action buttons could not be prepared safely.")

    if not keyboard:
        if warnings:
            await message.reply_text("\n".join(warnings + ["ℹ️ Could not build a complete exam list. Please try again later."]))
        else:
            await message.reply_text("ℹ️ No open exam registrations and no active registrations found right now.")
        return

    warning_text = "\n".join(f"<i>{_h(warning)}</i>" for warning in warnings)
    warning_text = f"\n{warning_text}\n" if warning_text else "\n"
    await message.reply_text(
        "📝 <b>Exam Registration</b>\nSelect an exam to register or deregister:"
        f"{warning_text}",
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
            await _delete_status_message(status_msg)
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
        action_key = (query.from_user.id, sid)
        if action_key in _pending_exam_actions:
            await query.message.reply_text("⏳ This exam action is already being submitted.")
            return
        _pending_exam_actions.add(action_key)
        try:
            status_msg = await query.edit_message_text(f"⏳ Submitting: {verb} {info['title']}...")
            await _show_typing(query)
            stop_evt, anim_task = _start_loading_animation(status_msg, f"Submitting: {verb} {info['title']}...")
            try:
                # always the tapping student's own StuMS session, never the admin's
                session = get_user_session(query.from_user.id)
                if not db.private_context_is_current(user_id):
                    return
                result = await submit_exam_action(session, info["unit_id"], info["action_type"])
            except Exception as e:
                logging.error("exam action failed: %s", e)
                await query.message.reply_text(f"❌ Failed: {str(e)[:200]}")
                return
            finally:
                await _stop_loading_animation(stop_evt, anim_task)
                await _delete_status_message(status_msg)

            if not db.private_context_is_current(user_id):
                return

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
        finally:
            _pending_exam_actions.discard(action_key)


async def handle_examopen_wa_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on an exam-registration notification's "Forward to WA" button
    - ask for confirmation with a new message rather than touching the
    notification's own row, so its other two buttons (Register / Don't
    remind me again) stay usable no matter what's tapped first."""
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        # the group belongs to the administrator: students can't post into it
        await query.answer(WA_ADMIN_ONLY, show_alert=True)
        return
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
    if not is_user_admin(query.from_user.id):
        await query.answer(WA_ADMIN_ONLY, show_alert=True)
        return
    forward_key = (query.message.chat_id, query.message.message_id)
    if whatsapp_forward_in_flight(forward_key):
        await query.answer("This message is already being sent.", show_alert=True)
        return
    await query.answer("Sending to WhatsApp...")
    sid = query.data.split("|", 1)[1]
    info = _exam_action(sid, query.from_user.id)
    if not info:
        await query.edit_message_text("⚠️ This notification has expired.")
        return
    if not db.private_context_is_current(query.from_user.id):
        return
    # Mirrors the Telegram notification's own format (see exam_reminder.py's
    # check_exam_reminders) instead of just the course title - the WA
    # forward used to carry noticeably less information than what the
    # student actually saw in Telegram.
    class_line = f"👥 <b>Class:</b> {_h(info['group_label'])}\n" if info.get("group_label") else ""
    room_line = f"📍 <b>Room:</b> {_h(info['room'])}\n" if info.get("room") else ""
    exam_date_line = f"📅 <b>Exam Date:</b> {_h(info['exam_date_display'])}\n" if info.get("exam_date_display") else ""
    window_line = f"🗓 <b>Window:</b> {_h(info['window_display'])}\n" if info.get("window_display") else ""
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
    success, message = await _send_text_to_whatsapp(
        text,
        dedupe_key=forward_key,
    )
    if not success and message == "This message is already being sent.":
        return
    if not db.private_context_is_current(query.from_user.id):
        return
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
    if not db.private_context_is_current(uid):
        return
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
        if not db.private_context_is_current(user_id) or not is_user_allowed(user_id):
            await _delete_status_message(status_msg)
            return
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

    if not db.private_context_is_current(user_id) or not is_user_allowed(user_id):
        await _delete_status_message(status_msg)
        return

    try:
        await status_msg.delete()
    except TelegramError:
        pass

    # Deletion, /stop or an administrator block may happen while the status
    # message is being removed. Recheck immediately before sending the PDF.
    if not db.private_context_is_current(user_id) or not is_user_allowed(user_id):
        return

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

    if not db.private_context_is_current(user_id):
        return
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

    if not db.private_context_is_current(user.id):
        return
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
    chunks = _split_message("\n".join(lines), limit=3800)

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
        await _delete_status_message(status_msg)

    if not db.private_context_is_current(user_id):
        return

    matched = filter_exams_by_module_codes(exams, module_codes)

    today = berlin_now().date()
    upcoming = [e for e in matched if datetime.fromisoformat(e["exam_date"]).date() >= today]
    upcoming.sort(key=lambda e: (e["exam_date"], e["title"]))

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 All Exams", callback_data="exam_dates_all")]])

    if not upcoming:
        await query.message.reply_text("ℹ️ No upcoming exam dates found for your enrolled courses.", reply_markup=keyboard)
        return

    lines = _format_exam_dates_message("📚 <b>My Upcoming Exam Dates</b>", upcoming)
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
        await _delete_status_message(status_msg)

    if not db.private_context_is_current(user_id):
        return

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
        await _delete_status_message(status_msg)

    if not db.private_context_is_current(user_id):
        return

    summary = compute_transcript_summary(grades)

    lines = ["📜 <b>Transcript Summary</b>", "━━━━━━━━━━━━━━━━━"]
    gpa_text = summary["gpa"] if summary["gpa"] is not None else "-"
    lines.append(f"📊 <b>Weighted Average Grade:</b> {gpa_text}")
    lines.append(f"🎓 <b>Total Credits Earned:</b> {summary['total_credits']}")
    lines.append(f"✅ <b>Passed:</b> {len(summary['passed'])} module(s)")
    lines.append(f"❌ <b>Failed:</b> {len(summary['failed'])} module(s)")
    lines.append("━━━━━━━━━━━━━━━━━")

    for item in summary["passed"]:
        title = html.escape(str(item.get("title") or "Module"))
        grade = html.escape(str(item.get("grade") or ""))
        lines.append(f"✅ {title} - {grade} ({item['credits']} ECTS)")
    for item in summary["failed"]:
        title = html.escape(str(item.get("title") or "Module"))
        grade_part = f" - {html.escape(str(item['grade']))}" if item.get("grade") else ""
        lines.append(f"❌ {title}{grade_part}")

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


# Exam dates used to be read from course files (syllabus, slides) as well;
# that feature was removed. Its stored data is dropped once at startup.
RETIRED_EXAM_FILE_CACHE_PATH = "exam_file_cache.json"
RETIRED_STUMS_SITTINGS_RECORD = "stums_sittings"


def drop_retired_exam_file_data(user_ids) -> None:
    """Delete what the removed course-file exam scan stored: the shared
    per-file cache and each student's StuMS sitting copy."""
    try:
        if db.exists(RETIRED_EXAM_FILE_CACHE_PATH):
            db.delete(RETIRED_EXAM_FILE_CACHE_PATH)
            logging.info("Removed the retired course-file exam cache")
    except Exception:
        logging.warning("Could not remove the retired course-file exam cache", exc_info=True)
    for uid in user_ids:
        try:
            if personal_record(uid, RETIRED_STUMS_SITTINGS_RECORD):
                save_personal_record(uid, RETIRED_STUMS_SITTINGS_RECORD, None)
        except Exception:
            logging.debug("Retired exam record not removed for a student", exc_info=True)
