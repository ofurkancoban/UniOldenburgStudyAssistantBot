"""First-run setup: right after a student's first sign-in (or their next
/start while signed in, see menus.start) the bot asks every question that is
the student's own decision in one message: the semester, the two
permissions for the AI service, and the preferences of /settings. Each tap
saves that answer and redraws the same message; Done closes it once the
questions without a default (semester and both permissions) are answered.

Callbacks: setup|<question>|<value>, setup|info|<question>, setup|done, and
the older setup_sem|<id> and setup_done from messages sent before.

Part of the unibot package.
"""

import html
import logging
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from fast_enroll import current_semester_id, get_semester_options, semester_display, semester_start

from unibot import preferences as prefs
from unibot import state
from unibot import db
from unibot.auth import acting_as, get_user_session, is_user_admin, is_user_allowed, login_studip
from unibot.settings_menu import QUIET_PRESETS
from unibot.storage import (
    clear_default_semester,
    get_ai_grades_enabled,
    get_ai_personal_enabled,
    get_travel_mode,
    load_default_semester,
    mark_setup_done,
    personal_record,
    rebuild_watched_courses,
    save_default_semester,
    save_personal_record,
    SETUP_ANSWERS_RECORD,
    set_ai_grades_enabled,
    set_ai_personal_enabled,
    set_travel_mode,
    setup_done,
)

SETUP_SEMESTER_CHOICES = 4  # the current term and the ones around it
SETUP_ASK_EACH_TIME = "ask"
SETUP_REOFFER_SECONDS = 600  # a question still on screen isn't sent again (sign-in and /start close together)
_setup_offered: dict[int, float] = {}
_setup_semesters: dict[int, tuple] = {}  # user id -> (options, current id) shown in their setup message

MORNING_CHOICES = ("off", "07:00", "07:30", "08:00", "08:30", "09:00")
YES_NO = (("yes", "Yes"), ("no", "No"))
ON_OFF = (("on", "On"), ("off", "Off"))

# (key, title, explanation shown when the title is tapped, needs an answer)
QUESTIONS = (
    ("sem", "🗓️ Your semester",
     "📁 Files, Browse Courses and Sign Out open this semester directly. Change it later in /status.", True),
    ("ai_personal", "🤖 AI may use personal data",
     "Needed for the spoken morning summary, 🔊 Listen, spoken questions, course-file summaries and study-plan topics, AI meal suggestions, and outfit suggestions. They are made by outside AI "
     "services (including image services for wardrobe features). If you tap a task-suggestion button, the selected course announcement or forum post is also sent for that request.", True),
    ("ai_grades", "🤖 AI may use your grades",
     "Lets you ask the assistant about your grades. They are sent to the outside AI services (GWDG, OpenRouter) "
     "only for that question.", True),
    ("campus_anchor", "🏫 Home campus",
     "Used for study spots and the mensa when your classes don't say where you are.", False),
    ("diet", "🍽️ Diet",
     "The lunch pick in the morning summary and \"What should I eat?\" only show what fits.", False),
    ("morning_time", "🌅 Morning summary",
     "Your day in one message: weather, classes, study windows, mensa and exams. Finer times in /settings.",
     False),
    ("quiet_hours", "🌙 Quiet hours",
     "Course updates arriving then wait and come after your morning summary. Grades and exams always come.",
     False),
    ("delivery_mode", "📬 Course updates",
     "Each update as it happens, or all of them together at 18:00.", False),
    ("file_delivery", "📄 New files",
     "A card with a download button, the file itself, or only its name and link (saves mobile data).", False),
    ("exam_thresholds", "⏳ Exam reminders",
     "How many days before an exam you are reminded.", False),
    ("tuition_reminders", "💶 Unpaid fee reminders",
     "How often an unpaid semester fee is brought up. A paid fee is noticed on StuMS and they stop.", False),
    ("travel", "🚲 Between campuses",
     "How the morning summary plans the way between Haarentor and Wechloy.", False),
    ("outfit", "👔 Outfit suggestion",
     "A clothing tip for the weather in the morning summary.", False),
    ("watch_previous_semester", "👀 Previous semester courses",
     "Keep getting alerts from last semester's courses, for example for exams and grades.", False),
)
REQUIRED = tuple(key for key, _title, _info, required in QUESTIONS if required)
TITLES = {key: title for key, title, _info, _required in QUESTIONS}
INFO = {key: info for key, _title, info, _required in QUESTIONS}

