"""Unified background watcher loop and session keep-alive.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import random
import time
from datetime import datetime
from typing import Optional

from exam_reminder import (
    adopt_legacy_exam_cache,
    background_stums,
    exam_cache_user,
    load_exam_cache,
    check_exam_date_reminders,
    check_exam_reminders,
    check_grade_reminders,
    check_tuition_reminders,
    get_registered_exam_schedule,
)
from studip_utils import TZ_BERLIN, berlin_now

from unibot import db
from unibot import state
from unibot.config import (
    ALLOWED_USER_IDS,
    GWDG_API_KEY,
)
from unibot.storage import (
    get_weekly_study_plan_enabled,
    load_general_cache,
    save_general_cache,
    update_general_cache,
)
from unibot.telegram_ui import broadcast
from unibot.auth import act_as_bot, acting_as, get_user_session, has_stored_login, login_studip
from unibot.schedule import (
    check_calendar_reminders,
    check_course_schedule_changes,
    run_morning_summary_schedule,
)
from unibot.monitoring import (
    check_new_announcements_parallel,
    check_new_files_parallel,
    check_new_forum_posts_parallel,
    message_poll_loop,
    watched_courses,
)
from unibot.exams import (
    check_file_exam_reminders,
    exam_action_cache,
    notify_new_file_exam_events,
    scan_course_files_for_exams,
)
from unibot.study_plan import index_course_files, run_weekly_study_plan_job
from unibot.tasks import check_task_reminders
from unibot.system import HEALTH_CHECK_LABELS, run_health_checks


FILE_WATCHER_INTERVAL = 30 * 60


# ── unified watcher controller ─────────────────────────────────────────────────
# One background task runs every check in a fixed order. Each check is guarded
# on its own, so one failing subsystem (StuMS down, a broken AI key, ...) never
# stops the others; a failure is logged and retried on the next cycle.

EXAM_DATE_CHECK_INTERVAL_SECONDS = 3 * 3600  # upcoming exam dates change rarely
# StuMS checks are throttled: every check signs in to StuMS through the SSO,
# and a student account should not log in hundreds of times a day.
GRADE_CHECK_INTERVAL_SECONDS = 15 * 60
EXAM_REGISTRATION_CHECK_INTERVAL_SECONDS = 60 * 60
EXAM_REGISTRATION_OPEN_INTERVAL_SECONDS = 30 * 60  # while a registration window is open
TUITION_ATTEMPT_INTERVAL_SECONDS = 60 * 60  # check_tuition_reminders itself runs once a day
HEALTH_CHECK_INTERVAL_SECONDS = 30 * 60
# Pause between two students' StuMS checks, so N users never hit StuMS as
# one burst from the server's IP.
STUMS_USER_GAP_SECONDS = (4.0, 10.0)


async def _guarded(label: str, job, *args, start_message: Optional[str] = None, **kwargs):
    """Await job(*args, **kwargs); log "<label> failed" instead of raising."""
    try:
        if start_message:
            logging.info(start_message)
        return await job(*args, **kwargs)
    except Exception as e:
        logging.error("❌ %s failed: %s", label, e)
        return None


async def _run_once_per_period(cache_key: str, period: str, label: str, start_message: str, job, *args):
    """Run job once per period (a date or ISO week string).

    The period is recorded even when the job fails, so a broken dependency is
    retried in the next period instead of on every single watcher cycle.
    """
    if load_general_cache().get(cache_key) == period:
        return
    await _guarded(label, job, *args, start_message=start_message)
    cache = load_general_cache()
    cache[cache_key] = period
    save_general_cache(cache)


def _is_due(cache_key: str, now: datetime, interval_seconds: int) -> bool:
    """True when the ISO timestamp under cache_key is older than the interval."""
    last_iso = load_general_cache().get(cache_key)
    if not last_iso:
        return True
    try:
        return (now - datetime.fromisoformat(last_iso)).total_seconds() >= interval_seconds
    except ValueError:
        return True


async def _scan_and_announce_file_exams(bot, session):
    new_events = await scan_course_files_for_exams(session)
    await notify_new_file_exam_events(bot, new_events)


def _get_active_user_ids() -> list[int]:
    try:
        active = [u["telegram_id"] for u in db.get_all_users() if u.get("is_active", 1)]
        if active:
            return active
    except Exception:
        logging.debug("Ignored a non-critical error", exc_info=True)
    return list(ALLOWED_USER_IDS)


async def _run_weekly_study_plan_all_users(bot, active_uids, session):
    for uid in active_uids:
        try:
            user_session = get_user_session(uid) or session
            await run_weekly_study_plan_job(bot, [uid], user_session, user_id=uid)
        except Exception as e:
            logging.warning("Weekly study plan failed for %s: %s", uid, e)


async def _run_scheduled_jobs(bot, session, now: datetime):
    """Morning summary plus the weekly and daily background jobs."""
    # Morning summary: prepared ahead of time (AI lunch pick, outfit
    # image, voice), then sent all at once at MORNING_SUMMARY_HOUR.
    active_uids = _get_active_user_ids()
    await _guarded("Morning summary", run_morning_summary_schedule, bot, active_uids, now)

    # Weekly content-aware study plan (Sunday evening, opt-in toggle).
    if now.weekday() == 6 and now.hour >= 18 and get_weekly_study_plan_enabled():
        await _run_once_per_period(
            "last_weekly_study_plan", now.strftime("%G-W%V"), "Weekly study plan",
            "📘 Generating weekly study plan...", _run_weekly_study_plan_all_users, bot, active_uids, session,
        )

    # Exam dates from course files (syllabus, info sheets, slides): once a day.
    if now.hour >= EXAM_FILE_SCAN_HOUR:
        await _run_once_per_period(
            "last_exam_file_scan", now.date().isoformat(), "Exam file scan",
            "📄 Scanning course files for exam dates...", _scan_and_announce_file_exams, bot, session,
        )

    # Course material search index: once a day, only new/changed files.
    if GWDG_API_KEY and now.hour >= COURSE_INDEX_HOUR:
        await _run_once_per_period(
            "last_course_index", now.date().isoformat(), "Course file indexing",
            "🔎 Indexing course files for questions...", index_course_files, session,
        )


async def _send_calendar_reminders(bot):
    for uid in _get_active_user_ids():
        # a signed-out user has nothing to read; asking anyway only earns 401s
        if not has_stored_login(get_user_session(uid)):
            continue
        with acting_as(uid):
            await check_calendar_reminders(bot, uid, silent=True)


def _exam_registration_interval() -> int:
    """Shorter while StuMS has an open registration we have notified about."""
    return EXAM_REGISTRATION_OPEN_INTERVAL_SECONDS if load_exam_cache().get("notified_open") \
        else EXAM_REGISTRATION_CHECK_INTERVAL_SECONDS


async def _run_throttled(cache_key: str, now: datetime, interval: int, label: str, job, *args,
                         start_message=None, **kwargs):
    """Run a job when its interval has passed; the attempt is recorded even
    when it fails, so a broken StuMS is not retried on every cycle."""
    if not _is_due(cache_key, now, interval):
        return
    await _guarded(label, job, *args, start_message=start_message, **kwargs)
    update_general_cache({cache_key: now.isoformat()})


def _user_sender(user_id: int):
    """broadcast()-compatible sender that only reaches one student: StuMS data
    (registrations, grades, fees) is personal."""
    async def send(bot, text, parse_mode="HTML", disable_notification=True, reply_markup=None, cid=None):
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode=parse_mode,
                                   disable_notification=disable_notification, reply_markup=reply_markup)
        except Exception as e:
            logging.warning("Could not send StuMS alert to %s: %s", user_id, e)
    return send


def _stums_users() -> list:
    """(telegram_id, session) of every active student with a stored login."""
    users = []
    for uid in _get_active_user_ids():
        try:
            sess = get_user_session(uid)
        except Exception as e:
            logging.debug("No session for %s: %s", uid, e)
            continue
        if has_stored_login(sess):
            users.append((uid, sess))
    return users


async def _run_stums_checks(bot, session, now: datetime, cache_suffix: str = "", send=None):
    """Exam registrations, grades and fees for one StuMS account."""
    send = send or broadcast
    await _run_throttled(f"last_exam_registration_check{cache_suffix}", now, _exam_registration_interval(),
                         "Exam reminder check", check_exam_reminders, session, bot, send, exam_action_cache,
                         start_message="🎓 Checking exam registrations...")
    await _run_throttled(f"last_grade_check{cache_suffix}", now, GRADE_CHECK_INTERVAL_SECONDS, "Grade reminder check",
                         check_grade_reminders, session, bot, send,
                         start_message="🎓 Checking for new grades...")
    await _run_throttled(f"last_tuition_attempt{cache_suffix}", now, TUITION_ATTEMPT_INTERVAL_SECONDS,
                         "Tuition reminder check", check_tuition_reminders, session, bot, send,
                         start_message="💶 Checking tuition / fee status...")


async def _for_each_stums_user(job):
    """Run `job(uid, session)` for every signed-in student, one at a time and
    spaced out; each runs in that student's user and exam-cache context."""
    admin_id = next(iter(ALLOWED_USER_IDS), None)
    for index, (uid, sess) in enumerate(_stums_users()):
        if index:
            await asyncio.sleep(random.uniform(*STUMS_USER_GAP_SECONDS))
        if uid == admin_id:
            adopt_legacy_exam_cache(uid)
        with acting_as(uid), exam_cache_user(uid):
            await _guarded(f"StuMS checks for {uid}", job, uid, sess)


