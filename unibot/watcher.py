"""Unified background watcher loop and session keep-alive.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import contextvars
import html
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional

from exam_reminder import (
    adopt_legacy_exam_cache,
    exam_cache_user,
    load_exam_cache,
    check_exam_date_reminders,
    check_exam_reminders,
    check_grade_reminders,
    check_tuition_reminders,
)
from studip_utils import TZ_BERLIN, berlin_now

from unibot import course_registry, db, stagger
from unibot import state
from unibot.config import (
    ADMIN_USER_IDS,
    ALLOWED_USER_IDS,
    PRIMARY_ADMIN_ID,
    GWDG_API_KEY,
    LOG_FILE,
)
from unibot.diagnostics import daily_digest, traffic_snapshot
from unibot.preferences import get_pref
from unibot.storage import (
    get_ai_personal_enabled,
    load_general_cache,
    personal_record,
    save_personal_record,
    save_general_cache,
    student_courses,
    update_general_cache,
)
from unibot.telegram_ui import broadcast, markup_for
from unibot.auth import (
    ACTIVE,
    NEEDS_REAUTH,
    act_as_bot,
    acting_as,
    all_sessions,
    get_user_session,
    has_stored_login,
    is_user_allowed,
    lifecycle_state,
    login_studip,
    send_reauth_notice,
    signed_in_accounts,
)
from unibot.schedule import (
    check_calendar_reminders,
    check_course_session_changes,
    check_personal_timetable_changes,
    run_morning_summary_schedule,
)
from unibot.monitoring import (
    check_new_announcements_parallel,
    check_new_files_parallel,
    check_new_forum_posts_parallel,
    message_poll_loop,
    watched_courses,
)
from unibot.exams import exam_action_cache
from unibot.study_plan import index_course_files, prune_course_index, run_weekly_study_plan_job
from unibot.tasks import check_task_reminders
from unibot.study_week import weekly_report, weekly_report_due
from unibot.system import HEALTH_CHECK_LABELS, run_health_checks


FILE_WATCHER_INTERVAL = 30 * 60


def _cache_datetime(value) -> Optional[datetime]:
    """Read cached timestamps written by both old naive and current aware code."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TZ_BERLIN)
    return parsed.astimezone(TZ_BERLIN)


# ── unified watcher controller ─────────────────────────────────────────────────
# One background task runs every check in a fixed order. Each check is guarded
# on its own, so one failing subsystem (StuMS down, a broken AI key, ...) never
# stops the others; a failure is logged and retried on the next cycle.

EXAM_DATE_CHECK_INTERVAL_SECONDS = 3 * 3600  # upcoming exam dates change rarely
# Grades, exam registrations and fees: each student at their own staggered
# times per day (unibot.stagger), so StuMS sees no login bursts.
HEALTH_CHECK_INTERVAL_SECONDS = 30 * 60
# Admin daily digest: sent on the first watcher cycle from this hour on.
DAILY_DIGEST_HOUR = 9
# Daily database backup, at night when the bot is quiet.
BACKUP_HOUR = 3
# Pause between two students' StuMS checks, so N users never hit StuMS as
# one burst from the server's IP.
STUMS_USER_GAP_SECONDS = (4.0, 10.0)
_cycle_failures: contextvars.ContextVar[Optional[list[str]]] = contextvars.ContextVar(
    "watcher_cycle_failures", default=None,
)
JOB_TIMEOUT_SECONDS = 300


async def _guarded(label: str, job, *args, start_message: Optional[str] = None, **kwargs):
    """Await job(*args, **kwargs); log "<label> failed" instead of raising."""
    try:
        if start_message:
            logging.info(start_message)
        timeout = 900 if label == "Scheduled jobs" else JOB_TIMEOUT_SECONDS
        result = await asyncio.wait_for(job(*args, **kwargs), timeout)
        if result is None and label in {"Announcement check", "Forum check", "File check"}:
            failures = _cycle_failures.get()
            if failures is not None:
                failures.append(label)
        return result
    except Exception as e:
        failures = _cycle_failures.get()
        if failures is not None:
            failures.append(label)
        logging.error("❌ %s failed: %s", label, e)
        return None


