"""Course enrolment, de-enrolment, Fast Enroll jobs and semester settings.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from fast_enroll import clear_pending_job, decline_course, enroll_now, get_semester_options, load_pending

from unibot import state
from unibot.storage import (
    clear_default_semester,
    get_scan_previous_semester_enabled,
    get_watch_previous_semester_enabled,
    load_default_semester,
    save_default_semester,
    set_scan_previous_semester_enabled,
    set_watch_previous_semester_enabled,
)
from unibot.telegram_ui import (
    user_data,
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
    get_main_keyboard,
)
from unibot.auth import is_user_allowed, login_studip
from unibot.schedule import fastenroll_tasks, schedule_fastenroll_job
from unibot.study_plan import _render_enrolled_courses_for_semester, _render_open_courses_for_semester
from unibot.tasks import _run_fastenroll_task


SEM_ID_RE = re.compile(r"(?:sem_id=|course_id=)?([a-f0-9]{32})")


def extract_sem_id(text: str) -> Optional[str]:
    """Pull a 32-char hex Stud.IP course id out of a raw id or a pasted course URL."""
    match = SEM_ID_RE.search(text.strip())
    return match.group(1) if match else None


def validate_fastenroll_args(sem_id: Optional[str], target_time_str: Optional[str], target_date_str: Optional[str]) -> Optional[str]:
    """Return an error message if invalid, else None."""
    if not sem_id or not re.fullmatch(r"[a-f0-9]{32}", sem_id):
        return "Could not find a valid course id (expected a 32-char hex sem_id, or a course link containing one)."
    if not target_time_str or not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", target_time_str):
        return "Time must be in HH:MM:SS format (Europe/Berlin)."
    if target_date_str and not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", target_date_str):
        return "Date must be in DD.MM.YYYY format."
    return None


def _fastenroll_job_line(sem_id: str, job: dict) -> str:
    when = f"{job.get('target_date_str')} {job.get('target_time_str')}" if job.get("target_date_str") else f"{job.get('target_time_str')} (next occurrence)"
    return f"• {sem_id} — {when}"


def _build_fastenroll_list_content(chat_id: int):
    """Return (text, InlineKeyboardMarkup | None) for the jobs across every Telegram
    chat connected to this bot, not just `chat_id` — the bot logs into a single
    shared Stud.IP account regardless of which chat scheduled a job, so a job set
    up from one chat/device should be visible (and cancellable) from any other."""
    all_pending = load_pending()
    entries = [
        (other_chat_id_str, sem_id, job)
        for other_chat_id_str, jobs in all_pending.items()
        for sem_id, job in jobs.items()
    ]
    if not entries:
        return "No fast-enroll jobs scheduled.", None

    lines = ["📋 Scheduled fast-enroll jobs (all chats):"]
    keyboard = []
    for owner_chat_id_str, sem_id, job in entries:
        owner_tag = " (this chat)" if owner_chat_id_str == str(chat_id) else f" (chat {owner_chat_id_str})"
        lines.append(_fastenroll_job_line(sem_id, job) + owner_tag)
        keyboard.append([InlineKeyboardButton(
            f"🛑 Cancel {sem_id[:8]}…{owner_tag}",
            callback_data=f"fastenroll_cancel|{owner_chat_id_str}|{sem_id}",
        )])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def send_fastenroll_list(chat, chat_id: int):
    """Send the pending-jobs list with an inline Cancel button per job. `chat` is
    anything with a .reply_text(...) coroutine (an Update.message or CallbackQuery.message)."""
    text, markup = _build_fastenroll_list_content(chat_id)
    await chat.reply_text(text, reply_markup=markup)


async def fastenroll_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return
    await send_fastenroll_list(update.message, update.effective_chat.id)


async def cancel_fastenroll_job(chat_id: int, sem_id: str) -> str:
    """Cancel a running/pending job for one course. Returns the result message."""
    task = fastenroll_tasks.get(chat_id, {}).get(sem_id)
    if task and not task.done():
        task.cancel()
        return f"🛑 Cancelled fast-enroll job for course {sem_id}."
    clear_pending_job(chat_id, sem_id)
    return f"No running job found for {sem_id}, removed from pending list if present."


def _find_job_owner_chat_id(sem_id: str) -> Optional[int]:
    """Search every chat's pending jobs for `sem_id`, return the owning chat_id or None."""
    all_pending = load_pending()
    for chat_id_str, jobs in all_pending.items():
        if sem_id in jobs:
            return int(chat_id_str)
    return None


