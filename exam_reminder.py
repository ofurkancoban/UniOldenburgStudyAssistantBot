"""Exam registration watcher for the StuMS/HISinOne portal (stums.uni-oldenburg.de).

Reuses the existing Stud.IP session (SSO cookies are shared between
elearning.uni-oldenburg.de and stums.uni-oldenburg.de) to read the
"Prüfungsanmeldung" study planner tree, and reports which exams currently
have an open registration window.

Key detail learned while reverse-engineering the page: the study planner
tree shows an "Apply" (anmelden) button for every exam the student is
allowed to register for, even after the registration window has closed.
The actual open/closed state has to be read from the exam's detail page,
which uses a tense difference in its wording:
  - "Enrollment feasible from X until Y"      -> currently open
  - "Enrollment was feasible from X until Y"  -> already closed

Layers: stums_client.py fetches (SSO session, pages, register action),
stums_parsers.py parses, and this module keeps the reminder state (the exam
cache) and decides what to announce. Both lower layers are re-exported here.
"""

import contextlib
import contextvars
import functools
import html
import logging
import os
import time
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from stums_parsers import (  # noqa: F401 - the parsing layer, re-exported for callers
    ACTION_ONCLICK_RE,
    ACTION_PARAM_PAIR_RE,
    ENROLLMENT_LINE_RE,
    EXAM_DATE_RE,
    EXAM_SCHEDULE_LINE_RE,
    EXAM_SCHEDULE_TIME_RE,
    EXAM_TIME_RANGE_RE,
    FOOTER_NOISE,
    GROUP_LABEL_RE,
    MODULE_CODE_SUFFIX_RE,
    PERIOD_HEADER_RE,
    SUMMARY_ROW_TITLE_RE,
    WEEKDAY_RE,
    WINDOW_DATETIME_RE,
    _extract_action_params,
    _format_time_range_24h,
    _parse_candidate_units,
    _parse_exam_schedule_line,
    _parse_examination_periods,
    _parse_invoice_table_rows,
    _parse_registration_status,
    _parse_window_datetimes,
    base_module_code,
    compute_transcript_summary,
    filter_exams_by_module_codes,
    is_passed_exam,
)

from stums_client import (  # noqa: F401 - the client layer, re-exported for callers
    _build_jsf_form_payload,
    _establish_stums_session,
    _expand_grades_tree,
    _fetch_examination_periods,
    _find_confirm_action,
    _find_span,
    _has_stums_cookie,
    _mark_stums_verified,
    _span_text,
    _stums_background,
    _stums_lock,
    _stums_logged_in,
    _stums_verified,
    _submit_action,
    background_stums,
    berlin_now,
    download_stums_document,
    ENROLLMENT_INFO_URL,
    forget_stums_login,
    get_all_exam_dates,
    get_grades,
    get_open_exam_registrations,
    get_registered_exam_schedule,
    get_registered_exams,
    get_tuition_status,
    GRADES_URL,
    JSF_AJAX_CALL_RE,
    SSO_ENTRY_URL,
    STUDY_PLANNER_URL,
    STUDY_SERVICE_URL,
    STUMS_BASE,
    STUMS_DOCUMENTS,
    STUMS_HOST,
    STUMS_RETRY_SECONDS,
    STUMS_START_URL,
    STUMS_VERIFIED_SECONDS,
    StumsLoginUnavailable,
    submit_exam_action,
)

WA_FORWARD_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])

TUITION_CHECK_SECONDS = 24 * 3600  # fee status changes rarely