async def _run_once_per_period(cache_key: str, period: str, label: str, start_message: str, job, *args):
    """Run job once per period (a date or ISO week string).

    Failures are retried after a short cooldown. Success is recorded for the
    whole period so a completed job does not run on every watcher cycle.
    """
    cache = load_general_cache()
    if cache.get(cache_key) == period:
        return
    retry_after = cache.get(f"{cache_key}_retry_after")
    if retry_after:
        retry_dt = _cache_datetime(retry_after)
        now = berlin_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=TZ_BERLIN)
        if retry_dt is not None and retry_dt > now:
            return
    try:
        if start_message:
            logging.info(start_message)
        await asyncio.wait_for(job(*args), 900)
    except Exception as e:
        logging.error("❌ %s failed: %s", label, e)
        cache = load_general_cache()
        cache[f"{cache_key}_retry_after"] = (berlin_now() + timedelta(minutes=30)).isoformat()
        save_general_cache(cache)
        return
    cache = load_general_cache()
    cache[cache_key] = period
    cache.pop(f"{cache_key}_retry_after", None)
    save_general_cache(cache)


def _is_due(cache_key: str, now: datetime, interval_seconds: int) -> bool:
    """True when the ISO timestamp under cache_key is older than the interval."""
    last_iso = load_general_cache().get(cache_key)
    if not last_iso:
        return True
    last_dt = _cache_datetime(last_iso)
    if last_dt is None:
        return True
    now_dt = now.replace(tzinfo=TZ_BERLIN) if now.tzinfo is None else now.astimezone(TZ_BERLIN)
    return (now_dt - last_dt).total_seconds() >= interval_seconds


def _get_active_user_ids() -> list[int]:
    """Students the per-user background jobs (morning summary, calendar
    reminders, weekly plan) run for: ACTIVE only. An expired (NEEDS_REAUTH)
    session would read an empty timetable and the summary would claim a free
    day; those students get their sign-in prompt instead. The .env list is
    only a fallback for an install with no users in the database."""
    try:
        users = db.get_all_users()
    except Exception:
        logging.warning("User list unavailable for the background jobs", exc_info=True)
        return []
    if not users:
        return list(ALLOWED_USER_IDS)
    return [u["telegram_id"] for u in users
            if u.get("is_active", 1) and is_user_allowed(u["telegram_id"])
            and lifecycle_state(u["telegram_id"]) == ACTIVE]


async def _run_weekly_study_plan_all_users(bot, active_uids, session, period: str):
    progress_key = f"weekly_study_plan_delivery:{period}"
    sent_ids = set(load_general_cache().get(progress_key) or [])
    failed = []
    for uid in active_uids:
        if uid in sent_ids:
            continue
        if not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE:
            continue
        try:
            user_session = get_user_session(uid) or session
            with acting_as(uid):
                result = await run_weekly_study_plan_job(bot, [uid], user_session, user_id=uid)
                if (result is None or not db.private_context_is_current(uid)
                        or not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE):
                    continue
            sent_ids.add(uid)
            update_general_cache({progress_key: sorted(sent_ids)})
        except Exception as e:
            logging.warning("Weekly study plan failed for %s: %s", uid, e)
            failed.append(uid)
    if failed:
        raise RuntimeError(f"Weekly study plan failed for {len(failed)} user(s)")
    cache = load_general_cache()
    cache.pop(progress_key, None)
    save_general_cache(cache)


async def _run_scheduled_jobs(bot, session, now: datetime):
    """Morning summary plus the weekly and daily background jobs."""
    # Morning summary: prepared ahead of time (AI lunch pick, outfit
    # image, voice), then sent all at once at MORNING_SUMMARY_HOUR.
    active_uids = _get_active_user_ids()
    await _guarded("Morning summary", run_morning_summary_schedule, bot, active_uids, now)

    # Weekly content-aware study plan (Sunday evening, opt-in toggle).
    plan_uids = [uid for uid in active_uids if get_pref(uid, "weekly_study_plan")]
    # Sunday evening, or Monday morning when the bot was down then: the
    # period is the week of that Sunday, so a Monday run uses yesterday's
    sunday = now if now.weekday() == 6 else now - timedelta(days=1)
    plan_due = (now.weekday() == 6 and now.hour >= 18) or (now.weekday() == 0 and now.hour < 12)
    if plan_due and plan_uids:
        period = sunday.strftime("%G-W%V")
        await _run_once_per_period(
            "last_weekly_study_plan", period, "Weekly study plan",
            "📘 Generating weekly study plan...", _run_weekly_study_plan_all_users,
            bot, plan_uids, session, period,
        )

    # Course material search index: once a day, only new/changed files.
    if GWDG_API_KEY and now.hour >= COURSE_INDEX_HOUR:
        await _run_once_per_period(
            "last_course_index", now.date().isoformat(), "Course file indexing",
            "🔎 Indexing course files for questions...", _index_course_files_for_users, session,
        )