async def fastenroll_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return
    chat_id = update.effective_chat.id
    args = context.args or []
    if len(args) != 1:
        if not load_pending():
            await update.message.reply_text("No fast-enroll jobs scheduled to cancel.")
            return
        await send_fastenroll_list(update.message, chat_id)
        return

    sem_id = extract_sem_id(args[0]) or args[0]
    owner_chat_id = _find_job_owner_chat_id(sem_id) or chat_id
    result = await cancel_fastenroll_job(owner_chat_id, sem_id)
    await update.message.reply_text(result)


async def fastenroll_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    _, owner_chat_id_str, sem_id = query.data.split("|", 2)
    owner_chat_id = int(owner_chat_id_str)
    result = await cancel_fastenroll_job(owner_chat_id, sem_id)

    text, markup = _build_fastenroll_list_content(query.message.chat_id)
    try:
        await query.edit_message_text(f"{result}\n\n{text}", reply_markup=markup)
    except TelegramError:
        # Message may be too old to edit (>48h) or already identical; fall back to a fresh reply.
        await query.message.reply_text(result)


async def fastenroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /fastenroll <sem_id_or_link> <HH:MM:SS> [DD.MM.YYYY]  — waits until the
    given Berlin-time clock reading (tracked from the site's own clock widget, not
    the local machine's clock), then submits the course enrollment the instant it
    arrives. If the date is omitted, the next occurrence of that time (today or
    tomorrow) is used."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    args = context.args or []
    if len(args) not in (2, 3):
        await update.message.reply_text(
            "Usage: /fastenroll <sem_id_or_link> <HH:MM:SS> [DD.MM.YYYY]\n"
            "Example: /fastenroll c62312911d752b8f2e11344377cc1bff 08:30:00\n"
            "Example with date: /fastenroll c62312911d752b8f2e11344377cc1bff 08:30:00 15.09.2026\n\n"
            "Tip: send ⚡ Fast Enroll from the menu for a guided step-by-step setup."
        )
        return

    sem_id = extract_sem_id(args[0])
    target_time_str = args[1]
    target_date_str = args[2] if len(args) == 3 else None

    reply = await schedule_fastenroll_job(chat_id, sem_id, target_time_str, target_date_str, context)
    await update.message.reply_text(reply)


async def handle_browse_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle browse_courses callback. If a default semester is set (via
    /status's "Set Default Semester"), skip straight to that semester's open
    courses; otherwise show a semester picker first, listing the semesters
    available in the student's own degree-programme module directory."""
    from unibot.menus import _show_semester_picker
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    default = load_default_semester()
    if default.get("id"):
        await _render_open_courses_for_semester(query, default["id"])
        return

    await _show_semester_picker(query, "course_sem", "📚 Pick a semester to see courses currently open for enrolment:")


async def handle_course_semester_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle course_sem|<id> callback — list this semester's currently
    open-for-enrolment courses within the student's own degree programme."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    semester_id = query.data.split("|", 1)[1]
    await _render_open_courses_for_semester(query, semester_id)


async def handle_toggle_prev_sem_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggle_prev_sem_scan|<semester_id> — flip the "include the
    previous semester too" setting (see get_scan_previous_semester_enabled)
    and re-render the same semester's open-courses view with the new
    setting applied."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    semester_id = query.data.split("|", 1)[1]
    set_scan_previous_semester_enabled(not get_scan_previous_semester_enabled())
    await _render_open_courses_for_semester(query, semester_id)


async def handle_course_enroll_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm before actually submitting an enrolment request."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    sem_id = query.data.split("|", 1)[1]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, enroll", callback_data=f"course_enroll_confirm|{sem_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="course_enroll_cancel"),
    ]])
    await query.message.reply_text("⚠️ Enroll in this course now?", reply_markup=keyboard)