async def _run_reminder_checks(bot, session, now: datetime):
    """Calendar, schedule change, task, exam, grade and tuition reminders."""
    await _guarded("Calendar reminder check", _send_calendar_reminders, bot)
    # Automatic course cancellation & room change alerts
    await _guarded("Course schedule change check", check_course_schedule_changes, session, bot)
    # Personal task reminders (local JSON, no network calls needed)
    await _guarded("Task reminder check", check_task_reminders, bot)
    # StuMS: exam registrations, grades and fees, per student, each on its own interval
    if _stums_users():
        await _for_each_stums_user(
            lambda uid, sess: _run_stums_checks(bot, sess, now, f"_{uid}", _user_sender(uid))
        )
    else:
        # single-account setup without per-user logins
        await _run_stums_checks(bot, session, now)


async def _check_file_exam_reminders(bot, session):
    try:
        with background_stums():
            registered = await get_registered_exam_schedule(session)
    except Exception as e:
        log = logging.debug if getattr(e, "quiet", False) else logging.warning
        log("File exam reminders: registered schedule unavailable: %s", e)
        registered = []
    await check_file_exam_reminders(bot, registered)


async def _run_exam_date_checks(bot, session, now: datetime):
    """Upcoming exam dates (StuMS and course files), throttled to every 3 hours."""
    if not _is_due("last_exam_date_check", now, EXAM_DATE_CHECK_INTERVAL_SECONDS):
        return
    if _stums_users():
        await _for_each_stums_user(
            lambda uid, sess: check_exam_date_reminders(sess, bot, _user_sender(uid))
        )
    else:
        await _guarded("Exam date reminder check", check_exam_date_reminders, session, bot, broadcast,
                       start_message="🎓 Checking upcoming exam dates...")
    await _guarded("File exam reminder check", _check_file_exam_reminders, bot, session)
    update_general_cache({"last_exam_date_check": now.isoformat()})