async def _index_course_files_for_users(fallback_session=None) -> dict:
    """Index enrolled course material only for active students who enabled
    personal AI use. The indexer requires an explicit owner for that consent
    check, so a watcher-wide call with user_id=None would silently do nothing."""
    combined = {"indexed": 0, "chunks": 0, "removed": 0}
    keep_course_ids: set[str] = set()
    for uid in _get_active_user_ids():
        if get_ai_personal_enabled(uid):
            keep_course_ids.update(student_courses(uid))
    for account in signed_in_accounts():
        uid = account.user_id
        if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                or not get_ai_personal_enabled(uid)):
            continue
        if uid != getattr(account.session, "user_id", None):
            logging.warning("Skipped course indexing for %s: session owner mismatch", uid)
            continue
        with acting_as(uid):
            result = await index_course_files(
                account.session or fallback_session, user_id=uid, prune_unenrolled=False,
            )
        for key in combined:
            combined[key] += int(result.get(key, 0))
    combined["removed"] += await prune_course_index(keep_course_ids)
    return combined


async def _send_calendar_reminders(bot):
    async def send_one(uid):
        # a signed-out user has nothing to read; asking anyway only earns 401s
        if not has_stored_login(get_user_session(uid)):
            return
        try:
            with acting_as(uid):
                await asyncio.wait_for(check_calendar_reminders(bot, uid, silent=True), 60)
        except Exception as exc:
            failures = _cycle_failures.get()
            if failures is not None:
                failures.append(f"Calendar reminders for {uid}")
            logging.warning("Calendar reminders failed for %s; continuing with other users: %s", uid, exc)

    await asyncio.gather(*(send_one(uid) for uid in _get_active_user_ids()))


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
        with db.private_write_context(user_id):
            # A fetch can outlive /stop or data deletion. Recheck at delivery
            # time so stale work cannot send personal results afterward.
            if (not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
                    or lifecycle_state(user_id) != ACTIVE):
                return False
            return await bot.send_message(chat_id=user_id, text=text, parse_mode=parse_mode,
                                          disable_notification=disable_notification,
                                          reply_markup=markup_for(user_id, reply_markup))
    return send


def _stums_users() -> list:
    """The Accounts of every active student with a stored login."""
    return signed_in_accounts()


async def _run_stums_checks(bot, session, send) -> bool:
    """Exam registrations, grades and fees for one StuMS account, sent only
    to that student. check_tuition_reminders limits itself to once a day."""
    checks = (
        ("Exam reminder check", check_exam_reminders,
         (session, bot, send, exam_action_cache), "🎓 Checking exam registrations..."),
        ("Grade reminder check", check_grade_reminders,
         (session, bot, send), "🎓 Checking for new grades..."),
        ("Tuition reminder check", check_tuition_reminders,
         (session, bot, send), "💶 Checking tuition / fee status..."),
    )
    failed = []
    user_id = getattr(session, "user_id", None)
    for label, job, args, start_message in checks:
        if user_id is not None and (
            not db.private_context_is_current(user_id) or not is_user_allowed(user_id)
            or lifecycle_state(user_id) != ACTIVE
        ):
            return False
        result = await _guarded(label, job, *args, start_message=start_message)
        if user_id is not None and not db.private_context_is_current(user_id):
            return False
        # The reminder checks now return True only after fetching and
        # delivering successfully. _guarded returns None when one raises;
        # treating that as success would consume the student's daily slot.
        if result is not True:
            failed.append(label)
    if failed:
        raise RuntimeError("StuMS check delivery or fetch failed: " + ", ".join(failed))
    return True


PERSONAL_SLOTS_KEY = "personal_check_slots"
PERSONAL_RETRY_KEY = "personal_check_retry_after"


def _personal_due(uid: int, now: datetime) -> Optional[str]:
    """The student's due check slot (stagger.due_slot), or None."""
    retry_after = personal_record(uid, PERSONAL_RETRY_KEY, 0)
    try:
        if float(retry_after) > now.timestamp():
            return None
    except (TypeError, ValueError):
        pass
    done = personal_record(uid, PERSONAL_SLOTS_KEY)
    try:
        registration_open = bool(load_exam_cache(uid).get("notified_open"))
    except Exception:
        logging.debug("Exam cache unreadable; assuming no open registration", exc_info=True)
        registration_open = False
    return stagger.due_slot(uid, now, done, stagger.is_grade_season(now.date(), registration_open))


PERSONAL_LAST_KEY = "personal_check_last"