async def handle_course_enroll_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    sem_id = query.data.split("|", 1)[1]
    status_msg = await query.edit_message_text("⏳ Enrolling...")
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Enrolling...")

    try:
        session = await login_studip()
        success, message = await enroll_now(session, sem_id)
    except Exception as e:
        logging.error("handle_course_enroll_confirm: enroll failed: %s", e)
        await query.message.reply_text(f"❌ Failed: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if success:
        await query.message.reply_text("✅ Enrolled successfully!")
    else:
        await query.message.reply_text(f"❌ {message}")


async def handle_course_enroll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    await query.edit_message_text("Cancelled.")


async def handle_browse_my_courses_for_deenroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle browse_my_courses_deenroll callback. If a default semester is
    set (via /status's "Set Default Semester"), skip straight to that
    semester's enrolled courses; otherwise show a semester picker first."""
    from unibot.menus import _show_semester_picker
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    default = load_default_semester()
    if default.get("id"):
        await _render_enrolled_courses_for_semester(query, default["id"])
        return

    await _show_semester_picker(query, "deenroll_sem", "🚪 Pick a semester to see its enrolled courses:")


async def handle_deenroll_semester_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle deenroll_sem|<id> callback — list that semester's currently
    enrolled courses so the student can pick one to sign out of."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    semester_id = query.data.split("|", 1)[1]
    await _render_enrolled_courses_for_semester(query, semester_id)


async def handle_course_deenroll_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm before actually signing the student out of a course."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    cid = query.data.split("|", 1)[1]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, sign out", callback_data=f"course_deenroll_confirm|{cid}"),
        InlineKeyboardButton("❌ Cancel", callback_data="course_deenroll_cancel"),
    ]])
    await query.message.reply_text("⚠️ Sign out of this course now? This withdraws your enrolment.", reply_markup=keyboard)