async def _report_health_changes(bot):
    results = await run_health_checks(live_openrouter=False)
    prev_state = load_general_cache().get("health_state", {})
    new_state = {}
    for key, (ok, detail) in results.items():
        new_state[key] = ok
        # Assume healthy if never checked before, so the very first run
        # can't fire a false "broke" alert for a check that has simply
        # never run yet (e.g. OPENROUTER_API_KEY genuinely unset).
        was_ok = prev_state.get(key, True)
        if was_ok and not ok:
            await broadcast(bot, f"🔴 <b>Health check failed:</b> {HEALTH_CHECK_LABELS[key]}\n{html.escape(detail)}", parse_mode="HTML")
        elif not was_ok and ok:
            await broadcast(bot, f"🟢 <b>Recovered:</b> {HEALTH_CHECK_LABELS[key]} is back up.", parse_mode="HTML")
    update_general_cache({"health_state": new_state})


async def _run_health_check(bot, now: datetime):
    """Subsystem health check (Stud.IP/WhatsApp/weather, plus confirming the
    OpenRouter key is set; a live OpenRouter call is reserved for the
    on-demand /healthcheck command to save API quota). Runs every 30 minutes
    and only alerts on a state change (something broke, or recovered)."""
    if not _is_due("last_health_check", now, HEALTH_CHECK_INTERVAL_SECONDS):
        return
    await _guarded("Health check", _report_health_changes, bot)
    update_general_cache({"last_health_check": now.isoformat()})


async def _run_content_watchers(bot, admin_id, cycle_count: int, now: datetime):
    """Announcements, files and forum posts. Messages are polled separately
    every MESSAGE_POLL_SECONDS by message_poll_loop, not once per cycle."""
    # every signed-in user's courses, each fetched once with a participant's session
    try:
        courses = await watched_courses()
    except Exception as e:
        logging.error("❌ Could not build the course watch plan: %s", e)
        return
    await _guarded("Announcement check", check_new_announcements_parallel, bot, admin_id, silent=True,
                   courses=courses, start_message="📢 Checking announcements...")
    await asyncio.sleep(15)
    await _guarded("Forum check", check_new_forum_posts_parallel, bot, admin_id, silent=True,
                   courses=courses, start_message="💬 Checking forum posts...")
    await asyncio.sleep(15)
    # File checks recursively query folder hierarchies; throttle to once every 30 minutes.
    await _run_throttled("last_file_check", now, FILE_WATCHER_INTERVAL, "File check",
                         check_new_files_parallel, bot, admin_id, silent=True, courses=courses,
                         start_message="📁 Checking files...")