def mark_personal_check(uid: int, slot: Optional[str] = None, ts: Optional[float] = None) -> None:
    """Record a personal check of the student: the slot it covered (if a
    scheduled one) and when it ran."""
    if slot is not None:
        save_personal_record(uid, PERSONAL_SLOTS_KEY, slot)
    save_personal_record(uid, PERSONAL_LAST_KEY, ts if ts is not None else time.time())


def _mark_personal_slot(uid: int, slot: str) -> None:
    mark_personal_check(uid, slot)


async def _for_each_stums_user(job, accounts=None):
    """Run `job(uid, session)` for every signed-in student (or `accounts`),
    one at a time and spaced out; each runs in that student's user and
    exam-cache context."""
    admin_id = PRIMARY_ADMIN_ID
    succeeded = True
    for index, account in enumerate(_stums_users() if accounts is None else accounts):
        uid, sess = account.user_id, account.session
        if index:
            await asyncio.sleep(random.uniform(*STUMS_USER_GAP_SECONDS))
        # The account list may have been built before this user stopped or
        # deleted their data while another student's check was running.
        if not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE:
            continue
        if uid == admin_id:
            adopt_legacy_exam_cache(uid)
        with acting_as(uid), exam_cache_user(uid):
            result = await _guarded(f"StuMS checks for {uid}", job, uid, sess)
            if result is not True:
                succeeded = False
    return succeeded


def _single_account_owner(session) -> Optional[int]:
    """Whose StuMS data the single-account setup reads: the watcher session's
    student, else the primary admin."""
    return getattr(session, "user_id", None) or PRIMARY_ADMIN_ID


REAUTH_NOTIFIED_KEY = "reauth_notified"


async def _notify_reauth_needed(bot) -> int:
    """Each student who just moved to NEEDS_REAUTH gets one sign-in prompt
    (again only after the next expiry). Returns how many were told."""
    notified = dict(load_general_cache().get(REAUTH_NOTIFIED_KEY) or {})
    told = 0
    for uid in db.users_in_lifecycle(NEEDS_REAUTH):
        if not is_user_allowed(uid):
            continue
        changed_at = db.get_lifecycle(uid)["changed_at"]
        if notified.get(str(uid)) == changed_at:
            continue
        try:
            await send_reauth_notice(bot, uid)
            told += 1
            notified[str(uid)] = changed_at
        except Exception as e:
            logging.warning("Sign-in prompt for %s failed: %s", uid, e)
        await asyncio.sleep(0.3)
    if told:
        update_general_cache({REAUTH_NOTIFIED_KEY: notified})
    return told


async def _run_reminder_checks(bot, session, now: datetime):
    """Calendar, schedule change, task, exam, grade and tuition reminders."""
    await _guarded("Sign-in prompts", _notify_reauth_needed, bot)
    # course alerts held for quiet hours or the evening digest go out when due
    await _guarded("Course digest", course_registry.flush_alert_queues, bot, now)
    # Cancelled, moved or rescheduled sessions: once per course, to its students only
    await _guarded("Course session change check", check_course_session_changes, bot)
    # Each student's own weekly timetable (added, moved or removed courses)
    await _guarded("Timetable change check", check_personal_timetable_changes, bot)
    # Personal task reminders (local JSON, no network calls needed)
    # StuMS: exam registrations, grades and fees, each student at their own
    # staggered times (unibot.stagger), never at night
    if _stums_users():
        due = [(account, slot) for account in _stums_users()
               if (slot := _personal_due(account.user_id, now)) is not None]
        slots = {account.user_id: slot for account, slot in due}

        async def job(uid, sess):
            try:
                completed = await _run_stums_checks(bot, sess, _user_sender(uid))
            except Exception:
                save_personal_record(uid, PERSONAL_RETRY_KEY, time.time() + 30 * 60)
                raise
            if not completed:
                return False  # user stopped/deleted their data while the check was in flight
            save_personal_record(uid, PERSONAL_RETRY_KEY, 0)
            _mark_personal_slot(uid, slots[uid])
            return True

        if due:
            await _for_each_stums_user(job, [account for account, _slot in due])
    else:
        # single-account setup without per-user logins: still that one student's data only
        owner = _single_account_owner(session) if session else None
        if owner is None or not is_user_allowed(owner) or lifecycle_state(owner) != ACTIVE:
            return
        adopt_legacy_exam_cache(owner)
        slot = _personal_due(owner, now)
        if slot is not None:
            # that student's own exam cache (encrypted record), like every other student's
            try:
                with acting_as(owner), exam_cache_user(owner):
                    completed = await _run_stums_checks(bot, session, _user_sender(owner))
            except Exception:
                save_personal_record(owner, PERSONAL_RETRY_KEY, time.time() + 30 * 60)
                logging.warning("StuMS check failed for %s; retrying after cooldown", owner, exc_info=True)
            else:
                if completed:
                    save_personal_record(owner, PERSONAL_RETRY_KEY, 0)
                    _mark_personal_slot(owner, slot)