CHOICES: dict[str, tuple] = {
    "ai_personal": YES_NO,
    "ai_grades": YES_NO,
    "campus_anchor": (("both", "Both"), ("haarentor", "Haarentor"), ("wechloy", "Wechloy")),
    "diet": (("all", "Everything"), ("vegetarian", "Vegetarian"), ("vegan", "Vegan"),
             ("halal", "Halal"), ("fish", "Fish, no meat"), ("budget", "Budget")),
    "morning_time": tuple((t, "Off" if t == "off" else t) for t in MORNING_CHOICES),
    "quiet_hours": (("off", "Off"), ("22-07", "22-07"), ("23-08", "23-08"), ("00-09", "00-09")),
    "delivery_mode": (("instant", "Instant"), ("digest", "Evening digest")),
    "file_delivery": (("card", "Card"), ("upload", "The file"), ("link", "Link only")),
    "exam_thresholds": (("standard", "14/7/3/1 d"), ("relaxed", "7/3/1 d"), ("critical", "3 d"), ("off", "Off")),
    "tuition_reminders": (("smart", "Smart"), ("weekly", "Weekly"), ("daily_last7", "Last week daily"),
                          ("off", "Off")),
    "travel": (("bike", "🚲 Bike"), ("bus", "🚌 Bus")),
    "outfit": ON_OFF,
    "watch_previous_semester": ON_OFF,
}
ADMIN_ONLY = ("outfit",)  # the outfit tip is made for administrators only (morning.py)


def questions_for(user_id: int) -> tuple:
    return tuple(q for q in QUESTIONS if q[0] not in ADMIN_ONLY or is_user_admin(user_id))


ROW_WIDTH = {"diet": 3, "morning_time": 3, "quiet_hours": 4, "exam_thresholds": 4, "tuition_reminders": 2}


def semester_choices(options: list, current: Optional[str]) -> list:
    """[(id, label, is_current)]: the current semester first, then the next
    one and the ones before it. options is newest first."""
    ids = [sid for sid, _label in options]
    if current not in ids:
        return [(sid, label, False) for sid, label in options[:SETUP_SEMESTER_CHOICES]]
    i = ids.index(current)
    order = [i, i - 1] + list(range(i + 1, len(options)))
    picked = [k for k in order if 0 <= k < len(options)][:SETUP_SEMESTER_CHOICES]
    return [(options[k][0], options[k][1], k == i) for k in picked]


def short_semester(label: str) -> str:
    """"❄️ Winter 26/27" or "☀️ Summer 2027" for a button two to a row."""
    start = semester_start(label)
    if start is None:
        return label
    if start.month == 4:
        return f"☀️ Summer {start.year}"
    return f"❄️ Winter {start.year % 100:02d}/{(start.year + 1) % 100:02d}"


# ── answers ───────────────────────────────────────────────────────────────────

def _answered(user_id: int) -> set:
    stored = personal_record(user_id, SETUP_ANSWERS_RECORD)
    return set(stored) if isinstance(stored, list) else set()


def _mark_answered(user_id: int, key: str) -> None:
    save_personal_record(user_id, SETUP_ANSWERS_RECORD, sorted(_answered(user_id) | {key}))


def missing_answers(user_id: int) -> list:
    """The questions without a default the student hasn't answered yet."""
    answered = _answered(user_id)
    if load_default_semester(user_id).get("id"):
        answered.add("sem")
    return [key for key in REQUIRED if key not in answered]


def current_value(user_id: int, key: str) -> Optional[str]:
    """The option shown as picked; None for a question still open."""
    if key in REQUIRED and key in missing_answers(user_id):
        return None
    if key == "sem":
        return load_default_semester(user_id).get("id") or SETUP_ASK_EACH_TIME
    if key == "ai_personal":
        return "yes" if get_ai_personal_enabled(user_id) else "no"
    if key == "ai_grades":
        return "yes" if get_ai_grades_enabled(user_id) else "no"
    if key == "travel":
        return get_travel_mode(user_id)
    value = prefs.get_pref(user_id, key)
    if key == "quiet_hours":
        return next((k for k, v in QUIET_PRESETS.items() if v == value), None)
    if key in ("outfit", "watch_previous_semester"):
        return "on" if value else "off"
    return value


