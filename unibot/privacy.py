"""The student's Privacy screen: their personal check times, a rate-limited
"check now", whether the assistant may use their grades, and deleting
everything personal the bot keeps about them."""

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from exam_reminder import PRIVATE_EXAM_KIND, exam_cache_user, load_exam_cache
from fast_enroll import clear_pending_job
from studip_utils import TZ_BERLIN
from unibot import course_registry, db, stagger, state
from unibot.auth import (
    _perform_logout,
    acting_as,
    forget_user_invite_failures,
    forget_user_runtime_state as forget_auth_runtime_state,
    has_stored_login,
    is_user_allowed,
    personal_session,
)
from unibot.exams import exam_action_cache
from unibot.schedule import fastenroll_tasks
from unibot.weather import forget_weather_place, load_weather_location
from unibot.storage import (
    forget_user_link_cache,
    forget_user_legacy_data,
    forget_personal_cache,
    get_ai_grades_enabled,
    get_ai_personal_enabled,
    personal_record,
    restore_pending_reminder_tasks,
    save_personal_record,
    set_ai_grades_enabled,
    set_ai_personal_enabled,
)
from unibot.watcher import PERSONAL_LAST_KEY, _run_stums_checks, _user_sender, mark_personal_check
from unibot.security_events import log_security_event

MANUAL_CHECK_KEY = "personal_check_manual"
MANUAL_CHECK_COOLDOWN_SECONDS = 3600
_manual_check_locks: dict[int, asyncio.Lock] = {}


def _valid_timestamp(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _season(user_id: int, day) -> bool:
    try:
        registration_open = bool(load_exam_cache(user_id).get("notified_open"))
    except Exception:
        logging.debug("Exam cache unreadable; assuming no open registration", exc_info=True)
        registration_open = False
    return stagger.is_grade_season(day, registration_open)


def _fmt_ts(ts) -> str:
    if not _valid_timestamp(ts):
        return "not yet"
    try:
        return datetime.fromtimestamp(ts, TZ_BERLIN).strftime("%d.%m %H:%M")
    except (OverflowError, OSError, ValueError):
        return "not yet"


def manual_check_wait(user_id: int, now_ts: Optional[float] = None) -> int:
    """Seconds until "Check now" may run again (0 when it may run)."""
    last = personal_record(user_id, MANUAL_CHECK_KEY)
    if not _valid_timestamp(last):
        return 0
    now_ts = now_ts if now_ts is not None else time.time()
    if last < 0 or last > now_ts + MANUAL_CHECK_COOLDOWN_SECONDS:
        return 0
    return max(0, int(last + MANUAL_CHECK_COOLDOWN_SECONDS - now_ts))


def privacy_view(user_id: int, now: Optional[datetime] = None, note: str = "") -> tuple:
    now = now or datetime.now(TZ_BERLIN)
    season = _season(user_id, now.date())
    times = stagger.describe_slots(user_id, now.date(), season)
    last = personal_record(user_id, PERSONAL_LAST_KEY)
    ai_on = get_ai_grades_enabled(user_id)
    personal_on = get_ai_personal_enabled(user_id)
    mode = "exam season, 4 a day" if season else "2 a day"
    text = (
        "🔒 <b>Privacy</b>\n\n"
        f"🕒 Your grade, exam and fee checks today: <b>{times}</b> ({mode}, none 00:00 to 07:00)\n"
        f"✅ Last check: {_fmt_ts(last)}\n"
        f"🤖 Assistant may use personal data for AI (timetable, tasks, exams, course-file summaries, study-plan topics, meal preferences, outfit suggestions, and course announcement or forum task suggestions): {'✅ Yes' if personal_on else '🔲 No'}\n"
        f"🤖 Assistant may use your grades: {'✅ Yes' if ai_on else '🔲 No'}\n\n"
        "<i>Grades, exam registrations and fee status are stored encrypted with a key of your own and "
        "fetched only with your own login. Alerts about them go only to this chat.</i>\n\n"
        "<i>The spoken morning summary, 🔊 Listen and spoken questions are made by outside AI services "
        "(GWDG, OpenRouter and, for wardrobe features, other image services; speech recognition may use Google). They use personal context, including your timetable, tasks, exams, first name, weather place, course-file text for requested summaries or study-plan topics, meal preferences for AI meal suggestions, clothing photos and your reference photo for outfit suggestions, only when you "
        "allow it. Course announcement or forum text is sent only after you tap its task-suggestion button and only while this permission is on; grades need their own permission.</i>"
    )
    if note:
        text += f"\n\n{note}"
    rows = [
        [InlineKeyboardButton("🔄 Check now", callback_data="privacy|check")],
        [InlineKeyboardButton("🤖 Don't let the assistant use my timetable" if personal_on
                              else "🤖 Let the assistant use my timetable", callback_data="privacy|ai_personal")],
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
        "• your activity log and the Telegram and Stud.IP names kept with your account\n\n"
        "Your own tasks stay. To start fresh afterward, send /start and then /login. This cannot be undone."
    )
    rows = [[InlineKeyboardButton("🗑️ Yes, delete everything", callback_data="privacy|delete_yes")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data="privacy")]]
    return text, InlineKeyboardMarkup(rows)