async def _run_exam_date_checks(bot, session, now: datetime):
    """Upcoming exam dates from StuMS, throttled to every 3 hours."""
    cache = load_general_cache()
    retry_after = cache.get("exam_date_check_retry_after")
    if retry_after:
        retry_dt = _cache_datetime(retry_after)
        now_dt = now.replace(tzinfo=TZ_BERLIN) if now.tzinfo is None else now.astimezone(TZ_BERLIN)
        if retry_dt is not None and retry_dt > now_dt:
            return
    if stagger.is_night(now) or not _is_due("last_exam_date_check", now, EXAM_DATE_CHECK_INTERVAL_SECONDS):
        return
    success = True
    if _stums_users():
        success = await _for_each_stums_user(
            lambda uid, sess: check_exam_date_reminders(sess, bot, _user_sender(uid))
        )
    else:
        owner = _single_account_owner(session)
        if owner is not None and is_user_allowed(owner) and lifecycle_state(owner) == ACTIVE:
            if session is None:
                success = False
            else:
                adopt_legacy_exam_cache(owner)
                with acting_as(owner), exam_cache_user(owner):
                    result = await _guarded("Exam date reminder check", check_exam_date_reminders, session, bot,
                                            _user_sender(owner), start_message="🎓 Checking upcoming exam dates...")
                if result is not True:
                    success = False
    if success:
        cache = load_general_cache()
        cache["last_exam_date_check"] = now.isoformat()
        cache.pop("exam_date_check_retry_after", None)
    else:
        cache = load_general_cache()
        cache["exam_date_check_retry_after"] = (now + timedelta(minutes=30)).isoformat()
    save_general_cache(cache)


async def _report_health_changes(bot):
    results = await run_health_checks(live_openrouter=False)
    prev_state = load_general_cache().get("health_state", {})
    new_state = {}
    admin_ids = set(ADMIN_USER_IDS)
    try:
        admin_ids.update(
            user["telegram_id"]
            for user in db.get_all_users()
            if user.get("role") == "admin" and user.get("is_active", True)
        )
    except Exception:
        logging.warning("Could not load database administrators for health alerts", exc_info=True)
    for key, (ok, detail) in results.items():
        new_state[key] = ok
        # Assume healthy if never checked before, so the very first run
        # can't fire a false "broke" alert for a check that has simply
        # never run yet (e.g. OPENROUTER_API_KEY genuinely unset).
        was_ok = prev_state.get(key, True)
        if was_ok and not ok:
            delivery = await broadcast(
                bot, f"🔴 <b>Health check failed:</b> {HEALTH_CHECK_LABELS[key]}\n{html.escape(detail)}",
                parse_mode="HTML", members=admin_ids,
            )  # server internals: admins only
        elif not was_ok and ok:
            delivery = await broadcast(
                bot, f"🟢 <b>Recovered:</b> {HEALTH_CHECK_LABELS[key]} is back up.",
                parse_mode="HTML", members=admin_ids,
            )
        else:
            continue
        if delivery["failed"]:
            # Leave the old state in place so this transition is retried on a
            # later health cycle instead of being permanently consumed.
            raise RuntimeError(f"Health alert failed for {len(delivery['failed'])} admin(s)")
    update_general_cache({"health_state": new_state})
    return True


async def _run_health_check(bot, now: datetime):
    """Subsystem health check (Stud.IP/WhatsApp/weather, plus confirming the
    OpenRouter key is set; a live OpenRouter call is reserved for the
    on-demand /healthcheck command to save API quota). Runs every 30 minutes
    and only alerts on a state change (something broke, or recovered)."""
    if not _is_due("last_health_check", now, HEALTH_CHECK_INTERVAL_SECONDS):
        return
    completed = await _guarded("Health check", _report_health_changes, bot)
    if completed is True:
        update_general_cache({"last_health_check": now.isoformat()})