def apply_answer(user_id: int, key: str, value: str, semesters: list) -> None:
    """Save one answer; ValueError when the value isn't one of the options."""
    if key == "sem":
        if value == SETUP_ASK_EACH_TIME:
            clear_default_semester(user_id)
        else:
            label = next((lbl for sid, lbl in semesters if sid == value), None)
            if label is None:
                raise ValueError("semester not offered")
            save_default_semester(value, label, user_id=user_id)
        rebuild_watched_courses(user_id)
    elif key not in CHOICES or value not in dict(CHOICES[key]):
        raise ValueError(f"unknown answer {key}={value}")
    elif key in ADMIN_ONLY and not is_user_admin(user_id):
        raise ValueError(f"{key} is not offered to students")
    elif key == "ai_personal":
        set_ai_personal_enabled(user_id, value == "yes")
    elif key == "ai_grades":
        set_ai_grades_enabled(user_id, value == "yes")
    elif key == "travel":
        set_travel_mode(user_id, value)
    elif key == "quiet_hours":
        prefs.set_pref(user_id, key, QUIET_PRESETS[value])
    elif key in ("outfit", "watch_previous_semester"):
        prefs.set_pref(user_id, key, value == "on")
        if key == "watch_previous_semester":
            rebuild_watched_courses(user_id)
    else:
        prefs.set_pref(user_id, key, value)
    if key in REQUIRED:
        _mark_answered(user_id, key)
    if (not db.private_context_is_current(user_id)
            or current_value(user_id, key) != value
            or (key in REQUIRED and key not in _answered(user_id))):
        raise RuntimeError(f"Setup answer {key!r} was not saved for this user")


# ── the setup message ─────────────────────────────────────────────────────────

def _semester_rows(options: list, current: Optional[str]) -> tuple:
    rows: list = []
    row: list = []
    for sid, label, is_current in semester_choices(options, current):
        row.append((sid, short_semester(label) + (" ⭐" if is_current else "")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(SETUP_ASK_EACH_TIME, "🗓️ Ask me each time")])
    return tuple(rows)


def _option_rows(key: str, choices: tuple, picked: Optional[str]) -> list:
    width = ROW_WIDTH.get(key, 3)
    flat = [InlineKeyboardButton(("✅ " if value == picked else "") + label, callback_data=f"setup|{key}|{value}")
            for value, label in choices]
    return [flat[i:i + width] for i in range(0, len(flat), width)]


def setup_view(user_id: int, options: list, current: Optional[str]) -> tuple:
    """The whole setup on one message: every question, its options with the
    picked one ticked, and Done."""
    missing = missing_answers(user_id)
    rows: list = []
    questions = questions_for(user_id)
    for n, (key, title, _info, _required) in enumerate(questions, 1):
        mark = "❗ " if key in missing else ""
        rows.append([InlineKeyboardButton(f"{mark}{n}. {title}", callback_data=f"setup|info|{key}")])
        picked = current_value(user_id, key)
        if key == "sem":
            for pairs in _semester_rows(options, current):
                rows.append([InlineKeyboardButton(("✅ " if sid == picked else "") + label,
                                                  callback_data=f"setup|sem|{sid}") for sid, label in pairs])
        else:
            rows.extend(_option_rows(key, CHOICES[key], picked))
    rows.append([InlineKeyboardButton("✅ Done", callback_data="setup|done")])
    if missing:
        numbers = ", ".join(str(next(i for i, q in enumerate(questions, 1) if q[0] == key)) for key in missing)
        status = f"❗ Still to answer: <b>{numbers}</b>"
    else:
        status = "Everything is answered. Change what you like, then tap <b>✅ Done</b>."
    text = (
        "🧭 <b>Quick setup</b>\n\n"
        "All your choices are below in one place. Tap an option to pick it (✅ is the current one), "
        "or a question's title to see what it does.\n\n"
        "<b>1 to 3 need your answer.</b> The others already have a usual setting.\n"
        "🤖 <i>2 and 3: the spoken morning summary, 🔊 Listen and spoken questions are made by outside "
        "AI services (GWDG, OpenRouter; speech recognition may use Google). Nothing personal goes there unless you say yes.</i>\n\n"
        "Everything stays changeable in /settings (the semester in /status).\n\n" + status
    )
    return text, InlineKeyboardMarkup(rows)


def done_text(user_id: int) -> str:
    lines = []
    for key, title, _info, _required in questions_for(user_id):
        picked = current_value(user_id, key)
        if key == "sem":
            label = load_default_semester(user_id).get("label")
            shown = html.escape(semester_display(label)) if label else "ask each time"
        else:
            shown = html.escape(dict(CHOICES[key]).get(picked or "", picked or "custom"))
        lines.append(f"{title}: <b>{shown}</b>")
    return ("✅ <b>Setup complete.</b>\n\n" + "\n".join(lines)
            + "\n\nChange anything later in /settings, or the semester in /status.")


async def _semester_options(user_id: int, session=None) -> list:
    session = session or get_user_session(user_id)
    if session is None or not await session.is_logged_in():
        session = await login_studip(user_id=user_id)
    return await get_semester_options(session)


async def _semesters_for(user_id: int) -> tuple:
    """The semesters shown in the student's setup message, fetched again
    after a restart."""
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return [], None
    cached = _setup_semesters.get(user_id)
    if cached:
        return cached
    options = await _semester_options(user_id)
    cached = (options, current_semester_id(options))
    if is_user_allowed(user_id) and db.private_context_is_current(user_id):
        _setup_semesters[user_id] = cached
    return cached


