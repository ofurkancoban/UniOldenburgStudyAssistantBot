"""The student's Privacy screen: their personal check times, a rate-limited
"check now", whether the assistant may use their grades, and deleting
everything personal the bot keeps about them."""

import logging
import time
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from exam_reminder import PRIVATE_EXAM_KIND, exam_cache_user, load_exam_cache
from fast_enroll import clear_pending_job
from studip_utils import TZ_BERLIN
from unibot import db, stagger, state
from unibot.auth import _perform_logout, acting_as, has_stored_login, is_user_allowed, personal_session
from unibot.exams import exam_action_cache
from unibot.schedule import fastenroll_tasks
from unibot.weather import forget_weather_place, load_weather_location
from unibot.storage import (
    forget_personal_cache,
    get_ai_grades_enabled,
    personal_record,
    save_personal_record,
    set_ai_grades_enabled,
)
from unibot.watcher import PERSONAL_LAST_KEY, _run_stums_checks, _user_sender, mark_personal_check

MANUAL_CHECK_KEY = "personal_check_manual"
MANUAL_CHECK_COOLDOWN_SECONDS = 3600


def _season(user_id: int, day) -> bool:
    try:
        registration_open = bool(load_exam_cache(user_id).get("notified_open"))
    except Exception:
        logging.debug("Exam cache unreadable; assuming no open registration", exc_info=True)
        registration_open = False
    return stagger.is_grade_season(day, registration_open)


def _fmt_ts(ts) -> str:
    if not isinstance(ts, (int, float)):
        return "not yet"
    return datetime.fromtimestamp(ts, TZ_BERLIN).strftime("%d.%m %H:%M")


def manual_check_wait(user_id: int, now_ts: Optional[float] = None) -> int:
    """Seconds until "Check now" may run again (0 when it may run)."""
    last = personal_record(user_id, MANUAL_CHECK_KEY)
    if not isinstance(last, (int, float)):
        return 0
    now_ts = now_ts if now_ts is not None else time.time()
    return max(0, int(last + MANUAL_CHECK_COOLDOWN_SECONDS - now_ts))


def privacy_view(user_id: int, now: Optional[datetime] = None, note: str = "") -> tuple:
    now = now or datetime.now(TZ_BERLIN)
    season = _season(user_id, now.date())
    times = stagger.describe_slots(user_id, now.date(), season)
    last = personal_record(user_id, PERSONAL_LAST_KEY)
    ai_on = get_ai_grades_enabled(user_id)
    mode = "exam season, 4 a day" if season else "2 a day"
    text = (
        "🔒 <b>Privacy</b>\n\n"
        f"🕒 Your grade, exam and fee checks today: <b>{times}</b> ({mode}, none 00:00 to 07:00)\n"
        f"✅ Last check: {_fmt_ts(last)}\n"
        f"🤖 Assistant may use your grades: {'✅ Yes' if ai_on else '🔲 No'}\n\n"
        "<i>Grades, exam registrations and fee status are stored encrypted with a key of your own and "
        "fetched only with your own login. Alerts about them go only to this chat.</i>"
    )
    if note:
        text += f"\n\n{note}"
    rows = [
        [InlineKeyboardButton("🔄 Check now", callback_data="privacy|check")],
        [InlineKeyboardButton("🤖 Don't let the assistant use my grades" if ai_on
                              else "🤖 Let the assistant use my grades", callback_data="privacy|ai")],
        [InlineKeyboardButton("🗑️ Delete my data", callback_data="privacy|delete")],
    ]
    return text, InlineKeyboardMarkup(rows)


def delete_confirm_view() -> tuple:
    text = (
        "🗑️ <b>Delete my data?</b>\n\n"
        "This removes:\n"
        "• your saved Stud.IP and StuMS login (you are signed out)\n"
        "• your stored grades, exam registration and fee data\n"
        "• your course alert settings, study block log, deadline streak, calendar link, "
        "Study Buddy and assistant settings, name, weather place and default semester\n"
        "• your activity log and the Telegram name kept with your account\n\n"
        "Your own tasks stay. Signing in again starts fresh. This cannot be undone."
    )
    rows = [[InlineKeyboardButton("🗑️ Yes, delete everything", callback_data="privacy|delete_yes")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="privacy")]]
    return text, InlineKeyboardMarkup(rows)