def _privacy_digest_line(now: datetime) -> str:
    """Counts only: encrypted records and how many students had a personal
    check in the last 24 h. No names, grades or amounts."""
    counts = db.privacy_counts()
    recent = 0
    for user in db.get_all_users():
        try:
            ts = personal_record(user["telegram_id"], PERSONAL_LAST_KEY)
        except Exception:
            logging.debug("Last check time unreadable", exc_info=True)
            continue
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            age = now.timestamp() - ts
            if 0 <= age < 86400:
                recent += 1
    line = (f"🔒 Privacy: {counts['credentials_own_key']} logins under per-student keys, "
            f"{counts['private_rows']} private records for {counts['private_students']} students, "
            f"{recent} students checked in 24 h")
    if counts["credentials_shared_key"]:
        line += f", ⚠️ {counts['credentials_shared_key']} logins still on the shared key"
    return line


async def _send_daily_digest(bot, report_date: str):
    previous = load_general_cache().get("daily_digest_snapshot") or {}
    log_paths = [f"{LOG_FILE}.1", LOG_FILE]  # the rotated file covers a rollover
    # Log timestamps are the server's naive local time, so compare in that clock.
    lines = daily_digest(datetime.now(), log_paths, previous)
    lines.append(_privacy_digest_line(datetime.now(TZ_BERLIN)))
    registry_line = course_registry.savings_line()
    if registry_line:
        lines.append(registry_line)
    cache = load_general_cache()
    progress = cache.get("daily_digest_delivery") or {}
    sent_ids = set(progress.get("sent") or []) if progress.get("date") == report_date else set()
    admin_ids = set(ADMIN_USER_IDS)
    try:
        admin_ids.update(
            user["telegram_id"]
            for user in db.get_all_users()
            if user.get("role") == "admin" and user.get("is_active", True)
        )
    except Exception:
        logging.warning("Could not load database administrators for daily digest", exc_info=True)
    if not admin_ids:
        raise RuntimeError("No administrator recipients available for the daily digest")
    pending_admins = admin_ids - sent_ids
    result = await broadcast(bot, "\n".join(lines), parse_mode="HTML", members=pending_admins)
    sent_ids.update(result["sent"])
    update_general_cache({"daily_digest_delivery": {"date": report_date, "sent": sorted(sent_ids)}})
    if result["failed"]:
        raise RuntimeError(f"Daily digest failed for {len(result['failed'])} admin(s)")
    course_registry.reset_digest_counters()
    update_general_cache({"daily_digest_snapshot": traffic_snapshot(), "daily_digest_delivery": {}})


async def _send_weekly_reports(bot, period: str):
    """Every signed-in student gets their own Sunday report (study_week.weekly_report)."""
    progress_key = f"weekly_report_delivery:{period}"
    sent_ids = set(load_general_cache().get(progress_key) or [])
    failed = []
    for account in signed_in_accounts():
        if (account.user_id in sent_ids or not is_user_allowed(account.user_id)
                or lifecycle_state(account.user_id) != ACTIVE):
            continue
        try:
            with acting_as(account.user_id):
                text = await weekly_report(account.user_id)
                if (text is None or not db.private_context_is_current(account.user_id)
                        or not is_user_allowed(account.user_id)
                        or lifecycle_state(account.user_id) != ACTIVE):
                    continue
                await bot.send_message(chat_id=account.user_id, text=text, parse_mode="HTML")
                if not db.private_context_is_current(account.user_id):
                    continue
                sent_ids.add(account.user_id)
                update_general_cache({progress_key: sorted(sent_ids)})
        except Exception as e:
            logging.warning("Weekly report failed for %s: %s", account.user_id, e)
            failed.append(account.user_id)
    if failed:
        raise RuntimeError(f"Weekly report failed for {len(failed)} user(s)")
    cache = load_general_cache()
    cache.pop(progress_key, None)
    save_general_cache(cache)


async def _run_weekly_report(bot, now: datetime):
    """Sunday evening, once per ISO week."""
    if not weekly_report_due(now):
        return
    year, week, _ = now.isocalendar()
    period = f"{year}-W{week:02d}"
    await _run_once_per_period(
        "last_weekly_report", period, "Weekly report",
        "📊 Sending the weekly reports...", _send_weekly_reports, bot, period,
    )


async def _backup_database():
    pruned = await asyncio.to_thread(db.prune_activity)
    if pruned:
        logging.info("🧹 %s activity rows older than %s days removed", pruned, db.ACTIVITY_KEEP_DAYS)
    # Snapshot only after retention cleanup, otherwise each daily archive
    # would keep expired activity until that archive itself aged out.
    path = await asyncio.to_thread(db.backup)
    logging.info("💾 Database backup written: %s", os.path.basename(path))