async def offer_setup(bot, user_id: int, session=None) -> bool:
    """Send the setup message unless the student already finished it.
    Returns whether it was sent."""
    # Login hooks and /start can run outside a Telegram update context. Bind
    # the whole async flow to the user's current data generation so deletion
    # can invalidate the in-flight semester lookup and prevent it from
    # recreating onboarding state afterward.
    with acting_as(user_id):
        return await _offer_setup_for_user(bot, user_id, session)


async def _offer_setup_for_user(bot, user_id: int, session=None) -> bool:
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return False
    if setup_done(user_id):
        return False
    offered_at = time.time()
    if offered_at - _setup_offered.get(user_id, 0.0) < SETUP_REOFFER_SECONDS:
        return False
    _setup_offered[user_id] = offered_at  # before any await: a concurrent offer sees it
    semester_snapshot = None

    def clear_stale_offer() -> None:
        if _setup_offered.get(user_id) == offered_at:
            _setup_offered.pop(user_id, None)
        if semester_snapshot is not None and _setup_semesters.get(user_id) is semester_snapshot:
            _setup_semesters.pop(user_id, None)

    try:
        options = await _semester_options(user_id, session)
        if (not is_user_allowed(user_id) or not db.private_context_is_current(user_id)
                or setup_done(user_id)):
            clear_stale_offer()
            return False
        if not options:
            clear_stale_offer()
            return False
        current = current_semester_id(options)
        semester_snapshot = (options, current)
        _setup_semesters[user_id] = semester_snapshot
        text, markup = setup_view(user_id, options, current)
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
            clear_stale_offer()
            return False
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=markup)
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
            clear_stale_offer()
            return False
    except Exception:
        clear_stale_offer()  # not shown: the next sign-in or /start tries again
        logging.warning("Setup: could not ask %s", user_id, exc_info=True)
        return False
    return True


async def _redraw(query, text: str, markup=None) -> None:
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as e:  # "message is not modified" after tapping the option already picked
        if "not modified" not in str(e).lower():
            raise


async def _finish(query, user_id: int) -> None:
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        await query.answer()
        return
    missing = missing_answers(user_id)
    if missing:
        await query.answer("Please answer first: " + ", ".join(TITLES[k] for k in missing)[:180], show_alert=True)
        return
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        await query.answer()
        return
    if not mark_setup_done(user_id):
        await query.answer("Couldn't save setup completion. Please try again.", show_alert=True)
        return
    await query.answer("Saved")
    _setup_offered.pop(user_id, None)
    _setup_semesters.pop(user_id, None)
    await _redraw(query, done_text(user_id))


async def handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """setup|<question>|<value>, setup|info|<question>, setup|done, and the
    older setup_sem|<id>."""
    query = update.callback_query
    if query is None:
        return
    with acting_as(query.from_user.id):
        await _handle_setup(update, context)


async def _handle_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer()
        return
    data = query.data or ""
    if data.startswith("setup_sem|"):
        data = "setup|sem|" + data.split("|", 1)[1]
    parts = data.split("|", 2)
    action = parts[1] if len(parts) > 1 else ""
    if action == "done":
        await _finish(query, user_id)
        return
    if action == "info":
        key = parts[2] if len(parts) > 2 else ""
        await query.answer(INFO.get(key, "")[:200] or None, show_alert=key in INFO)
        return
    value = parts[2] if len(parts) > 2 else ""
    try:
        options, current = await _semesters_for(user_id)
    except Exception:
        logging.warning("Setup: semesters unavailable for %s", user_id, exc_info=True)
        await query.answer("Couldn't reach Stud.IP, please try again.", show_alert=True)
        return
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        await query.answer()
        return
    try:
        apply_answer(user_id, action, value, options)
    except ValueError:
        logging.info("Setup: answer refused for %s: %s", user_id, data)
        await query.answer("That option isn't offered any more, run /start again.", show_alert=True)
        return
    except RuntimeError:
        logging.warning("Setup: answer could not be saved for %s", user_id, exc_info=True)
        await query.answer("Couldn't save that choice. Please try again.", show_alert=True)
        return
    await query.answer("Saved")
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return
    text, markup = setup_view(user_id, options, current)
    await _redraw(query, text, markup)


async def handle_setup_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """setup_done from a setup message sent before the one-page setup."""
    query = update.callback_query
    if query is None:
        return
    with acting_as(query.from_user.id):
        await _handle_setup_done(update, context)


async def _handle_setup_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_user_allowed(query.from_user.id):
        await query.answer()
        return
    await _finish(query, query.from_user.id)


def register(app) -> None:
    """Hook the setup into sign-in; the handlers are added by the app."""
    state.on_signed_in = offer_setup