def _should_skip_cycle() -> bool:
    """Skip while paused, explicitly logged out, or while the user is actively
    using the bot (so background requests don't compete with theirs)."""
    session = state.global_session
    return session is not None and (
        session.explicitly_logged_out
        or time.time() - getattr(session, "last_interactive_activity", 0.0) < 180
    )


async def unified_watcher_controller(app):
    """Centrally manage all watchers - with a single task"""

    logging.info("🟢 Unified Watcher Controller STARTED")
    state.watcher_controller_running = True
    act_as_bot()  # this task acts for the bot itself, never for a user

    # Wait 2 minutes for initial check (for bot to fully start)
    await asyncio.sleep(120)

    # Determine admin user
    admin_id = next(iter(ALLOWED_USER_IDS), None)
    if not admin_id:
        logging.error("❌ No allowed users found for watcher")
        return

    bot = app.bot
    cycle_count = 0
    while True:
        try:
            if state.global_watcher_paused:
                logging.info("⏸️ Watcher controller paused")
                await asyncio.sleep(30)
                continue
            if _should_skip_cycle():
                await asyncio.sleep(30)
                continue

            try:
                session = await login_studip()
            except Exception as e:
                logging.error("❌ Login error in watcher: %s", e)
                session = None
            else:
                if not session:
                    logging.warning("🔑 Failed to acquire logged-in session, retrying...")
            if not session:
                await asyncio.sleep(60)
                continue

            cycle_count += 1
            logging.info("🔄 Watcher cycle #%s started", cycle_count)
            now = datetime.now(TZ_BERLIN)

            await _run_scheduled_jobs(bot, session, now)
            await _run_reminder_checks(bot, session, now)
            await _run_exam_date_checks(bot, session, now)
            await _run_health_check(bot, now)
            await _run_content_watchers(bot, admin_id, cycle_count, now)

            logging.info("✅ Watcher cycle #%s completed", cycle_count)
            state.global_scan_count += 1
            state.last_full_check_time = berlin_now()
            await asyncio.sleep(180)

        except Exception as e:
            logging.error("❌ Watcher controller error: %s", e)
            await asyncio.sleep(120)


SESSION_KEEPALIVE_SECONDS = 600  # 10 min keep-alive for OAuth + Stud.IP/NetIQ cookies


async def session_keepalive_loop():
    """Periodically refresh OAuth2 tokens and touch Stud.IP / NetIQ IDP cookies
    so sessions stay alive indefinitely without re-entering credentials."""
    act_as_bot()  # this task acts for the bot itself, never for a user
    while True:
        await asyncio.sleep(SESSION_KEEPALIVE_SECONDS + random.uniform(0, 60))
        sessions = []
        if state.global_session is not None:
            sessions.append(state.global_session)
        sessions += [s for s in state.user_sessions.values() if s is not None and s is not state.global_session]
        for index, sess in enumerate(sessions):
            if index:
                await asyncio.sleep(random.uniform(2.0, 6.0))  # never all users at once
            try:
                await sess.keep_alive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.debug("Session keep-alive error for %s: %s", getattr(sess, "user_id", None), exc)


async def start_unified_watcher(app):
    """Start the unified watcher"""

    if state.watcher_controller_running:
        logging.info("⚠️ Watcher controller already running")
        return

    logging.info("🚀 Starting unified watcher controller...")
    state.unified_watcher_task = asyncio.create_task(unified_watcher_controller(app))
    if state.message_poll_task is None or state.message_poll_task.done():
        state.message_poll_task = asyncio.create_task(message_poll_loop(app))
    if state.session_keepalive_task is None or state.session_keepalive_task.done():
        state.session_keepalive_task = asyncio.create_task(session_keepalive_loop())

    # Check initial state
    await asyncio.sleep(5)
    if state.watcher_controller_running:
        logging.info("✅ Unified watcher controller started successfully")
    else:
        logging.error("❌ Failed to start watcher controller")


async def stop_unified_watcher():
    """Stop the unified watcher"""

    for task in (state.unified_watcher_task, state.message_poll_task, state.session_keepalive_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    state.watcher_controller_running = False
    logging.info("🛑 Unified watcher controller stopped")
COURSE_INDEX_HOUR = 8  # daily, on the first watcher cycle from this hour on
EXAM_FILE_SCAN_HOUR = 7  # daily scan runs on the first watcher cycle from this hour on