async def _run_daily_backup(now: datetime):
    """Once a day, the first watcher cycle after BACKUP_HOUR (see db.backup)."""
    if now.hour < BACKUP_HOUR:
        return
    await _run_once_per_period("last_db_backup", now.date().isoformat(), "Database backup",
                               "💾 Backing up the database...", _backup_database)


async def _run_daily_digest(bot, now: datetime):
    """Once a day the admins get a summary of errors, university traffic,
    worker failovers and session state (see diagnostics.daily_digest)."""
    if now.hour < DAILY_DIGEST_HOUR:
        return
    await _run_once_per_period(
        "last_daily_digest", now.date().isoformat(), "Daily digest",
        "🩺 Sending the daily health digest...", _send_daily_digest, bot, now.date().isoformat(),
    )


async def _run_content_watchers(bot, admin_id, cycle_count: int, now: datetime):
    """Announcements, files and forum posts. Messages are polled separately
    every MESSAGE_POLL_SECONDS by message_poll_loop, not once per cycle."""
    # every signed-in user's courses, each fetched once with a participant's session
    try:
        courses = await watched_courses()
    except Exception as e:
        logging.error("❌ Could not build the course watch plan: %s", e)
        raise
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
    """Wait while the admin is actively using the bot, so background requests
    don't compete with theirs. A signed-out admin no longer stops the cycle:
    the students' checks run on their own sessions (_admin_watcher_session)."""
    session = state.global_session
    owner = getattr(session, "user_id", None) if session is not None else None
    if owner is not None and (not is_user_allowed(owner) or lifecycle_state(owner) != ACTIVE):
        return False
    return session is not None and not session.explicitly_logged_out and (
        time.time() - getattr(session, "last_interactive_activity", 0.0) < 180
    )


_admin_session_missing = False


async def _admin_watcher_session():
    """The admin's own Stud.IP session, or None. Only the single-account
    fallback needs it: courses are read with their members' sessions and
    every student's checks use their own, so a signed-out admin must not
    stop the cycle for everyone. Logged once per outage, not every cycle."""
    global _admin_session_missing
    session = None
    current = state.global_session
    if current is None or not current.explicitly_logged_out:
        owner = getattr(current, "user_id", None) if current is not None else None
        if owner is None or (is_user_allowed(owner) and lifecycle_state(owner) == ACTIVE):
            try:
                session = await login_studip()
            except Exception as e:
                logging.debug("Admin Stud.IP login failed: %s", e)
        else:
            logging.debug("Skipping watcher login for inactive or blocked admin %s", owner)
    if session and _admin_session_missing:
        logging.info("🔑 The admin's Stud.IP session is back")
    elif not session and not _admin_session_missing:
        logging.warning("🔑 No admin Stud.IP session; the watcher runs on the students' own sessions")
    _admin_session_missing = not session
    return session or None


async def unified_watcher_controller(app):
    """Centrally manage all watchers - with a single task"""
    logging.info("🟢 Unified Watcher Controller STARTED")
    state.watcher_controller_running = True
    act_as_bot()  # this task acts for the bot itself, never for a user
    try:
        # Wait 2 minutes for initial check (for bot to fully start)
        await asyncio.sleep(120)
        bot = app.bot
        cycle_count = 0
        while True:
            try:
                # Resolve the admin inside the retrying loop. A transient DB
                # error during startup must not permanently kill all watcher
                # jobs while Telegram polling continues to run.
                admin_id = PRIMARY_ADMIN_ID or next(
                    (u["telegram_id"] for u in db.get_all_users()
                     if u.get("role") == "admin" and u.get("is_active", 1)),
                    None,
                )
                if not admin_id:
                    logging.error("❌ No admin user found for watcher")
                    await asyncio.sleep(120)
                    continue

                if state.global_watcher_paused:
                    logging.info("⏸️ Watcher controller paused")
                    await asyncio.sleep(30)
                    continue
                if _should_skip_cycle():
                    await asyncio.sleep(30)
                    continue

                session = await _admin_watcher_session()
                cycle_count += 1
                logging.info("🔄 Watcher cycle #%s started", cycle_count)
                now = datetime.now(TZ_BERLIN)
                _cycle_failures.set([])
                await _run_reminder_checks(bot, session, now)
                await _run_exam_date_checks(bot, session, now)
                await _run_health_check(bot, now)
                await _run_daily_backup(now)
                await _run_daily_digest(bot, now)
                await _run_weekly_report(bot, now)
                await _run_content_watchers(bot, admin_id, cycle_count, now)
                logging.info("✅ Watcher cycle #%s completed", cycle_count)
                if not _cycle_failures.get():
                    logging.info("Watcher cycle healthy pid=%s", os.getpid())
                state.global_scan_count += 1
                state.last_full_check_time = berlin_now()
                await asyncio.sleep(180)

            except Exception as e:
                logging.error("❌ Watcher controller error: %s", e)
                await asyncio.sleep(120)
    finally:
        state.watcher_controller_running = False


