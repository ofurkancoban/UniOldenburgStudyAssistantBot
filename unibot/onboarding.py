"""First-run setup: right after a student's first sign-in (or their next
/start while signed in) the bot asks for their preferences, starting with
the semester. The picked semester becomes the default that Files, Browse
Courses and Sign Out open directly; the rest are the /settings pages.

Part of the unibot package.
"""

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from fast_enroll import current_semester_id, get_semester_options

from unibot import state
from unibot.auth import get_user_session, has_stored_login, is_user_allowed, login_studip
from unibot.menus import start
from unibot.settings_menu import main_view
from unibot.storage import mark_setup_done, save_default_semester, setup_done

SETUP_SEMESTER_CHOICES = 4  # the current term and the ones around it
SETUP_ASK_EACH_TIME = "ask"


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


def semester_step_view(options: list, current: Optional[str]) -> tuple:
    rows = []
    for sid, label, is_current in semester_choices(options, current):
        text = f"⭐ {label} (current)" if is_current else label
        rows.append([InlineKeyboardButton(text[:64], callback_data=f"setup_sem|{sid}")])
    rows.append([InlineKeyboardButton("🗓️ Ask me each time", callback_data=f"setup_sem|{SETUP_ASK_EACH_TIME}")])
    text = (
        "🧭 <b>Quick setup</b> (1/2)\n\n"
        "<b>Which semester are you in?</b>\n"
        "📁 Files, Browse Courses and Sign Out will open this semester directly. "
        "You can change it any time in /status."
    )
    return text, InlineKeyboardMarkup(rows)


def preferences_step_view(user_id: int, semester_line: str) -> tuple:
    settings_text, settings_markup = main_view(user_id)
    rows = list(settings_markup.inline_keyboard) + [[InlineKeyboardButton("✅ Done", callback_data="setup_done")]]
    text = (
        f"🧭 <b>Quick setup</b> (2/2)\n{semester_line}\n\n"
        "These are your other preferences. Tap a section to change it, or <b>Done</b> to keep the defaults. "
        "They stay in /settings.\n\n" + settings_text
    )
    return text, InlineKeyboardMarkup(rows)


async def _semester_options(user_id: int, session=None) -> list:
    session = session or get_user_session(user_id)
    if session is None or not await session.is_logged_in():
        session = await login_studip(user_id=user_id)
    return await get_semester_options(session)


async def offer_setup(bot, user_id: int, session=None) -> bool:
    """Ask the first setup question unless the student already answered it.
    Returns whether it was asked."""
    if setup_done(user_id):
        return False
    try:
        options = await _semester_options(user_id, session)
    except Exception:
        logging.warning("Setup: semesters unavailable for %s", user_id, exc_info=True)
        return False
    if not options:
        return False
    text, markup = semester_step_view(options, current_semester_id(options))
    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=markup)
    return True


async def start_with_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start: the dashboard, then the setup for a signed-in student who
    hasn't done it yet (a student who isn't signed in gets it after /login)."""
    await start(update, context)
    user = update.effective_user
    if user is None or not is_user_allowed(user.id) or setup_done(user.id):
        return
    session = get_user_session(user.id)
    if session is not None and has_stored_login(session):
        try:
            await offer_setup(context.bot, user.id, session)
        except Exception:
            logging.warning("Setup offer after /start failed for %s", user.id, exc_info=True)


async def handle_setup_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """setup_sem|<semester id> or setup_sem|ask."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer()
        return
    choice = (query.data or "").split("|", 1)[1] if "|" in (query.data or "") else ""
    if choice == SETUP_ASK_EACH_TIME:
        await query.answer()
        semester_line = "🗓️ Semester: <b>ask each time</b>"
    else:
        try:
            options = await _semester_options(user_id)
        except Exception:
            logging.warning("Setup: semesters unavailable for %s", user_id, exc_info=True)
            await query.answer("Couldn't reach Stud.IP, please try again.", show_alert=True)
            return
        label = next((lbl for sid, lbl in options if sid == choice), None)
        if label is None:
            await query.answer("That semester isn't offered any more, run /start again.", show_alert=True)
            return
        await query.answer("Saved")
        save_default_semester(choice, label, user_id=user_id)
        if state.global_session is not None and getattr(state.global_session, "user_id", None) == user_id:
            state._courses_map_built_with_prev = None  # the watcher's own student: rebuild its course list
        semester_line = f"✅ Semester: <b>{html.escape(label)}</b>"
    mark_setup_done(user_id)
    text, markup = preferences_step_view(user_id, semester_line)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_setup_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✅ <b>Setup complete.</b>\nChange anything later in /settings, or the semester in /status.",
        parse_mode="HTML")


def register(app) -> None:
    """Hook the setup into sign-in; the handlers are added by the app."""
    state.on_signed_in = offer_setup