def _delete_plain_documents(user_id: int) -> list[str]:
    """Per-student documents from before the encrypted records (inbox,
    study progress) and the calendar reminder bookkeeping."""
    # The inbox migration also used the shared legacy filename for the
    # watcher account. Include it when this is that account, or it can be
    # imported back into the encrypted record after deletion.
    from unibot.monitoring import _legacy_message_keys
    keys = [*_legacy_message_keys(user_id), f"study_progress_{user_id}.json",
            f"reminders_cache_{user_id}.json", f"exam_reminder_cache_{user_id}.json"]
    removed = []
    failed = []
    for key in keys:
        try:
            had_data = db.exists(key) or db.has_imported_backup(key)
            if had_data:
                db.delete(key)
                removed.append(key)
            if db.exists(key) or db.has_imported_backup(key):
                raise RuntimeError(f"personal document {key} remains after deletion")
        except Exception:
            logging.warning("Could not delete a personal document", exc_info=True)
            failed.append(key)
    if failed:
        raise RuntimeError(f"Could not delete personal documents: {', '.join(failed)}")
    return removed


async def delete_personal_data(user_id: int) -> dict:
    """Wait for a manual check to finish, then remove the student's data."""
    uid = int(user_id)
    lock = _manual_check_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        return await _delete_personal_data_locked(uid)


async def _delete_personal_data_locked(user_id: int) -> dict:
    # Fired reminders are kept as callback actions after they leave the normal
    # task list. Restore them before deletion invalidates private writes, since
    # the user explicitly keeps tasks when the rest of their data is removed.
    with acting_as(user_id):
        restore_pending_reminder_tasks(user_id)
    db.begin_private_deletion(user_id)
    try:
        return await _delete_personal_data_body(user_id)
    finally:
        db.finish_private_deletion(user_id)


async def _delete_personal_data_body(user_id: int) -> dict:
    """Remove everything personal the bot keeps about the student."""
    # Invalidate writes from user-scoped work already in flight, then pause
    # future watcher selection before the first await.
    db.set_lifecycle(user_id, "inactive", "deleted their data")
    removed: dict = {"private": db.get_user_scope(user_id).has_private(PRIVATE_EXAM_KIND)}
    place = load_weather_location(user_id)  # read before the record goes
    # everything private except their own tasks, which the confirmation keeps
    db.get_user_scope(user_id).delete_private(keep=(db.TASKS_KIND,))
    # Resolve the legacy shared inbox while this user can still be identified
    # as the watcher; logout may immediately select a different admin session.
    removed["documents"] = _delete_plain_documents(user_id)
    await _perform_logout(user_id)  # session, cookies, tokens, StuMS state
    db.forget_user_courses(user_id)
    removed["cache_sections"] = forget_personal_cache(user_id)
    removed["legacy_data"] = forget_user_legacy_data(user_id)
    removed["file_links"] = forget_user_link_cache(user_id)
    forget_user_invite_failures(user_id)
    # These navigation and onboarding selections are process memory, but may
    # still hold the student's course URLs and names after persistent data is gone.
    from unibot.menus import nav_stack
    from unibot.menus import user_courses
    from unibot.monitoring import forget_user_runtime_state as forget_monitoring_runtime_state
    from unibot.morning import forget_prepared_morning_user
    from unibot.onboarding import _setup_offered, _setup_semesters
    from unibot.telegram_ui import nav_names
    nav_stack.pop(user_id, None)
    user_courses.pop(user_id, None)
    nav_names.pop(user_id, None)
    _setup_semesters.pop(user_id, None)
    _setup_offered.pop(user_id, None)
    forget_prepared_morning_user(user_id)
    forget_auth_runtime_state(user_id)
    forget_monitoring_runtime_state(user_id)
    course_registry.forget_user(user_id)
    for task in (fastenroll_tasks.pop(user_id, None) or {}).values():
        task.cancel()  # a scheduled fast enroll would log in again for them
    clear_pending_job(user_id)
    for sid in [sid for sid, info in exam_action_cache.items() if info.get("user_id") == user_id]:
        exam_action_cache.pop(sid, None)
    for members in (getattr(state, "course_members", None) or {}).values():
        members.discard(user_id)
    db.forget_user_identity(user_id)  # activity log, Telegram name and username
    forget_weather_place(user_id, place)
    # Existing full-database snapshots would otherwise retain this student's
    # deleted records on the VPS. Replace the old history with one snapshot
    # of the now-sanitized database before confirming deletion.
    await asyncio.to_thread(db.backup, keep=1)
    logging.info("Personal data deleted for user %s", user_id)
    return removed