SESSION_KEEPALIVE_SECONDS = 600  # 10 min keep-alive for OAuth + Stud.IP/NetIQ cookies


async def scheduled_jobs_loop(app):
    """Slow AI and indexing jobs must not block content or deadline checks."""
    act_as_bot()
    while True:
        if not state.global_watcher_paused:
            await _guarded("Scheduled jobs", _run_scheduled_jobs, app.bot,
                           state.global_session, berlin_now())
        await asyncio.sleep(60)


async def deadline_reminders_loop(app):
    """Poll time-sensitive reminders independently of all long watcher jobs."""
    act_as_bot()
    while True:
        if not state.global_watcher_paused:
            token = _cycle_failures.set([])
            try:
                await asyncio.gather(
                    _guarded("Task reminder check", check_task_reminders, app.bot),
                    _guarded("Calendar reminder check", _send_calendar_reminders, app.bot),
                )
                if not _cycle_failures.get():
                    logging.info("Deadline reminder cycle healthy pid=%s", os.getpid())
            finally:
                _cycle_failures.reset(token)
        await asyncio.sleep(30)


async def session_keepalive_loop():
    """Periodically refresh OAuth2 tokens and touch Stud.IP / NetIQ IDP cookies
    so sessions stay alive indefinitely without re-entering credentials."""
    act_as_bot()  # this task acts for the bot itself, never for a user
    while True:
        await asyncio.sleep(SESSION_KEEPALIVE_SECONDS + random.uniform(0, 60))
        # an expired (NEEDS_REAUTH) or stopped (INACTIVE) student's session is left alone
        sessions = [
            s for s in all_sessions()
            if lifecycle_state(getattr(s, "user_id", None)) == ACTIVE
            and (getattr(s, "user_id", None) is None or is_user_allowed(getattr(s, "user_id", None)))
        ]
        for index, sess in enumerate(sessions):
            if index:
                await asyncio.sleep(random.uniform(2.0, 6.0))  # never all users at once
            user_id = getattr(sess, "user_id", None)
            if user_id is not None and (
                not is_user_allowed(user_id) or lifecycle_state(user_id) != ACTIVE
            ):
                continue
            try:
                await sess.keep_alive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.debug("Session keep-alive error for %s: %s", getattr(sess, "user_id", None), exc)


async def start_unified_watcher(app):
    """Start the unified watcher"""

    if state.watcher_controller_running and state.unified_watcher_task and not state.unified_watcher_task.done():
        logging.info("⚠️ Watcher controller already running")
        return

    logging.info("🚀 Starting unified watcher controller...")
    state.unified_watcher_task = state.spawn(
        unified_watcher_controller(app), name="unified_watcher_controller"
    )
    if state.message_poll_task is None or state.message_poll_task.done():
        state.message_poll_task = state.spawn(message_poll_loop(app), name="message_poll_loop")
    if state.session_keepalive_task is None or state.session_keepalive_task.done():
        state.session_keepalive_task = state.spawn(session_keepalive_loop(), name="session_keepalive_loop")
    if state.scheduled_jobs_task is None or state.scheduled_jobs_task.done():
        state.scheduled_jobs_task = state.spawn(scheduled_jobs_loop(app), name="scheduled_jobs_loop")
    if state.deadline_reminders_task is None or state.deadline_reminders_task.done():
        state.deadline_reminders_task = state.spawn(deadline_reminders_loop(app), name="deadline_reminders_loop")

    # Check initial state
    await asyncio.sleep(5)
    if state.watcher_controller_running:
        logging.info("✅ Unified watcher controller started successfully")
    else:
        logging.error("❌ Failed to start watcher controller")


async def stop_unified_watcher():
    """Stop the unified watcher"""

    for task in (state.unified_watcher_task, state.message_poll_task, state.session_keepalive_task,
                 state.scheduled_jobs_task, state.deadline_reminders_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    state.watcher_controller_running = False
    logging.info("🛑 Unified watcher controller stopped")
COURSE_INDEX_HOUR = 8  # daily, on the first watcher cycle from this hour on