def _delete_plain_documents(user_id: int) -> list[str]:
    """Per-student documents from before the encrypted records (inbox,
    study progress) and the calendar reminder bookkeeping."""
    keys = [f"messages_cache_{user_id}.json", f"study_progress_{user_id}.json", f"reminders_cache_{user_id}.json"]
    removed = []
    for key in keys:
        try:
            if db.exists(key):
                db.delete(key)
                removed.append(key)
        except Exception:
            logging.warning("Could not delete a personal document", exc_info=True)
    return removed


async def delete_personal_data(user_id: int) -> dict:
    """Remove everything personal the bot keeps about the student."""
    removed: dict = {"private": db.get_user_scope(user_id).has_private(PRIVATE_EXAM_KIND)}
    place = load_weather_location(user_id)  # read before the record goes
    # everything private except their own tasks, which the confirmation keeps
    db.get_user_scope(user_id).delete_private(keep=(db.TASKS_KIND,))
    await _perform_logout(user_id)  # session, cookies, tokens, StuMS state
    db.get_user_scope(user_id).sync_courses(set())
    removed["cache_sections"] = forget_personal_cache(user_id)
    removed["documents"] = _delete_plain_documents(user_id)
    for task in (fastenroll_tasks.pop(user_id, None) or {}).values():
        task.cancel()  # a scheduled fast enroll would log in again for them
    clear_pending_job(user_id)
    for sid in [sid for sid, info in exam_action_cache.items() if info.get("user_id") == user_id]:
        exam_action_cache.pop(sid, None)
    for members in (getattr(state, "course_members", None) or {}).values():
        members.discard(user_id)
    db.forget_user_identity(user_id)  # activity log, Telegram name and username
    forget_weather_place(user_id, place)
    db.set_lifecycle(user_id, "inactive", "deleted their data")  # no background work until /start
    logging.info("Personal data deleted for user %s", user_id)
    return removed


async def run_manual_check(bot, user_id: int) -> str:
    """One personal check now, if signed in and not done in the last hour."""
    wait = manual_check_wait(user_id)
    if wait:
        return f"⏳ You can check again in {max(1, wait // 60)} min."
    session = personal_session(user_id)
    if not has_stored_login(session):
        return "🔐 Sign in first (/login), then your checks can run."
    save_personal_record(user_id, MANUAL_CHECK_KEY, time.time())
    with acting_as(user_id), exam_cache_user(user_id):
        await _run_stums_checks(bot, session, _user_sender(user_id))
    mark_personal_check(user_id)
    return "✅ Checked. Anything new was sent to you above; no message means nothing changed."


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop - pause everything the bot does in the background for this
    student (INACTIVE); their data stays, /start resumes."""
    user, message = update.effective_user, update.message
    if user is None or message is None or not is_user_allowed(user.id):
        return
    db.set_lifecycle(user.id, "inactive", "stopped with /stop")
    await message.reply_text(
        "⏸️ <b>Paused.</b> No more background checks, alerts, reminders or morning summaries.\n"
        "Your data and settings stay. Send /start to resume, or use 🔒 Privacy to delete everything.",
        parse_mode="HTML")


async def handle_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """privacy, privacy|check, privacy|ai, privacy|delete, privacy|delete_yes."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    if action == "delete":
        await query.answer()
        text, markup = delete_confirm_view()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return
    if action == "delete_yes":
        await query.answer("Deleting...")
        await delete_personal_data(user_id)
        await query.edit_message_text(
            "🗑️ <b>Your data was deleted.</b>\n\nYou are signed out. Use /login to start again.",
            parse_mode="HTML")
        return
    note = ""
    if action == "check":
        await query.answer("Checking...")
        try:
            note = await run_manual_check(context.bot, user_id)
        except Exception as e:
            logging.warning("Manual personal check failed for %s: %s", user_id, type(e).__name__)
            note = "⚠️ The check failed. Try again later."
    elif action == "ai":
        set_ai_grades_enabled(user_id, not get_ai_grades_enabled(user_id))
        await query.answer("Saved")
    else:
        await query.answer()
    text, markup = privacy_view(user_id, note=note)
    if action:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