async def run_manual_check(bot, user_id: int) -> str:
    """One personal check now, if signed in and not done in the last hour."""
    uid = int(user_id)
    lock = _manual_check_locks.setdefault(uid, asyncio.Lock())
    async with lock:
        # The user may have been blocked while waiting behind another check.
        if not is_user_allowed(uid):
            return "⛔ This account no longer has access to the bot."
        # Recheck after waiting: another callback may have completed the check
        # while this one was queued on the per-user lock.
        wait = manual_check_wait(uid)
        if wait:
            return f"⏳ You can check again in {max(1, wait // 60)} min."
        session = personal_session(uid)
        if not has_stored_login(session):
            return "🔐 Sign in first (/login), then your checks can run."
        with acting_as(uid), exam_cache_user(uid):
            completed = await _run_stums_checks(bot, session, _user_sender(uid))
        if not completed:
            return "⏸️ The check was interrupted because your account was paused. Resume with /start to try again."
        # A failed fetch or delivery must not consume the user's cooldown.
        save_personal_record(uid, MANUAL_CHECK_KEY, time.time())
        mark_personal_check(uid)
        return "✅ Checked. Anything new was sent to you above; no message means nothing changed."


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop - pause everything the bot does in the background for this
    student (INACTIVE); their data stays, /start resumes."""
    user, message = update.effective_user, update.message
    if user is None or message is None or not is_user_allowed(user.id):
        return
    db.set_lifecycle(user.id, "inactive", "stopped with /stop")
    # Fast Enroll is a user-triggered background task too. Remove its
    # persisted schedule before cancelling the coroutine, whose cancellation
    # handler deliberately keeps schedules across process restarts.
    clear_pending_job(user.id)
    for task in (fastenroll_tasks.pop(user.id, None) or {}).values():
        task.cancel()
    await message.reply_text(
        "⏸️ <b>Paused.</b> No more background checks, alerts, reminders or morning summaries.\n"
        "Your data and settings stay. Send /start to resume, or use 🔒 Privacy to delete everything.",
        parse_mode="HTML")


async def handle_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """privacy, privacy|check, privacy|ai, privacy|ai_personal, privacy|delete, privacy|delete_yes."""
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
        try:
            await delete_personal_data(user_id)
        except Exception:
            log_security_event("personal_data_deletion", actor_id=user_id, outcome="failed")
            logging.error("Personal data deletion did not complete for user %s", user_id, exc_info=True)
            text, markup = delete_confirm_view()
            await query.edit_message_text(
                "⚠️ Deletion could not be completed. Some data may already be gone. "
                "Review the confirmation and try again.\n\n" + text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return
        log_security_event("personal_data_deletion", actor_id=user_id)
        if context.user_data is not None:
            for key in ("pending_step", "task_wizard", "fastenroll_wizard", "studip_reply_target",
                        "pending_voice", "pending_voice_clarify"):
                context.user_data.pop(key, None)
        await query.edit_message_text(
            "🗑️ <b>Your data was deleted.</b>\n\nYou are signed out. Send /start, then /login to start again.",
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
    elif action == "ai_personal":
        set_ai_personal_enabled(user_id, not get_ai_personal_enabled(user_id))
        await query.answer("Saved")
    else:
        await query.answer()
    text, markup = privacy_view(user_id, note=note)
    if action:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except BadRequest as e:
            # a second tap inside the cooldown renders the same view again
            if "not modified" not in str(e).lower():
                raise
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