async def handle_course_deenroll_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    cid = query.data.split("|", 1)[1]
    status_msg = await query.edit_message_text("⏳ Signing out...")
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Signing out...")

    try:
        session = await login_studip()
        success, message = await decline_course(session, cid)
    except Exception as e:
        logging.error("handle_course_deenroll_confirm: decline failed: %s", e)
        await query.message.reply_text(f"❌ Failed: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if success:
        await query.message.reply_text("✅ Signed out of the course successfully!")
    else:
        await query.message.reply_text(f"❌ {message}")


async def handle_course_deenroll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    await query.edit_message_text("Cancelled.")


async def handle_enrollment_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle enrollment_menu callback (from /status) — submenu grouping the
    three course-enrollment-related actions that used to be separate
    top-level /status buttons."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    keyboard = [
        [InlineKeyboardButton("⚡ Fast Enroll", callback_data="fastenroll_menu")],
        [InlineKeyboardButton("➕ Enroll Course", callback_data="browse_courses")],
        [InlineKeyboardButton("🚪 Sign Out of a Course", callback_data="browse_my_courses_deenroll")],
    ]
    await query.message.reply_text("🎓 Course Enrollment:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_set_default_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle set_default_semester callback (from /status) — show a semester
    picker; picking one makes Files, Browse Courses (enroll), and Sign Out
    (deenroll) all skip straight to that semester instead of asking each time."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        session = await login_studip()
        semesters = await get_semester_options(session)
    except Exception as e:
        logging.error("handle_set_default_semester: failed to fetch semesters: %s", e)
        await query.message.reply_text(f"❌ Could not load semesters: {str(e)[:200]}")
        return

    if not semesters:
        await query.message.reply_text("❌ Could not find any semesters.")
        return

    keyboard = [[InlineKeyboardButton(label, callback_data=f"default_sem|{sid}")] for sid, label in semesters]
    current = load_default_semester()
    if current.get("id"):
        keyboard.append([InlineKeyboardButton("🗑️ Clear default (always ask)", callback_data="default_sem_clear")])
    await query.message.reply_text(
        "📆 Pick the default semester for Files, Browse Courses, and Sign Out:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_default_semester_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle default_sem|<id> callback — save the picked semester as the
    default used by Files, Browse Courses, and Sign Out."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    semester_id = query.data.split("|", 1)[1]
    try:
        session = await login_studip()
        semesters = await get_semester_options(session)
    except Exception as e:
        logging.error("handle_default_semester_pick: failed to fetch semesters: %s", e)
        await query.message.reply_text(f"❌ Error: {str(e)[:200]}")
        return

    label = next((lbl for sid, lbl in semesters if sid == semester_id), semester_id)
    save_default_semester(semester_id, label)
    await query.edit_message_text(f"✅ Default semester set to: {label}\nFiles, Browse Courses, and Sign Out will now use it directly.")


async def handle_default_semester_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle default_sem_clear callback — go back to asking for a semester
    every time in Files, Browse Courses, and Sign Out."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    clear_default_semester()
    await query.edit_message_text("✅ Default semester cleared. Files, Browse Courses, and Sign Out will ask each time again.")


async def handle_toggle_watch_prev_sem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggle_watch_prev_sem callback (from /status) — flip whether
    the background watchers (announcements, new files, forum posts) also
    monitor the previous semester's courses, not just the current one
    (see get_watch_previous_semester_enabled). Forces courses_map to be
    rebuilt on the next watcher cycle so the change takes effect without
    needing a bot restart."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    new_state = not get_watch_previous_semester_enabled()
    set_watch_previous_semester_enabled(new_state)
    state.courses_map = {}  # force _ensure_watched_courses_map to rebuild on the next watcher cycle

    if new_state:
        await query.edit_message_text("✅ Now also watching the previous semester's courses for new announcements, files, and forum posts — takes effect on the next check cycle.")
    else:
        await query.edit_message_text("🔲 Back to only watching the current semester's courses — takes effect on the next check cycle.")
FASTENROLL_ASK_TIME = "Please type the enrollment time (HH:MM:SS, Europe/Berlin):"
FASTENROLL_ASK_LINK = "Please paste the course link (or its sem_id):"


async def _handle_fastenroll_wizard_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str) -> bool:
    """If `step` is one of the fast-enroll wizard steps, handle it and return True."""
    state = context.user_data.setdefault("fastenroll_wizard", {})

    if step == "fastenroll_date":
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        lowered = text.lower()
        if lowered == "today":
            date_str = today.strftime("%d.%m.%Y")
        elif lowered == "tomorrow":
            date_str = (today + timedelta(days=1)).strftime("%d.%m.%Y")
        elif re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", text):
            date_str = text
        else:
            await update.message.reply_text(
                "Please use DD.MM.YYYY, 'today', or 'tomorrow'.",
                reply_markup=get_main_keyboard(),
            )
            return True  # pending_step stays "fastenroll_date" — ask again
        state["target_date_str"] = date_str
        user_data(context)["pending_step"] = "fastenroll_time"
        await update.message.reply_text(FASTENROLL_ASK_TIME, reply_markup=get_main_keyboard())
        return True

    if step == "fastenroll_time":
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", text):
            await update.message.reply_text(
                "Please use HH:MM:SS (Europe/Berlin), e.g. 08:30:00.",
                reply_markup=get_main_keyboard(),
            )
            return True  # pending_step stays "fastenroll_time" — ask again
        state["target_time_str"] = text
        user_data(context)["pending_step"] = "fastenroll_link"
        await update.message.reply_text(FASTENROLL_ASK_LINK, reply_markup=get_main_keyboard())
        return True

    if step == "fastenroll_link":
        sem_id = extract_sem_id(text)
        chat_id = update.effective_chat.id
        reply = await schedule_fastenroll_job(
            chat_id, sem_id, state.get("target_time_str"), state.get("target_date_str"), context
        )
        context.user_data.pop("fastenroll_wizard", None)
        context.user_data.pop("pending_step", None)
        await update.message.reply_text(reply, reply_markup=get_main_keyboard())
        return True

    return False


async def restore_pending_fastenroll_jobs(app):
    """Re-schedule fast-enroll jobs that were still pending when the bot last stopped
    (e.g. VPS reboot or pm2 restart), so a job scheduled for later is not silently lost."""
    pending = load_pending()
    for chat_id_str, jobs in pending.items():
        chat_id = int(chat_id_str)
        for sem_id, job in jobs.items():
            target_time_str = job.get("target_time_str")
            target_date_str = job.get("target_date_str")
            context = ContextTypes.DEFAULT_TYPE(application=app)
            logging.info("Restoring pending fastenroll job for chat %s: %s @ %s %s", chat_id, sem_id, target_date_str or '', target_time_str)
            try:
                await app.bot.send_message(chat_id=chat_id, text=f"🔄 Resuming previously scheduled fast enroll for course {sem_id} at {target_time_str}.")
            except Exception:
                logging.warning("Could not notify chat %s about resumed fastenroll job", chat_id, exc_info=True)
            task = asyncio.create_task(_run_fastenroll_task(chat_id, sem_id, target_time_str, target_date_str, context))
            fastenroll_tasks.setdefault(chat_id, {})[sem_id] = task
