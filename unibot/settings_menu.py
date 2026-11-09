"""/settings: every per-student preference (unibot.preferences) on inline
buttons, one tap per change.

Callbacks: settings, settings|<page>, settings|cycle|<key>, settings|flip|<key>,
settings|sec|<section>, settings|mt|<earlier|later|off|on>, settings|qh|<preset>.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from unibot import preferences as prefs
from unibot.auth import is_user_allowed
from unibot.storage import get_travel_mode, set_travel_mode

QUIET_PRESETS = {
    "off": None,
    "22-07": {"start": "22:00", "end": "07:00"},
    "23-08": {"start": "23:00", "end": "08:00"},
    "00-09": {"start": "00:00", "end": "09:00"},
}
CYCLE_KEYS = {
    "exam_thresholds": prefs.EXAM_THRESHOLD_PRESETS,
    "tuition_reminders": prefs.TUITION_MODES,
    "campus_anchor": prefs.CAMPUS_ANCHORS,
    "diet": prefs.DIETS,
    "delivery_mode": prefs.DELIVERY_MODES,
    "file_delivery": prefs.FILE_DELIVERY,
}
FLIP_KEYS = ("watch_previous_semester", "outfit")


def _onoff(value) -> str:
    return "✅ On" if value else "🔲 Off"


def _back(page: str = "") -> list:
    return [InlineKeyboardButton("⬅️ Back", callback_data=f"settings|{page}" if page else "settings")]


def _quiet_label(quiet) -> str:
    return f"{quiet['start']} to {quiet['end']}" if quiet else "off"


def main_view(user_id: int) -> tuple:
    p = prefs.get_prefs(user_id)
    morning = "off" if p["morning_time"] == "off" else p["morning_time"]
    text = (
        "⚙️ <b>Settings</b>\n\n"
        f"🎓 Exam reminders: {prefs.EXAM_THRESHOLD_LABELS[p['exam_thresholds']]}\n"
        f"🏫 Campus: {prefs.CAMPUS_ANCHORS[p['campus_anchor']]} · 🍽️ {prefs.DIETS[p['diet']]}\n"
        f"🌅 Morning summary: {morning}\n"
        f"🌙 Quiet hours: {_quiet_label(p['quiet_hours'])} · 📬 {prefs.DELIVERY_MODES[p['delivery_mode']]}"
    )
    rows = [
        [InlineKeyboardButton("🎓 Academic", callback_data="settings|acad"),
         InlineKeyboardButton("🏫 Daily life", callback_data="settings|life")],
        [InlineKeyboardButton("🌅 Morning summary", callback_data="settings|morning"),
         InlineKeyboardButton("🔔 Notifications", callback_data="settings|notif")],
        [InlineKeyboardButton("📚 Per-course alerts", callback_data="calert"),
         InlineKeyboardButton("🔒 Privacy", callback_data="privacy")],
    ]
    return text, InlineKeyboardMarkup(rows)


def academic_view(user_id: int) -> tuple:
    p = prefs.get_prefs(user_id)
    text = (
        "🎓 <b>Academic</b>\n\n"
        f"👀 Previous semester courses: {_onoff(p['watch_previous_semester'])}\n"
        f"⏳ Exam reminders: {prefs.EXAM_THRESHOLD_LABELS[p['exam_thresholds']]}\n"
        f"💶 Unpaid fee reminders: {prefs.TUITION_MODES[p['tuition_reminders']]}\n\n"
        "<i>A paid fee is noticed on StuMS by itself and the reminders stop.</i>"
    )
    rows = [
        [InlineKeyboardButton(f"👀 Previous semester: {_onoff(p['watch_previous_semester'])}",
                              callback_data="settings|flip|watch_previous_semester")],
        [InlineKeyboardButton("⏳ Change exam reminders", callback_data="settings|cycle|exam_thresholds")],
        [InlineKeyboardButton("💶 Change fee reminders", callback_data="settings|cycle|tuition_reminders")],
        [InlineKeyboardButton("📚 Per-course alerts (files, forum, mute)", callback_data="calert")],
        _back(),
    ]
    return text, InlineKeyboardMarkup(rows)


def life_view(user_id: int) -> tuple:
    p = prefs.get_prefs(user_id)
    travel = get_travel_mode(user_id)
    text = (
        "🏫 <b>Daily life</b>\n\n"
        f"🏫 Home campus: {prefs.CAMPUS_ANCHORS[p['campus_anchor']]}\n"
        "<i>Used for study spots and the mensa when your classes don't say where you are.</i>\n"
        f"🍽️ Diet: {prefs.DIETS[p['diet']]}\n"
        "<i>The lunch pick in the morning summary and \"What should I eat?\" only show what fits.</i>\n"
        f"🚲 Between campuses: {'Bike (bus when it rains)' if travel == 'bike' else 'Bus'}\n"
        f"👔 Outfit suggestion: {_onoff(p['outfit'])}"
    )
    rows = [
        [InlineKeyboardButton("🏫 Change campus", callback_data="settings|cycle|campus_anchor")],
        [InlineKeyboardButton("🍽️ Change diet", callback_data="settings|cycle|diet")],
        [InlineKeyboardButton("🚌 Switch to bus" if travel == "bike" else "🚲 Switch to bike",
                              callback_data="settings|travel")],
        [InlineKeyboardButton(f"👔 Outfit suggestion: {_onoff(p['outfit'])}", callback_data="settings|flip|outfit")],
        _back(),
    ]
    return text, InlineKeyboardMarkup(rows)


def morning_view(user_id: int) -> tuple:
    p = prefs.get_prefs(user_id)
    on = p["morning_time"] != "off"
    sections = p["morning_sections"]
    text = (
        "🌅 <b>Morning summary</b>\n\n"
        f"🕖 Time: <b>{p['morning_time'] if on else 'off'}</b> (07:00 to 09:30, 15 minute steps)\n"
        "Tap a part to show or hide it:"
    )
    rows = []
    if on:
        rows.append([InlineKeyboardButton("➖ 15 min", callback_data="settings|mt|earlier"),
                     InlineKeyboardButton(p["morning_time"], callback_data="settings|morning"),
                     InlineKeyboardButton("➕ 15 min", callback_data="settings|mt|later")])
        for key, label in prefs.MORNING_SECTIONS.items():
            rows.append([InlineKeyboardButton(f"{'✅' if sections[key] else '🔲'} {label}",
                                              callback_data=f"settings|sec|{key}")])
        rows.append([InlineKeyboardButton("🔕 Turn the morning summary off", callback_data="settings|mt|off")])
    else:
        rows.append([InlineKeyboardButton("🌅 Turn it on (07:00)", callback_data="settings|mt|on")])
    rows.append(_back())
    return text, InlineKeyboardMarkup(rows)


def notifications_view(user_id: int) -> tuple:
    p = prefs.get_prefs(user_id)
    text = (
        "🔔 <b>Notifications</b>\n\n"
        f"🌙 Quiet hours: <b>{_quiet_label(p['quiet_hours'])}</b>\n"
        "<i>Course updates arriving then wait and come together after your morning summary.</i>\n"
        f"📬 Course updates: {prefs.DELIVERY_MODES[p['delivery_mode']]}\n"
        f"📄 New files: {prefs.FILE_DELIVERY[p['file_delivery']]}\n\n"
        "<i>Grades, exam and fee alerts always come straight to you.</i>"
    )
    current = next((k for k, v in QUIET_PRESETS.items() if v == p["quiet_hours"]), None)
    quiet_row = [InlineKeyboardButton(("• " if key == current else "") + (label if key != "off" else "Off"),
                                      callback_data=f"settings|qh|{key}")
                 for key, label in (("off", "Off"), ("22-07", "22-07"), ("23-08", "23-08"), ("00-09", "00-09"))]
    rows = [
        quiet_row,
        [InlineKeyboardButton("📬 Instant / evening digest", callback_data="settings|cycle|delivery_mode")],
        [InlineKeyboardButton("📄 Change how new files arrive", callback_data="settings|cycle|file_delivery")],
        _back(),
    ]
    return text, InlineKeyboardMarkup(rows)


PAGES = {"": main_view, "acad": academic_view, "life": life_view, "morning": morning_view, "notif": notifications_view}
PAGE_OF = {"watch_previous_semester": "acad", "exam_thresholds": "acad", "tuition_reminders": "acad",
           "campus_anchor": "life", "diet": "life", "outfit": "life",
           "delivery_mode": "notif", "file_delivery": "notif"}


def _shift_morning(user_id: int, direction: str) -> None:
    options = prefs.morning_time_options()
    current = prefs.get_pref(user_id, "morning_time")
    index = options.index(current) if current in options else 0
    index = max(0, index - 1) if direction == "earlier" else min(len(options) - 1, index + 1)
    prefs.set_pref(user_id, "morning_time", options[index])


def apply_action(user_id: int, parts: list) -> str:
    """Apply one settings callback; returns the page to show next."""
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    if action in PAGES:
        return action
    if action == "cycle" and arg in CYCLE_KEYS:
        prefs.cycle_choice(user_id, arg, CYCLE_KEYS[arg])
        return PAGE_OF[arg]
    if action == "flip" and arg in FLIP_KEYS:
        prefs.set_pref(user_id, arg, not prefs.get_pref(user_id, arg))
        if arg == "watch_previous_semester":
            from unibot import state
            state.courses_map = {}  # the watcher map is rebuilt on the next cycle
        return PAGE_OF[arg]
    if action == "travel":
        set_travel_mode(user_id, "bus" if get_travel_mode(user_id) == "bike" else "bike")
        return "life"
    if action == "sec" and arg in prefs.MORNING_SECTIONS:
        sections = dict(prefs.get_pref(user_id, "morning_sections"))
        sections[arg] = not sections[arg]
        prefs.set_pref(user_id, "morning_sections", sections)
        return "morning"
    if action == "mt":
        if arg in ("earlier", "later"):
            _shift_morning(user_id, arg)
        elif arg == "off":
            prefs.set_pref(user_id, "morning_time", "off")
        elif arg == "on":
            prefs.set_pref(user_id, "morning_time", "07:00")
        return "morning"
    if action == "qh" and arg in QUIET_PRESETS:
        prefs.set_pref(user_id, "quiet_hours", QUIET_PRESETS[arg])
        return "notif"
    return ""


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settings - open the settings menu."""
    user, message = update.effective_user, update.message
    if user is None or message is None or not is_user_allowed(user.id):
        return
    text, markup = main_view(user.id)
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return
    parts = (query.data or "").split("|")
    try:
        page = apply_action(user_id, parts)
    except ValueError as e:
        logging.warning("Settings change rejected: %s", e)
        await query.answer("That value isn't allowed.")
        return
    await query.answer()
    text, markup = PAGES[page](user_id)
    if len(parts) == 1:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:  # "message is not modified" when nothing changed
            logging.debug("Settings redraw skipped: %s", html.escape(str(e)))