def _background_job(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with background_stums():
            return await fn(*args, **kwargs)
    return wrapper


def _log_stums_failure(context: str, exc: Exception) -> None:
    log = logging.debug if getattr(exc, "quiet", False) else logging.error
    log("exam_reminder: %s: %s", context, exc)

EXAM_CACHE_PATH = "exam_reminder_cache.json"
DEADLINE_THRESHOLDS_DAYS = [7, 3, 1]
EXAM_DATE_THRESHOLDS_DAYS = [3, 1, 0]
EXAM_HOURS_BEFORE_THRESHOLD = 3


# Whose StuMS data a check is about. Each student has their own registrations,
# grades and fees, so each gets their own cache file (and notifications only
# go to them); None is the bot's original single-user cache file.
_exam_cache_user = contextvars.ContextVar("exam_cache_user", default=None)


@contextlib.contextmanager
def exam_cache_user(user_id):
    """Use `user_id`'s exam cache for every load/save inside this block."""
    token = _exam_cache_user.set(user_id)
    try:
        yield
    finally:
        _exam_cache_user.reset(token)


def exam_cache_path(user_id=None) -> str:
    uid = user_id if user_id is not None else _exam_cache_user.get()
    if uid is None:
        return EXAM_CACHE_PATH
    root, ext = os.path.splitext(EXAM_CACHE_PATH)
    return f"{root}_{int(uid)}{ext or '.json'}"


def _exam_store():
    # SQLite document store of the bot; imported lazily so this module stays
    # importable on its own. A cache still on disk as JSON is imported into
    # it on first read (db.load) and lives in the database from then on.
    from unibot import db
    return db


PRIVATE_EXAM_KIND = "exam_cache"


def _cache_owner(user_id=None):
    return user_id if user_id is not None else _exam_cache_user.get()


def _empty_exam_cache() -> dict:
    return {
        "notified_open": {}, "notified_deadline": {}, "notified_exam_date": {},
        "notified_exam_hours": {}, "known_grades": {},
    }


def adopt_legacy_exam_cache(user_id) -> None:
    """Give the original single-user cache to `user_id` (the account it was
    built for) the first time per-user caches are used, so nothing is
    re-notified after the switch."""
    db = _exam_store()
    if user_id is None or db.get_user_scope(int(user_id)).has_private(PRIVATE_EXAM_KIND) \
            or db.exists(exam_cache_path(user_id)) or not db.exists(EXAM_CACHE_PATH):
        return
    try:
        save_exam_cache(db.load(EXAM_CACHE_PATH), user_id=user_id)
        db.delete(EXAM_CACHE_PATH)
        logging.info("exam_reminder: adopted the legacy exam cache for user %s", user_id)
    except Exception as e:
        logging.warning("Could not adopt the legacy exam cache for %s: %s", user_id, e)


def load_exam_cache(user_id=None) -> dict:
    """A student's exam cache (grades seen, registrations, fee status) comes
    from their encrypted private record; a per-user document left from older
    versions moves there on first read. Without any student (single-account
    setup) the original shared document is used."""
    db = _exam_store()
    owner = _cache_owner(user_id)
    try:
        if owner is not None:
            data = db.get_user_scope(int(owner)).private(PRIVATE_EXAM_KIND)
            if data is not None:
                return data
            legacy = exam_cache_path(owner)
            if db.exists(legacy):
                data = db.load(legacy)
                db.get_user_scope(int(owner)).save_private(PRIVATE_EXAM_KIND, data)
                db.delete(legacy)
                return data
        elif db.exists(EXAM_CACHE_PATH):
            return db.load(EXAM_CACHE_PATH)
    except Exception as e:
        logging.warning("Could not load exam reminder cache: %s", e)
    return _empty_exam_cache()


def save_exam_cache(data: dict, user_id=None) -> None:
    owner = _cache_owner(user_id)
    try:
        if owner is not None:
            _exam_store().get_user_scope(int(owner)).save_private(PRIVATE_EXAM_KIND, data)
        else:
            _exam_store().save(EXAM_CACHE_PATH, data)
    except Exception as e:
        logging.error("Could not save exam reminder cache: %s", e)


# How long an announced exam registration must be missing from successful
# checks before it counts as closed (and a later reopening is announced again).
OPEN_CLOSED_GRACE_SECONDS = 6 * 3600


@_background_job
async def check_exam_date_reminders(session, bot, broadcast_fn) -> None:
    """Remind about upcoming exam dates the student is registered for: at 3/1/0 days
    before, plus a same-day heads-up a few hours before start when a start time is
    known. Past exam dates are skipped (a registration can stay listed for a while
    after the exam itself has happened).
    """
    try:
        exams = await get_registered_exam_schedule(session)
    except Exception as e:
        _log_stums_failure("fetch registered exam schedule failed", e)
        return

    cache = load_exam_cache()
    notified_days = cache.get("notified_exam_date", {})
    notified_hours = cache.get("notified_exam_hours", {})

    now = berlin_now()
    today = now.date()
    still_upcoming_keys = set()

    for exam in exams:
        exam_date = datetime.fromisoformat(exam["date"]).date()
        days_left = (exam_date - today).days
        if days_left < 0:
            continue  # exam date already passed

        key = f"{exam['unit_id']}:{exam['date']}"
        still_upcoming_keys.add(key)

        sent_days = set(notified_days.get(key, []))
        for threshold in EXAM_DATE_THRESHOLDS_DAYS:
            if days_left == threshold and threshold not in sent_days:
                when_line = "🔴 <b>Exam is TODAY!</b>" if threshold == 0 else f"⏳ <b>{threshold} day(s) left</b>"
                form_line = f"🎯 <b>Form:</b> {exam['exam_form']}\n" if exam.get("exam_form") else ""
                examiner_line = f"👤 <b>Examiner:</b> {exam['examiner']}\n" if exam.get("examiner") else ""
                text = (
                    "📚 <b>UPCOMING EXAM</b>\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>Course:</b> {exam['title']}\n"
                    f"📅 <b>Date:</b> {exam['date_line']}\n"
                    f"{form_line}"
                    f"{examiner_line}"
                    f"{when_line}\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                try:
                    await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                    logging.info("🎓 Exam date reminder sent")
                except Exception as e:
                    logging.error("exam_reminder: failed to send exam-date reminder for %s: %s", key, e)
                sent_days.add(threshold)
                notified_days[key] = sorted(sent_days)
                cache["notified_exam_date"] = notified_days
                save_exam_cache(cache)
        notified_days[key] = sorted(sent_days)

        if exam.get("start_datetime") and not notified_hours.get(key):
            start_dt = datetime.fromisoformat(exam["start_datetime"])
            hours_left = (start_dt - now).total_seconds() / 3600
            if 0 <= hours_left <= EXAM_HOURS_BEFORE_THRESHOLD:
                form_line = f"🎯 <b>Form:</b> {exam['exam_form']}\n" if exam.get("exam_form") else ""
                text = (
                    "⏰ <b>EXAM STARTING SOON</b>\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>Course:</b> {exam['title']}\n"
                    f"📅 <b>Date:</b> {exam['date_line']}\n"
                    f"{form_line}"
                    f"⌛ <b>Starts in ~{int(hours_left)} hour(s)</b>\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                try:
                    await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                    logging.info("🎓 Exam starting soon reminder sent")
                except Exception as e:
                    logging.error("exam_reminder: failed to send starting-soon reminder for %s: %s", key, e)
                notified_hours[key] = True
                cache["notified_exam_hours"] = notified_hours
                save_exam_cache(cache)

    # Drop bookkeeping for exams that dropped off the upcoming list (already
    # happened, or deregistered), so a future re-registration is treated as new.
    for key in list(notified_days.keys()):
        if key not in still_upcoming_keys:
            notified_days.pop(key, None)
            notified_hours.pop(key, None)

    cache["notified_exam_date"] = notified_days
    cache["notified_exam_hours"] = notified_hours
    save_exam_cache(cache)


@_background_job
async def check_exam_reminders(session, bot, broadcast_fn, exam_action_cache: dict) -> None:
    """Check for newly-opened exam registrations and closing-soon deadlines.

    `broadcast_fn` must be an async callable with signature (bot, text, parse_mode="HTML"),
    matching studip_bot.broadcast. `exam_action_cache` is studip_bot's short_id -> {unit_id,
    action_type, title, ...} cache (the same one the Exam Registration menu's buttons use) —
    each notification gets its own short id registered here so its "Register" button can
    reuse the existing exam_ask/exam_do confirm flow instead of a new one.
    """
    failed_codes: set = set()
    try:
        open_exams = await get_open_exam_registrations(session, failed_codes=failed_codes)
    except Exception as e:
        _log_stums_failure("fetch failed", e)
        return

    cache = load_exam_cache()
    notified_open = cache.get("notified_open", {})
    notified_deadline = cache.get("notified_deadline", {})
    muted_codes = set(cache.get("muted_codes", []))

    now = berlin_now()
    still_open_keys = set()
    # Keys from before the window dates were parsed read "code:None:None" and
    # stood for the whole unit: every sitting of such a unit was announced.
    legacy_announced = {key.split(":", 1)[0]: notified_open.pop(key) for key in list(notified_open)
                        if key.endswith(":None:None")}
    legacy_deadlines = {key.split(":", 1)[0]: notified_deadline.pop(key) for key in list(notified_deadline)
                        if key.endswith(":None:None")}

    for exam in open_exams:
        code = exam["code"]
        # A unit can have several parallel groups open at once (each its own sitting),
        # so the notification/dedup key has to include the sitting, not just the code.
        occurrence_key = f"{code}:{exam['start_date']}:{exam['end_date']}"
        if code in legacy_announced and occurrence_key not in notified_open:
            notified_open[occurrence_key] = legacy_announced[code]
            if code in legacy_deadlines:
                notified_deadline[occurrence_key] = list(legacy_deadlines[code])
        # Still tracked even when muted, so the cache-cleanup pass below doesn't
        # treat "muted" as "closed" and purge its notification state.
        still_open_keys.add(occurrence_key)
        if code in muted_codes:
            continue
        end_date = datetime.fromisoformat(exam["end_date"]) if exam["end_date"] else None
        class_line = f"👥 <b>Class:</b> {exam['group_label']}\n" if exam.get("group_label") else ""
        room_line = f"📍 <b>Room:</b> {exam['room']}\n" if exam.get("room") else ""
        exam_date_line = f"📅 <b>Exam Date:</b> {exam['exam_date_display']}\n" if exam.get("exam_date_display") else ""

        sid = str(uuid.uuid4())[:8]
        exam_action_cache[sid] = {
            "unit_id": exam["unit_id"],
            "action_type": "anmelden",
            "title": exam["title"],
            "code": code,
            "user_id": getattr(session, "user_id", None),  # whose notification this is
            # Carried along so "📲 Forward to WA" (handle_examopen_wa_confirm,
            # studip_bot.py) can rebuild the same detailed text the Telegram
            # notification itself shows, instead of just the course title.
            "exam_date_display": exam.get("exam_date_display"),
            "window_display": exam.get("window_display") or exam.get("raw"),
            "group_label": exam.get("group_label"),
            "room": exam.get("room"),
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Register", callback_data=f"exam_ask|{sid}")],
            [InlineKeyboardButton("📲 Forward to WA", callback_data=f"examopen_wa_ask|{sid}")],
            [InlineKeyboardButton("🔕 Don't remind me again", callback_data=f"examopen_mute_ask|{sid}")],
        ])

        if occurrence_key not in notified_open:
            text = (
                "🆕 <b>EXAM REGISTRATION OPEN</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"📘 <b>Course:</b> {exam['title']}\n"
                f"{exam_date_line}"
                f"{class_line}"
                f"{room_line}"
                f"🗓 <b>Window:</b> {exam.get('window_display') or exam['raw']}\n"
                "━━━━━━━━━━━━━━━━━"
            )
            try:
                await broadcast_fn(bot, text, reply_markup=keyboard)
                logging.info("🎓 Exam registration opening sent")
            except Exception as e:
                logging.error("exam_reminder: failed to send open-notification for %s: %s", occurrence_key, e)
            notified_open[occurrence_key] = now.isoformat()
            # Persist immediately so a crash while processing a later exam in this
            # same batch can't cause this notification to be resent on restart.
            cache["notified_open"] = notified_open
            save_exam_cache(cache)

        if end_date:
            days_left = (end_date.date() - now.date()).days
            sent_thresholds = set(notified_deadline.get(occurrence_key, []))
            for threshold in DEADLINE_THRESHOLDS_DAYS:
                if days_left == threshold and threshold not in sent_thresholds:
                    text = (
                        "⏰ <b>EXAM REGISTRATION CLOSING SOON</b>\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"📘 <b>Course:</b> {exam['title']}\n"
                        f"{exam_date_line}"
                        f"{class_line}"
                        f"{room_line}"
                        f"⌛ <b>{days_left} day(s) left</b> (closes {end_date.strftime('%d.%m.%Y %H:%M')})\n"
                        "━━━━━━━━━━━━━━━━━"
                    )
                    try:
                        await broadcast_fn(bot, text, reply_markup=keyboard)
                        logging.info("🎓 Exam registration deadline reminder sent")
                    except Exception as e:
                        logging.error("exam_reminder: failed to send deadline-notification for %s: %s", occurrence_key, e)
                    sent_thresholds.add(threshold)
                    notified_deadline[occurrence_key] = sorted(sent_thresholds)
                    cache["notified_deadline"] = notified_deadline
                    save_exam_cache(cache)
            notified_deadline[occurrence_key] = sorted(sent_thresholds)

    # A sitting missing from still_open_keys has either closed or its registration
    # window ended; clear its notification state so a future re-opening (e.g. a
    # resit period) is treated as new again. But if its unit's detail-page fetch
    # simply failed this cycle (in failed_codes), we don't actually know it
    # closed — purging it here would make the next successful check treat it
    # as newly-opened again and resend the "registration open" notification
    # for an exam that was never actually closed.
    # a legacy unit whose sittings were not in this result keeps its old key
    for code, stamp in legacy_announced.items():
        if not any(key.startswith(f"{code}:") for key in still_open_keys):
            notified_open[f"{code}:None:None"] = stamp
            if code in legacy_deadlines:
                notified_deadline[f"{code}:None:None"] = legacy_deadlines[code]

    # A unit can also drop out of one result without an error (a StuMS detail
    # page that comes back without its periods), so a key is only forgotten
    # after it has been missing for OPEN_CLOSED_GRACE_SECONDS.
    missing_since = cache.get("open_missing_since", {})
    for key in list(notified_open.keys()):
        if key in still_open_keys:
            missing_since.pop(key, None)
            continue
        key_code = key.split(":", 1)[0]
        if key_code in failed_codes:
            continue
        first_missing = missing_since.setdefault(key, now.isoformat())
        try:
            missing_for = (now - datetime.fromisoformat(first_missing)).total_seconds()
        except ValueError:
            missing_for = OPEN_CLOSED_GRACE_SECONDS
        if missing_for < OPEN_CLOSED_GRACE_SECONDS:
            continue
        notified_open.pop(key, None)
        notified_deadline.pop(key, None)
        missing_since.pop(key, None)
    for key in list(missing_since):
        if key not in notified_open:
            missing_since.pop(key, None)

    cache["notified_open"] = notified_open
    cache["notified_deadline"] = notified_deadline
    cache["open_missing_since"] = missing_since
    save_exam_cache(cache)


# Terminal grade statuses worth notifying about (as opposed to interim states
# like "Coursework submitted/registered" or "admission"). The grades page spells
# the failing one "not passed", not "failed" — matched exactly since "passed" is
# a substring of "not passed".
FINAL_GRADE_STATUSES = {"passed", "not passed"}


@_background_job
async def check_grade_reminders(session, bot, broadcast_fn) -> None:
    """Notify when a module reaches a final grade (passed/failed) for the first
    time, or when that result changes (e.g. a correction)."""
    try:
        grades = await get_grades(session)
    except Exception as e:
        _log_stums_failure("fetch grades failed", e)
        return

    cache = load_exam_cache()
    known_grades = cache.get("known_grades", {})

    # Initial baseline seeding: record existing grades without blast-alerting historical exams
    if not known_grades:
        for entry in grades:
            st = (entry.get("status_title") or "").lower()
            if st in FINAL_GRADE_STATUSES:
                known_grades[entry["number"]] = {"status_title": st, "grade": entry.get("grade")}
        cache["known_grades"] = known_grades
        save_exam_cache(cache)
        logging.info("🎓 Grade baseline seeded")
        return

    for entry in grades:
        status_title = (entry.get("status_title") or "").lower()
        if status_title not in FINAL_GRADE_STATUSES:
            continue

        number = entry["number"]
        grade_val = (entry.get("grade") or "").strip()
        fingerprint = {"status_title": status_title, "grade": grade_val or None}
        if known_grades.get(number) == fingerprint:
            continue  # already notified about this exact result

        is_passed = status_title == "passed"
        congrats_header = "🎉 <b>CONGRATULATIONS! NEW EXAM GRADE</b>" if is_passed else "📋 <b>NEW EXAM RESULT ANNOUNCED</b>"
        congrats_line = "🥳 <i>Congratulations, you passed the course!</i>\n" if is_passed else ""

        praise = ""
        if is_passed and grade_val:
            try:
                val_f = float(grade_val.replace(",", "."))
                if val_f <= 1.3:
                    praise = " 🌟 <i>(Outstanding / Sehr gut!)</i>"
                elif val_f <= 2.0:
                    praise = " 👏 <i>(Great result / Gut!)</i>"
            except ValueError:
                pass

        grade_line = f"🔢 <b>Grade:</b> <b>{html.escape(grade_val)}</b>{praise}\n" if grade_val else ""
        credit_val = entry.get("credit_points")
        credit_line = f"🎓 <b>Credits:</b> <b>+{html.escape(credit_val)} ECTS</b>\n" if credit_val else ""

        # Personal message with the actual grade - no WA-forward button, since
        # this can carry personal grade info that shouldn't end up in a shared
        # (e.g. department) WhatsApp group by an accidental tap.
        personal_text = (
            f"{congrats_header}\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"{congrats_line}"
            f"📘 <b>Course:</b> {html.escape(entry.get('title') or 'Module')}\n"
            f"📊 <b>Status:</b> <b>{html.escape(status_title.capitalize())}</b>\n"
            f"{grade_line}"
            f"{credit_line}"
            "━━━━━━━━━━━━━━━━━"
        )
        # Separate, grade-free announcement - safe to forward, so it carries the
        # WA button instead.
        announcement_text = (
            "📢 <b>NEW EXAM RESULT ANNOUNCED</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"📘 <b>Course:</b> {html.escape(entry.get('title') or 'Module')}\n"
            "ℹ️ <i>Results are now online on StuMS.</i>\n"
            "━━━━━━━━━━━━━━━━━"
        )
        try:
            await broadcast_fn(bot, personal_text, parse_mode="HTML")
            await broadcast_fn(bot, announcement_text, parse_mode="HTML", reply_markup=WA_FORWARD_MARKUP)
            logging.info("🎓 Grade notification sent")
        except Exception as e:
            logging.error("exam_reminder: failed to send a grade notification: %s", type(e).__name__)

        known_grades[number] = fingerprint
        cache["known_grades"] = known_grades
        save_exam_cache(cache)


@_background_job
async def check_tuition_reminders(session, bot, broadcast_fn, force: bool = False) -> None:
    """Check tuition fee (Rückmeldung) status on StuMS and send proactive alerts if
    unpaid invoices exist or when payment is received."""
    now = berlin_now()
    today = now.date()
    today_str = today.isoformat()

    cache = load_exam_cache()
    t_state = cache.get("tuition_status", {})
    last_check_ts = t_state.get("last_check_ts", 0)

    # Check at most once a day unless forced
    if not force and (time.time() - last_check_ts < TUITION_CHECK_SECONDS):
        return

    try:
        t_data = await get_tuition_status(session)
    except Exception as exc:
        logging.debug("check_tuition_reminders: fetch failed (%s)", exc)
        return

    has_open = bool(t_data.get("has_open_payments", False))
    open_invoices = t_data.get("open_invoices", [])
    was_open = bool(t_state.get("has_open_payments", False))
    last_alert_date = t_state.get("last_alert_date")

    t_state["last_check_ts"] = int(time.time())
    t_state["has_open_payments"] = has_open

    # 1. Payment cleared notification
    if was_open and not has_open:
        cleared_text = (
            "🎉 <b>SEMESTER FEE PAYMENT RECEIVED!</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            "✅ Your semester fee payment has been confirmed by Uni Oldenburg.\n"
            "🎓 <b>Re-registration (Rückmeldung) is complete!</b>\n"
            "━━━━━━━━━━━━━━━━━"
        )
        try:
            await broadcast_fn(bot, cleared_text, parse_mode="HTML")
            logging.info("💶 Tuition fee cleared notification sent.")
        except Exception as exc:
            logging.error("Failed to send tuition clearance alert: %s", exc)

    # 2. Open payment alert
    elif has_open and open_invoices:
        inv = open_invoices[0]
        due_str = inv.get("due_date", "").strip()
        period = inv.get("period", "Upcoming Semester")
        to_pay = inv.get("to_pay", "")
        purpose = inv.get("purpose", "Semesterbeitrag / Rückmeldung")

        days_left = None
        if due_str:
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    due_date = datetime.strptime(due_str, fmt).date()
                    days_left = (due_date - today).days
                    break
                except ValueError:
                    pass

        should_alert = False
        if not was_open:
            should_alert = True
        elif last_alert_date != today_str:
            if days_left is not None:
                if days_left <= 14:
                    should_alert = True
                else:
                    last_alert_dt = datetime.fromisoformat(last_alert_date).date() if last_alert_date else None
                    if not last_alert_dt or (today - last_alert_dt).days >= 3:
                        should_alert = True
            else:
                should_alert = True

        if should_alert:
            days_badge = ""
            if days_left is not None:
                if days_left < 0:
                    days_badge = f" ⚠️ <b>OVERDUE by {abs(days_left)} days!</b>"
                elif days_left == 0:
                    days_badge = " ⚠️ <b>DUE TODAY!</b>"
                elif days_left == 1:
                    days_badge = " ⚠️ <b>DUE TOMORROW!</b>"
                else:
                    days_badge = f" <i>(in {days_left} days)</i>"

            alert_text = (
                "💶 <b>SEMESTER FEE DUE (RÜCKMELDUNG)</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"🎓 <b>Semester:</b> {html.escape(period)}\n"
                f"💰 <b>Amount:</b> <b>{html.escape(to_pay)}</b>\n"
                f"📅 <b>Due Date:</b> <b>{html.escape(due_str)}</b>{days_badge}\n"
                f"🔖 <b>Purpose:</b> {html.escape(purpose)}\n"
                "━━━━━━━━━━━━━━━━━\n"
                "ℹ️ <i>Please transfer the semester fee to complete your re-registration. Run /tuition to see payment details.</i>"
            )
            try:
                await broadcast_fn(bot, alert_text, parse_mode="HTML")
                t_state["last_alert_date"] = today_str
                logging.info("💶 Tuition due reminder sent for %s (%s).", period, to_pay)
            except Exception as exc:
                logging.error("Failed to send tuition due alert: %s", exc)

    cache["tuition_status"] = t_state
    save_exam_cache(cache)


# ── StuMS Official PDF Documents & Tuition / Fee Status ────────────────────────


