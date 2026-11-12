"""Main menu, reply keyboard, callback router, status buttons and text-input wizards.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import time
from typing import Any, Optional
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from exam_reminder import filter_exams_by_module_codes, get_all_exam_dates
from fast_enroll import get_semester_options, semester_display
from studip_utils import berlin_now, html_escape as _h, redact_url

from unibot import db, state
from unibot.config import (
    WA_HEADERS,
    WA_SERVICE_URL,
    get_wa_group_id,
)
from unibot.preferences import get_pref, set_pref
from unibot.storage import (
    cleanup_link_cache,
    student_courses,
    get_travel_mode,
    get_user_name,
    set_travel_mode,
    link_cache,
    link_cache_owned_by,
    load_default_semester,
    rebuild_watched_courses,
    user_tasks,
    save_studip_user_name,
    save_user_name,
    shared_http,
    task_due,
)
from unibot.formatting import _short_id, clean_html_text, escaped_chunks
from unibot.telegram_ui import (
    user_data,
    _breadcrumb,
    _delete_status_message,
    _reply_or_edit,
    _send_file_bytes,
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
    broadcast,
    get_main_keyboard,
    MAIN_KEYBOARD_LABELS,
    nav_names,
)
from unibot.auth import (
    _perform_logout,
    _send_login_prompt,
    get_user_session,
    has_stored_login,
    is_user_admin,
    is_user_allowed,
    login_studip,
)
from unibot.studip_api import (
    DownloadTooLargeError,
    LoginPageResponseError,
    ZIP_RAW_LIMIT_BYTES,
    _api_message,
    _api_names,
    _normalize_url,
    create_recursive_zip,
    get_fresh_file_url,
    get_studip_user_profile,
    list_courses,
    list_files,
    read_response_limited,
    studip_api_get,
)
from unibot.weather import geocode_location, save_weather_location
from unibot.mensa import (
    effective_food_filter,
    get_menu_navigation_keyboard,
    get_todays_menu_enhanced,
    menu_command,
    send_menu_content,
)
from unibot.schedule import get_calendar_events, send_daily_calendar
from unibot.monitoring import (
    check_command,
    send_studip_message,
    show_last_announcements,
    show_last_files,
    show_last_messages,
)
from unibot.exams import exams_command, get_my_module_codes
from unibot.study_plan import progress_command, study_plan_command, track_courses_command
from unibot.tasks import _handle_task_wizard_reply, send_tasks_menu
from unibot.enrollment import _handle_fastenroll_wizard_reply, send_fastenroll_list
from unibot.voice import nav_stack
from unibot.system import status_command


user_courses: dict[int, str] = {}  # user_id -> cid
START_MENU_DEDUP_SECONDS = 30


def _status_group_view(group: str, user_id: Optional[int] = None) -> tuple:
    """One /status submenu. The buttons reuse the existing callbacks, except
    the on/off toggles, which go through status_group_toggle so the
    submenu redraws in place instead of being replaced by a confirmation."""
    if group == "profile":
        studip_name = get_user_name(user_id) if user_id is not None else None
        if studip_name:
            name_line = f"Name: <b>{_h(studip_name)}</b>\n<i>(Automatically synced from Stud.IP)</i>"
        elif user_id is not None and has_stored_login(get_user_session(user_id)):
            name_line = "Name: <i>not synced from Stud.IP yet, try again in a minute</i>"
        else:
            name_line = "Name: <i>not signed in, use /login</i>"
        title = f"👤 <b>Profile</b>\n\n{name_line}"
        rows = [
            [InlineKeyboardButton("📍 Set Location", callback_data="set_weather_location")],
        ]
    elif group == "semester":
        watch_prev = get_pref(user_id, "watch_previous_semester")
        travel = get_travel_mode(user_id)
        travel_label = "🚲 Bike (15 min, bus when it rains)" if travel == "bike" else "🚌 Bus (20 min)"
        title = (f"📆 <b>Semester &amp; Calendar</b>\n\nWatch Previous Semester Too: {'✅ On' if watch_prev else '🔲 Off'}"
                 f"\nBetween Haarentor and Wechloy: {travel_label}")
        rows = [
            [InlineKeyboardButton("🗓️ Timetable", callback_data="calendar_timetable"), InlineKeyboardButton("📅 Weekly View", callback_data="calendar_weekly")],
            [InlineKeyboardButton("📆 Default Semester", callback_data="set_default_semester")],
            [InlineKeyboardButton(f"{'✅' if watch_prev else '🔲'} Watch Previous Semester Too", callback_data="status_group_toggle|semester|watch_prev")],
            [InlineKeyboardButton("🚌 Switch to Bus" if travel == "bike" else "🚲 Switch to Bike",
                                  callback_data="status_group_toggle|semester|travel")],
            [InlineKeyboardButton("📅 Calendar Sync", callback_data="cal_sync"),
             InlineKeyboardButton("👥 Study Buddy", callback_data="buddy")],
            [InlineKeyboardButton("🔔 Course Alerts", callback_data="calert"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        ]
    else:
        title = "📄 <b>Official Documents &amp; Tuition</b>\n\nDownload official StuMS PDF certificates, transcript, and semester fee status:"
        rows = [
            [InlineKeyboardButton("📄 Immatrikulationsbescheinigung", callback_data="stums_doc|imma")],
            [InlineKeyboardButton("📈 Studienverlaufsbescheinigung", callback_data="stums_doc|verlauf")],
            [InlineKeyboardButton("🇬🇧 Transcript of Records", callback_data="stums_doc|transcript_en")],
            [InlineKeyboardButton("💶 Tuition & Fees", callback_data="tuition_status"), InlineKeyboardButton("📊 Grades Summary", callback_data="transcript_summary")],
            [InlineKeyboardButton("🔒 Privacy", callback_data="privacy")],
        ]
    return title, InlineKeyboardMarkup(rows)


async def _sync_profile_name(user_id: int) -> None:
    """Fetch this student's own Stud.IP profile and keep its name. The
    profile is theirs: their own session's, or the watcher's when the
    watcher runs on their session (studip_api._uses_global_profile)."""
    try:
        profile = await asyncio.wait_for(get_studip_user_profile(user_id=user_id), 15)
    except Exception:
        logging.debug("Stud.IP profile lookup failed", exc_info=True)
        return
    name = (profile or {}).get("display_name")
    if name and not get_user_name(user_id):
        save_studip_user_name(name, user_id=user_id)


async def handle_status_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """status_group|<group> opens a /status submenu as a new message
    (keeping /status itself intact); status_group_toggle|<group>|<feature>
    flips a setting and redraws that submenu in place."""
    query = update.callback_query
    await query.answer()
    if not is_user_allowed(query.from_user.id):
        return
    parts = query.data.split("|")
    group = parts[1] if len(parts) > 1 else "profile"
    uid = query.from_user.id
    if group == "profile" and not get_user_name(uid) and has_stored_login(get_user_session(uid)):
        await _sync_profile_name(uid)
    if parts[0] == "status_group_toggle":
        feature = parts[2] if len(parts) > 2 else ""
        if feature == "watch_prev":
            uid = query.from_user.id
            set_pref(uid, "watch_previous_semester", not get_pref(uid, "watch_previous_semester"))
            rebuild_watched_courses(uid)
        elif feature == "study_plan":
            uid = query.from_user.id
            set_pref(uid, "weekly_study_plan", not get_pref(uid, "weekly_study_plan"))
        elif feature == "travel":
            set_travel_mode(query.from_user.id, "bus" if get_travel_mode(query.from_user.id) == "bike" else "bike")
        text, keyboard = _status_group_view(group, uid)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
    text, keyboard = _status_group_view(group, uid)
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for menu navigation items"""
    query = update.callback_query
    # Split data by | to support menu_nav|path
    parts = query.data.split("|")
    sub_path = parts[1] if len(parts) > 1 else "2/"

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return

    try:
        # Show specific loading status for the button pressed
        direction = "..."
        if "next" in sub_path: direction = "Next day..."
        elif "previous" in sub_path: direction = "Previous day..."
        elif sub_path == "2/": direction = "Today..."

        await query.answer(f"Loading {direction}")

        # Check if we should send a NEW message instead of editing
        # (Useful for morning summary where we want to keep the schedule)
        should_reply = len(parts) > 2 and parts[2] == "new"

        # Session check
        session = await login_studip()

        food = effective_food_filter(user_id)
        menu_text, prev, next_, menu_date, dish_items, header_text = await get_todays_menu_enhanced(
            session, sub_path=sub_path, avoid_codes=food["avoid_codes"], avoid_keywords=food["avoid_keywords"], max_price=food["max_price"]
        )

        if should_reply:
            await send_menu_content(
                query.message, menu_text,
                get_menu_navigation_keyboard(prev, next_, current_path=sub_path),
            )
        else:
            await send_menu_content(
                query.message, menu_text,
                get_menu_navigation_keyboard(prev, next_, current_path=sub_path),
                editor=query,
            )

    except Exception as e:
        error_str = str(e)
        if "Message is not modified" in error_str:
            # the same menu again; the tap was already answered with "Loading"
            return

        error_msg = f"❌ Error loading menu:\n{error_str}"
        # If edit fails (e.g. same text), don't crash
        try:
            await query.edit_message_text(error_msg)
        except TelegramError:
            pass
        logging.error("Menu button navigation error: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
#  TELEGRAM UI + MENU + FOLDER VIEW + INSTANT CHECK
# ──────────────────────────────────────────────────────────────────────────────

async def _send_courses_menu(query_or_message, courses, extra_buttons=None):
    """Send main course list as buttons. extra_buttons, if given, is a list of
    keyboard rows appended after the course rows (e.g. a "Change Semester" row)."""

    if not courses:
        text = "⚠️ <b>No courses found for the current semester.</b>\nPlease check your semester filter settings on the Stud.IP website."
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="show_menu")]] + (extra_buttons or [])
        reply_markup = InlineKeyboardMarkup(keyboard)
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return

    # Safety limit to avoid "Reply markup is too long" Telegram error
    MAX_COURSES = 80
    if len(courses) > MAX_COURSES:
        logging.warning("Too many courses (%s), limiting to %s", len(courses), MAX_COURSES)
        courses = courses[:MAX_COURSES]
        text = f"📚 <b>Select a course (Showing first {MAX_COURSES}):</b>"
    else:
        text = "📚 <b>Select a course to browse its files:</b>"

    logging.info("Sending courses menu with %s buttons.", len(courses))

    keyboard = []
    for name, cid in courses:
        # a button label is plain text: no HTML escaping, or "&" shows as "&amp;"
        keyboard.append([InlineKeyboardButton(f"📘 {name[:64]}", callback_data=f"course|{cid}")])

    keyboard.extend(extra_buttons or [])

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        if "Reply markup is too long" in str(e):
            logging.error("FATAL: Keyboard still too long for %s courses! %s", len(courses), e)
            # Dynamic fallback: send even fewer
            if len(courses) > 20:
                await _send_courses_menu(query_or_message, courses[:20], extra_buttons=extra_buttons)
            else:
                if hasattr(query_or_message, "reply_text"):
                    await query_or_message.reply_text("⚠️ Too many courses to display in one menu. Please select a specific semester on the website.")
        else:
            raise e


async def _show_semester_picker(sender, callback_prefix: str, prompt: str):
    """Fetch semesters and show a picker whose buttons carry
    "<callback_prefix>|<semester_id>". Shared by the three "browse" entry
    points (Files/Enroll/Sign Out) for their initial pick, and by the
    "📆 Change Semester" button they each offer afterwards to override it."""
    uid = None
    if hasattr(sender, "from_user") and sender.from_user:
        uid = sender.from_user.id
    elif hasattr(sender, "effective_user") and sender.effective_user:
        uid = sender.effective_user.id
    try:
        user_sess = get_user_session(uid) if uid else None
        session = user_sess or state.global_session
        if not session or not await session.is_logged_in():
            session = await login_studip(user_id=uid)
        semesters = await get_semester_options(session)
    except Exception as e:
        logging.error("_show_semester_picker: failed to fetch semesters: %s", e)
        err_msg = str(e).lower()
        if uid and ("login" in err_msg or "auth" in err_msg or "session" in err_msg):
            target = sender.message if hasattr(sender, "message") else sender
            await _reply_or_edit(
                sender,
                "🔐 <b>Stud.IP Login Required</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Your session is not active or has expired. Please sign in to browse semesters and files:",
                reply_markup=None,
            )
            await _send_login_prompt(target, uid)
            return
        await _reply_or_edit(sender, f"❌ Could not load semesters: {str(e)[:200]}", reply_markup=InlineKeyboardMarkup([]))
        return

    if not semesters:
        await _reply_or_edit(sender, "❌ Could not find any semesters.", reply_markup=InlineKeyboardMarkup([]))
        return

    keyboard = [[InlineKeyboardButton(semester_display(label), callback_data=f"{callback_prefix}|{sid}")]
                for sid, label in semesters]
    await _reply_or_edit(sender, prompt, reply_markup=InlineKeyboardMarkup(keyboard))


async def _render_files_courses_for_semester(sender, semester_id: str):
    """List a given semester's courses so the student can pick one to browse
    its files. Shared by handle_files_semester_pick (explicit picker) and
    handle_files_browse (when a default semester is set, skipping the picker)."""
    uid = None
    if hasattr(sender, "from_user") and sender.from_user:
        uid = sender.from_user.id
    elif hasattr(sender, "effective_user") and sender.effective_user:
        uid = sender.effective_user.id

    user_sess = get_user_session(uid) if uid else None
    target = sender.message if hasattr(sender, "message") else sender
    try:
        courses = await list_courses(semester_id=semester_id, session=user_sess, user_id=uid)
    except Exception as e:
        logging.error("_render_files_courses_for_semester: failed to fetch courses: %s", e)
        err_msg = str(e).lower()
        if uid and ("login" in err_msg or "auth" in err_msg or "session" in err_msg):
            await _reply_or_edit(
                sender,
                "🔐 <b>Stud.IP Login Required</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Your session is not active or has expired. Please sign in to access your course files:",
                reply_markup=None,
            )
            await _send_login_prompt(target, uid)
            return
        await _reply_or_edit(sender, f"❌ Error: {str(e)[:200]}", reply_markup=InlineKeyboardMarkup([]))
        return

    if not courses and uid:
        is_logged = await user_sess.is_logged_in() if user_sess else False
        if not is_logged:
            await _reply_or_edit(
                sender,
                "🔐 <b>Stud.IP Login Required</b>\n━━━━━━━━━━━━━━━━━━\n"
                "You are not currently logged in to Stud.IP.\nPlease sign in below to view your enrolled courses and files:",
                reply_markup=None,
            )
            await _send_login_prompt(target, uid)
            return

    extra_buttons = [
        [
            InlineKeyboardButton("📆 Change Semester", callback_data="change_sem|files"),
            InlineKeyboardButton("🎯 Track Courses", callback_data="sp_track_menu"),
        ]
    ]
    await _send_courses_menu(sender, courses, extra_buttons=extra_buttons)


async def handle_files_browse(sender):
    """Handle the "⬇️ Files" reply-keyboard button. If a default semester is
    set (via /status's "Set Default Semester"), skip straight to that
    semester's course list; otherwise show a semester picker first, instead of
    always defaulting to whatever semester the account's my_courses filter
    happened to be on."""
    default = load_default_semester()
    if default.get("id"):
        await _render_files_courses_for_semester(sender, default["id"])
        return

    await _show_semester_picker(sender, "files_sem", "⬇️ Pick a semester to browse its courses' files:")


async def handle_files_semester_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle files_sem|<id> callback - list that semester's courses so the
    student can pick one to browse its files."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    semester_id = query.data.split("|", 1)[1]
    await _render_files_courses_for_semester(query, semester_id)


async def send_folder(query, files, cid, user_id, current_url=None):
    """Display folder content with improved cache handling."""
    keyboard = []

    # Do cache cleanup first
    cleanup_link_cache()

    # Process in single list preserving original order
    for f in files:
        sid = _short_id()

        # Cache folder and file info - CRITICAL FIX
        cache_entry = {
            "cid": cid,
            "name": f["name"],
            "ts": time.time(),
            "user_id": user_id,
            # A stale download URL must be refreshed from the directory that
            # contained this item; looking only at the course root fails for
            # files inside nested folders.
            "current_url": current_url,
        }

        # Normalize the URL and store it in the link cache
        if f.get("url"):
            cache_entry["url"] = _normalize_url(f["url"])
            logging.info("💾 Caching %s: %s -> %s", f['type'], f['name'], cache_entry['url'])

        link_cache[sid] = cache_entry

        if f["type"] == "folder":
            keyboard.append([InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"folder|{sid}")])
        elif f["type"] == "file":
            meta = f" ({f['size']}, {f['modified']})"
            keyboard.append([InlineKeyboardButton(f"📄 {f['name']}{meta}", callback_data=f"file|{sid}")])

    # Navigation buttons
    nav_btns = []
    stack_len = len(nav_stack.get(user_id, []))

    if stack_len <= 1:
        courses_id = _short_id()
        link_cache[courses_id] = {
            "cid": cid,
            "user_id": user_id,
            "action": "courses",
            "ts": time.time()
        }
        nav_btns.append(InlineKeyboardButton("📚 All Courses", callback_data=f"nav|{courses_id}"))
    else:
        back_id = _short_id()
        home_id = _short_id()
        courses_id = _short_id()
        now_ts = time.time()

        link_cache[back_id] = {"cid": cid, "user_id": user_id, "action": "back", "ts": now_ts}
        link_cache[home_id] = {"cid": cid, "user_id": user_id, "action": "home", "ts": now_ts}
        link_cache[courses_id] = {"cid": cid, "user_id": user_id, "action": "courses", "ts": now_ts}

        nav_btns.extend([
            InlineKeyboardButton("⬅️ Back", callback_data=f"nav|{back_id}"),
            InlineKeyboardButton("🏠 Course Root", callback_data=f"nav|{home_id}"),
            InlineKeyboardButton("📚 All Courses", callback_data=f"nav|{courses_id}")
        ])

    keyboard.append(nav_btns)

    # ZIP download button (only if valid URL exists)
    if current_url:
        zip_id = _short_id()
        link_cache[zip_id] = {
            "cid": cid,
            "user_id": user_id,
            "action": "zip",
            "current_url": current_url,
            "ts": time.time()
        }
        keyboard.append([InlineKeyboardButton("📦 Download Entire Folder (ZIP)", callback_data=f"nav|{zip_id}")])

    cleanup_link_cache()  # once more now that this view's entries are added (the size cap)

    # Title (breadcrumb)
    title = _breadcrumb(user_id)
    folder_count = len([f for f in files if f["type"] == "folder"])
    file_count = len([f for f in files if f["type"] == "file"])
    caption = f"{title}\n\n📁 *{folder_count} folders* | 📄 *{file_count} files*"

    await query.edit_message_text(
        caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _status_button_studip_login(update, context, query):
    """Status button handlers: studip_login."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    await _send_login_prompt(query.message, user_id)


async def _status_button_studip_logout(update, context, query):
    """Status button handlers: studip_logout."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    await _perform_logout(user_id=user_id)
    try:
        if query.message and query.message.text:
            updated_html = query.message.text_html
            if "🔑 Logged in: ✅ Yes" in updated_html:
                updated_html = updated_html.replace("🔑 Logged in: ✅ Yes", "🔑 Logged in: ❌ No")
            if "🔑 <b>Connection:</b> ✅ Connected" in updated_html:
                updated_html = updated_html.replace("✅ Connected", "❌ Not connected")
            await query.edit_message_text(
                updated_html,
                parse_mode="HTML",
                reply_markup=query.message.reply_markup,
            )
    except TelegramError:
        pass
    await query.message.reply_text(
        "🚪 Logged out from Stud.IP. Saved session cookies and OAuth2 tokens have been cleared.\n"
        "Tap 🔐 Login or use /login whenever you want to sign in again."
    )


async def _status_button_fastenroll_menu(update, context, query):
    """Status button handlers: fastenroll_menu."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    keyboard = [
        [InlineKeyboardButton("➕ New Fast Enroll", callback_data="fastenroll_new")],
        [InlineKeyboardButton("📋 List / Cancel", callback_data="fastenroll_list_inline")],
    ]
    await query.message.reply_text("⚡ Fast Enroll menu:", reply_markup=InlineKeyboardMarkup(keyboard))


async def _status_button_wa_menu(update, context, query):
    """Status button handlers: wa_menu."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    keyboard = [
        [InlineKeyboardButton("📲 Request QR", callback_data="request_wa_qr"), InlineKeyboardButton("🔄 Refresh QR", callback_data="force_wa_qr")],
        [InlineKeyboardButton("🔍 Detect Groups", callback_data="detect_wa_groups"), InlineKeyboardButton("🧪 Test Message", callback_data="wa_test_message")],
    ]
    await query.message.reply_text("📱 <b>WhatsApp Gateway:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def _status_button_mensa_food(update, context, query):
    """Status button handlers: mensa_food."""
    await menu_command(update, context)


async def _status_button_fastenroll_new(update, context, query):
    """Status button handlers: fastenroll_new."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    context.user_data.pop("fastenroll_wizard", None)
    context.user_data["pending_step"] = "fastenroll_date"
    await query.message.reply_text(FASTENROLL_ASK_DATE, reply_markup=get_main_keyboard())


async def _status_button_fastenroll_list_inline(update, context, query):
    """Status button handlers: fastenroll_list_inline."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    await send_fastenroll_list(query.message, query.message.chat_id)


async def _status_button_start_watchers(update, context, query):
    """Status button handlers: start_watchers."""
    state.global_watcher_paused = False  # the one flag the watcher controller and the message poll read
    await query.edit_message_text("▶️ All watchers resumed.")


async def _status_button_stop_watchers(update, context, query):
    """Status button handlers: stop_watchers."""
    state.global_watcher_paused = True
    await query.edit_message_text("⏸️ All watchers paused.")


async def _status_button_request_wa_qr(update, context, query):
    """Status button handlers: request_wa_qr."""
    try:
        async with shared_http() as session:
            async with session.post(f"{WA_SERVICE_URL}/request_qr", headers=WA_HEADERS) as resp:
                data = await resp.json()
                await query.message.reply_text(f"ℹ️ {data.get('message', 'QR request processed.')}")
    except Exception as e:
        logging.error("Failed to request WA QR: %s", e)
        await query.message.reply_text("❌ WhatsApp service unreachable. Make sure it's running.")


async def _status_button_force_wa_qr(update, context, query):
    """Status button handlers: force_wa_qr."""
    # Logs the current WhatsApp session out first, so a real QR is generated
    # even if the client is currently connected (plain /request_qr silently
    # restores a still-valid saved session instead of producing a QR).
    try:
        async with shared_http() as session:
            async with session.post(f"{WA_SERVICE_URL}/force_new_qr", headers=WA_HEADERS) as resp:
                data = await resp.json()
                await query.message.reply_text(f"ℹ️ {data.get('message', 'QR request processed.')}")
    except Exception as e:
        logging.error("Failed to force new WA QR: %s", e)
        await query.message.reply_text("❌ WhatsApp service unreachable. Make sure it's running.")


async def _status_button_wa_test_message(update, context, query):
    """Status button handlers: wa_test_message."""
    # Sends to the linked account's own chat ("Message yourself"), never
    # to the shared group, so it's safe to tap any time.
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return
    try:
        async with shared_http() as session:
            async with session.post(f"{WA_SERVICE_URL}/send_test", headers=WA_HEADERS, timeout=15) as resp:
                data = await resp.json()
        if data.get("success"):
            await query.message.reply_text(f"✅ Test message sent to your own WhatsApp chat ({data.get('to', 'own number')}).")
        else:
            await query.message.reply_text(f"❌ WhatsApp test failed: {data.get('error', 'unknown error')}")
    except Exception as e:
        logging.error("WhatsApp test message failed: %s", e)
        await query.message.reply_text("❌ WhatsApp service unreachable. Make sure it's running.")


async def _status_button_detect_wa_groups(update, context, query):
    """Status button handlers: detect_wa_groups."""
    try:
        async with shared_http() as session:
            async with session.get(f"{WA_SERVICE_URL}/discovered_groups", headers=WA_HEADERS, timeout=10) as resp:
                data = await resp.json()
                groups = data.get("groups", [])
    except Exception as e:
        logging.error("Failed to fetch discovered WA groups: %s", e)
        await query.message.reply_text("❌ WhatsApp service unreachable. Make sure it's running.")
        return

    if not groups:
        await query.message.reply_text(
            "ℹ️ No WhatsApp groups detected yet. Send or receive a message in the target group, then try again."
        )
        return

    groups = groups[:10]

    # Resolve real names directly from WhatsApp (getChatById, see
    # /group_name) for any entry the discovered-groups log doesn't
    # have one for yet, so the picker always shows names to tap
    # rather than falling back to a raw ID.
    missing = [g for g in groups if not g.get("name")]
    if missing:
        async def _resolve(g):
            try:
                async with shared_http() as session:
                    async with session.get(f"{WA_SERVICE_URL}/group_name", headers=WA_HEADERS, params={"groupId": g["groupId"]}, timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("name"):
                                g["name"] = data["name"]
                        elif resp.status == 404:
                            g["not_member"] = True
            except Exception:
                logging.debug("WhatsApp group discovery request failed", exc_info=True)
        await asyncio.gather(*(_resolve(g) for g in missing))

    current_id = get_wa_group_id()
    kb = []
    for g in groups:
        label = g.get("name") or g["groupId"]
        if g.get("not_member"):
            label = f"⚠️ {label} (not a member)"
        if g["groupId"] == current_id:
            label = f"✅ {label} (active)"
        kb.append([InlineKeyboardButton(label, callback_data=f"set_wa_group|{g['groupId']}")])

    await query.message.reply_text(
        "📍 Detected WhatsApp groups. Tap one to set it as active:",
        reply_markup=InlineKeyboardMarkup(kb)
    )


_STATUS_BUTTON_HANDLERS = {
    "studip_login": _status_button_studip_login,
    "studip_logout": _status_button_studip_logout,
    "fastenroll_menu": _status_button_fastenroll_menu,
    "wa_menu": _status_button_wa_menu,
    "mensa_food": _status_button_mensa_food,
    "fastenroll_new": _status_button_fastenroll_new,
    "fastenroll_list_inline": _status_button_fastenroll_list_inline,
    "start_watchers": _status_button_start_watchers,
    "stop_watchers": _status_button_stop_watchers,
    "request_wa_qr": _status_button_request_wa_qr,
    "force_wa_qr": _status_button_force_wa_qr,
    "wa_test_message": _status_button_wa_test_message,
    "detect_wa_groups": _status_button_detect_wa_groups,
}


# Buttons that act on the whole bot (watchers, the shared WhatsApp gateway).
_ADMIN_STATUS_BUTTONS = frozenset({
    "wa_menu", "start_watchers", "stop_watchers", "request_wa_qr",
    "force_wa_qr", "wa_test_message", "detect_wa_groups",
})


async def handle_status_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    if data in _ADMIN_STATUS_BUTTONS and not is_user_admin(query.from_user.id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()

    handler = _STATUS_BUTTON_HANDLERS.get(data)
    if handler is not None:
        await handler(update, context, query)


# ── course selection ────────────────────────────────────────────────
async def _selection_course(update, context, query, data, user_id):
    """Inline button callback: course."""
    cid = data[1]
    user_courses[user_id] = cid
    course_name = student_courses(user_id).get(cid, f"Course {cid[:6]}")
    root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
    nav_stack[user_id] = [root_url]
    nav_names[user_id] = [course_name]
    user_sess = get_user_session(user_id)
    files = await list_files(cid, root_url, session=user_sess, user_id=user_id)
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return
    await send_folder(query, files, cid, user_id, current_url=root_url)


# ── folder open ─────────────────────────────────────────────────────
async def _selection_folder(update, context, query, data, user_id):
    """Inline button callback: folder."""
    sid = data[1]
    info = link_cache.get(sid, {})
    if not link_cache_owned_by(info, user_id):
        await query.message.reply_text("⚠️ This folder link belongs to another account.")
        return
    folder_url = info.get("url")
    cid = info.get("cid")
    folder_name = info.get("name", "Folder")

    if not folder_url or not cid:
        await query.message.reply_text("⚠️ Folder link not found or expired.")
        return

    # Normalize URL
    folder_url = _normalize_url(folder_url)

    # Update cache (refresh TTL)
    link_cache[sid] = {
        **info,
        "ts": time.time(),
        "url": folder_url
    }
    cleanup_link_cache()

    nav_stack.setdefault(user_id, []).append(folder_url)
    nav_names.setdefault(user_id, []).append(folder_name)

    user_sess = get_user_session(user_id)
    files = await list_files(cid, folder_url, session=user_sess, user_id=user_id)
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
        return
    await send_folder(query, files, cid, user_id, current_url=folder_url)


# ── file download ───────────────────────────────────────────────────
async def _selection_file(update, context, query, data, user_id):
    """Inline button callback: file."""
    sid = data[1]
    info = link_cache.get(sid, {})
    if not link_cache_owned_by(info, user_id):
        await query.message.reply_text("⚠️ This file link belongs to another account.")
        return
    cid = info.get("cid")
    fname = info.get("name")

    logging.debug("📥 Download request: sid=%s, cid=%s", sid, cid)

    if not cid or not fname:
        await query.message.reply_text("⚠️ Invalid file reference. Cache may have expired.")
        logging.error("❌ Invalid file reference: cid=%s, fname=%s", cid, fname)
        return
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
        return

    # Check cached or current URL
    temp_url = _normalize_url(info.get("url"))
    current_url = info.get("current_url")
    # always the tapping student's own session: Stud.IP decides what they may download
    user_sess = active_session = get_user_session(user_id)

    # Try cached URL first
    if temp_url:
        logging.info("🔄 Trying cached URL: %s", redact_url(temp_url))
        await _show_typing(query, action=ChatAction.UPLOAD_DOCUMENT)
        try:
            async with await active_session.get(temp_url) as resp:
                if resp.status == 200:
                    content = await read_response_limited(resp, 48 * 1024 * 1024)
                    if len(content) > 48 * 1024 * 1024:
                        await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
                        return

                    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                        return

                    await _send_file_bytes(query.message, content, fname, sid)
                    logging.info("✅ Downloaded via cached URL: %s", fname)

                    # Update cache (refresh TTL)
                    if is_user_allowed(user_id) and db.private_context_is_current(user_id):
                        link_cache[sid] = {**info, "ts": time.time()}
                    return
                else:
                    logging.warning("⚠️ Cached URL returned HTTP %s, getting fresh URL", resp.status)
        except DownloadTooLargeError:
            await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
            return
        except LoginPageResponseError:
            await query.message.reply_text("🔐 Your Stud.IP session expired. Sign in with /login, then try downloading again.")
            return
        except Exception as e:
            logging.warning("⚠️ Cached URL failed, getting fresh URL: %s", e)

    # If cached URL doesn't work, get fresh URL. The file card is reused as
    # the status message and put back afterwards, so its buttons stay.
    card_text, card_markup = getattr(query.message, "text_html", None), query.message.reply_markup
    status_msg = await query.edit_message_text(f"🔍 Getting fresh download link for:\n📄 {fname}")
    await _show_typing(query, action=ChatAction.UPLOAD_DOCUMENT)
    stop_evt, anim_task = _start_loading_animation(status_msg, f"Getting fresh download link for:\n📄 {fname}")

    try:
        url = await get_fresh_file_url(cid, fname, current_url, session=user_sess, user_id=user_id)
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return
        if not url:
            await query.message.reply_text(f"⚠️ Could not get download link for:\n{fname}")
            logging.error("❌ No download URL found for: %s", fname)
            return

        # Save URL to cache (for future use)
        link_cache[sid] = {
            **info,
            "url": url,
            "ts": time.time(),
            **({} if info.get("shared") is True or info.get("user_id") is None else {"user_id": user_id})
        }
        cleanup_link_cache()

        try:
            async with await active_session.get(url) as resp:
                if resp.status != 200:
                    await query.message.reply_text(f"⚠️ Download failed (HTTP {resp.status}).")
                    logging.error("❌ Download failed: HTTP %s for %s", resp.status, redact_url(url))
                    return

                content = await read_response_limited(resp, 48 * 1024 * 1024)
                if len(content) > 48 * 1024 * 1024:
                    await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
                    return

                if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                    return

                await _send_file_bytes(query.message, content, fname, sid, caption=f"✅ {fname}")
                logging.info("✅ Successfully downloaded: %s", fname)

        except DownloadTooLargeError:
            await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
        except LoginPageResponseError:
            await query.message.reply_text("🔐 Your Stud.IP session expired. Sign in with /login, then try downloading again.")
        except Exception as e:
            logging.error("❌ Download failed for %s: %s", fname, e)
            await query.message.reply_text(f"⚠️ Download failed:\n{str(e)[:200]}")
    finally:
        await _stop_loading_animation(stop_evt, anim_task)
        if card_text:
            try:
                await query.edit_message_text(card_text, parse_mode="HTML", reply_markup=card_markup)
            except TelegramError as e:
                logging.debug("File card not restored: %s", e)


# ── navigation (back, home, courses, zip) ────────────────────────────
async def _selection_nav(update, context, query, data, user_id):
    """Inline button callback: nav."""
    sid = data[1]
    info = link_cache.get(sid, {})
    if not link_cache_owned_by(info, user_id):
        await query.message.reply_text("⚠️ This navigation link belongs to another account.")
        return
    action = info.get("action")
    cid = info.get("cid")

    if action == "back":
        if len(nav_stack.get(user_id, [])) > 1:
            nav_stack[user_id].pop()
            if nav_names.get(user_id):
                nav_names[user_id].pop()

        if nav_stack.get(user_id):
            back_url = nav_stack[user_id][-1]
            files = await list_files(cid, back_url, session=get_user_session(user_id), user_id=user_id)
            if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                return
            await send_folder(query, files, cid, user_id, current_url=back_url)
        else:
            # Fallback: course root'a git
            root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
            nav_stack[user_id] = [root_url]
            nav_names[user_id] = [student_courses(user_id).get(cid, f"Course {cid[:6]}")]
            user_sess = get_user_session(user_id)
            files = await list_files(cid, root_url, session=user_sess, user_id=user_id)
            if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                return
            await send_folder(query, files, cid, user_id, current_url=root_url)

    elif action == "home":
        root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
        nav_stack[user_id] = [root_url]
        nav_names[user_id] = [student_courses(user_id).get(cid, f"Course {cid[:6]}")]
        user_sess = get_user_session(user_id)
        files = await list_files(cid, root_url, session=user_sess, user_id=user_id)
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return
        await send_folder(query, files, cid, user_id, current_url=root_url)

    elif action == "courses":
        user_sess = get_user_session(user_id)
        courses = await list_courses(session=user_sess, user_id=user_id)
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return
        await _send_courses_menu(query, courses)

    elif action == "zip":
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return
        current_url = info.get("current_url") or (
            nav_stack.get(user_id, [None])[-1] if nav_stack.get(user_id) else None)
        if not current_url:
            await query.message.reply_text("⚠️ No folder selected for ZIP download.")
            return

        root_name = nav_names.get(user_id, ["course"])[-1]

        msg = await query.message.reply_text("📦 Preparing ZIP... (0%)")
        await _show_typing(query, action=ChatAction.UPLOAD_DOCUMENT)

        async def progress(total, current):
            if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                return
            pct = int((current / total) * 100) if total else 100
            try:
                await msg.edit_text(f"📦 Preparing ZIP... ({pct}%)")
            except TelegramError:
                pass

        user_sess = get_user_session(user_id)
        try:
            buf, total, folder_name, success_count, total_size = await create_recursive_zip(
                cid, current_url, root_name=root_name, progress_callback=progress,
                session=user_sess, user_id=user_id,
            )
        except Exception as e:
            logging.warning("ZIP listing failed for user %s, course %s: %s", user_id, cid, e)
            await msg.edit_text("⚠️ Could not read every folder, so no ZIP was created. Please try again.")
            return

        if total == 0:
            await msg.edit_text("⚠️ No downloadable files found.")
            return

        if success_count == 0:
            await msg.edit_text("⚠️ None of the files could be downloaded, so no ZIP was created.")
            return

        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return

        zip_name = f"{root_name}.zip"

        def human_size(bytes_val):
            for unit in ["B", "KB", "MB", "GB"]:
                if bytes_val < 1024:
                    return f"{bytes_val:.1f} {unit}"
                bytes_val /= 1024
            return f"{bytes_val:.1f} TB"

        now_str = berlin_now().strftime("%d %b %Y %H:%M")
        summary = (
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ <b>ZIP CREATED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Folder:</b> {_h(folder_name)}\n"
            f"📄 <b>Files:</b> {success_count}/{total}\n"
            f"💾 <b>Total Size:</b> {human_size(total_size)}\n"
            f"🕒 <b>Created:</b> {now_str}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        if total_size > ZIP_RAW_LIMIT_BYTES or len(buf.getvalue()) > 48 * 1024 * 1024:
            await msg.edit_text("⚠️ ZIP too large for Telegram (>48MB).")
            await query.message.reply_text(summary, parse_mode="HTML")
            return

        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return
        await msg.edit_text(f"✅ {success_count} files added, sending ZIP...")
        await query.message.reply_document(document=buf, filename=zip_name)
        await msg.delete()
        await query.message.reply_text(summary, parse_mode="HTML")
        link_cache.pop(sid, None)


# ── message full view (📖 Show Full Message) ─────────────────────────
async def _selection_message(update, context, query, data, user_id):
    """Inline button callback: message."""
    sid = data[1]
    info = link_cache.get(sid, {})
    if not link_cache_owned_by(info, user_id):
        await query.message.reply_text("⚠️ This message link belongs to another account.")
        return
    msg_id = info.get("message_id")

    if not msg_id:
        await query.message.reply_text("⚠️ Message ID not found.")
        return
    try:
        logging.info("📨 Opening message ID %s via direct URL...", msg_id)

        user_sess = get_user_session(user_id)
        try:
            page = await studip_api_get(f"/messages/{msg_id}", {"include": "sender"}, session=user_sess, user_id=user_id)
            msg = _api_message(page["data"], _api_names(page.get("included", [])))
            subject, sender_name, date_str, content = msg["title"], msg["sender"], msg["date"], msg["body"]
        except Exception as api_err:
            logging.warning("Message %s: API failed (%s), reading the HTML page", msg_id, api_err)
            msg_url = f"https://elearning.uni-oldenburg.de/dispatch.php/messages/read/{msg_id}/rec"
            async with await user_sess.get(msg_url) as resp:
                html = await resp.text()

            # lxml, not html.parser - see the comment in check_new_announcements_parallel
            # on the same inline-SVG-icon parse-tree corruption bug.
            soup = BeautifulSoup(html, "lxml")

            # Extract information
            subject_tag = soup.select_one("#ui-id-7, .message-subject, h1")
            subject = subject_tag.get_text(strip=True) if subject_tag else "(No subject)"

            # Try to find elements by labels
            sender_name = "Unknown"
            date_str = "-"
            for row in soup.select("tr"):
                th = row.select_one("th, td:first-child")
                td = row.select_one("td")
                if th and td:
                    label = th.get_text(strip=True).lower()
                    if "from" in label or "von" in label:
                        sender_name = td.get_text(strip=True)
                    elif "date" in label or "datum" in label:
                        date_str = td.get_text(strip=True)

            body_container = soup.select_one("div.formatted-content, .message-content, .formatted-content.ck-content")
            content = clean_html_text(body_container) if body_container else "No content found."

        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
            return

        # Send to Telegram
        header = (
            "✉️ <b>FULL MESSAGE</b>\n"
            "                  \n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📬 <b>Subject:</b> {_h(subject)}\n"
            f"👤 <b>From:</b> {_h(sender_name)}\n"
            f"📅 <b>Date:</b> {_h(date_str)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

        # escaped parts that each fit the limit (escaping grows '&' and '<'), no entity is cut
        chunks = escaped_chunks(content, 3500)
        if chunks:
            await query.message.reply_text(f"{header}{chunks[0]}", parse_mode="HTML")
            for part in chunks[1:]:
                if not is_user_allowed(user_id) or not db.private_context_is_current(user_id) or not link_cache_owned_by(info, user_id):
                    return
                await query.message.reply_text(part, parse_mode="HTML")
        else:
            await query.message.reply_text(f"{header}No message content found.", parse_mode="HTML")

        logging.info("✅ Message %s successfully opened directly and sent to Telegram.", msg_id)

    except Exception as e:
        logging.error("Message read failed: %s", e)
        await query.message.reply_text(f"⚠️ Could not load message content:\n{str(e)[:200]}")


# ── show last items ─────────────────────────────────────────────────
async def _selection_show_last(update, context, query, data, user_id):
    """Inline button callback: show_last."""
    action = data[1]
    if action == "messages":
        await show_last_messages(update, context)
    elif action == "announcements":
        await show_last_announcements(update, context)
    elif action == "files":
        await show_last_files(update, context)


# ── watcher control ─────────────────────────────────────────────────
async def _selection_start_watchers(update, context, query, data, user_id):
    """Inline button callback: start_watchers / stop_watchers."""
    await handle_status_buttons(update, context)


async def _selection_show_menu(update, context, query, data, user_id):
    """Inline button callback: back to the main keyboard (e.g. from an empty course list)."""
    await query.message.reply_text("🏠 Main menu", reply_markup=get_main_keyboard())


_SELECTION_HANDLERS = {
    "show_menu": _selection_show_menu,
    "course": _selection_course,
    "folder": _selection_folder,
    "file": _selection_file,
    "nav": _selection_nav,
    "message": _selection_message,
    "show_last": _selection_show_last,
    "start_watchers": _selection_start_watchers,
    "stop_watchers": _selection_start_watchers,
}


async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    handler = _SELECTION_HANDLERS.get(data[0])
    if handler is None:
        # ── unknown callback ────────────────────────────────────────────
        logging.warning("Unknown callback data: %s", query.data)
        await query.message.reply_text("⚠️ Unknown action.")
        return
    await handler(update, context, query, data, user_id)


# ── multi-step text input (task/fast-enroll/settings wizards) ──────────────────
# These wizards used to rely on ForceReply + matching update.message.reply_to_message
# text. ForceReply causes several Telegram clients to permanently hide the bottom
# custom keyboard (get_main_keyboard()) even after it's resent later, so instead
# each step is tracked in context.user_data["pending_step"] and plain text answers
# (no forced reply) are matched against it here.
PENDING_STEP_STATE = ("pending_step", "task_wizard", "fastenroll_wizard", "studip_reply_target")


async def cancel_pending_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the current multi-step flow, including text-only prompts."""
    data = user_data(context)
    keys = (*PENDING_STEP_STATE, "pending_voice", "pending_voice_clarify")
    had_pending = any(key in data for key in keys)
    for key in keys:
        data.pop(key, None)
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "✅ Cancelled." if had_pending else "ℹ️ There is nothing to cancel.",
            reply_markup=get_main_keyboard(),
        )


async def handle_pending_step(update: Update, context: ContextTypes.DEFAULT_TYPE, text_override: Optional[str] = None) -> bool:
    """If the user has an active multi-step wizard pending, consume this message
    as that step's answer and return True. Otherwise return False so the caller
    falls through to normal button/text handling.

    `text_override` lets a caller supply the answer text directly (e.g. a
    voice-message transcription) instead of reading it from update.message.text.
    """
    step = context.user_data.get("pending_step")
    if not step:
        return False

    text = text_override if text_override is not None else (update.message.text or "").strip()
    if text_override is None and text in MAIN_KEYBOARD_LABELS:
        # a menu button tapped mid-wizard leaves the wizard: its label is
        # never a task time, a place or (worst) a reply sent to a lecturer
        for key in PENDING_STEP_STATE:
            context.user_data.pop(key, None)
        return False

    if step in ("wardrobe_add_item", "wardrobe_set_reference"):
        # These two steps only consume a photo (handle_photo_message) - a
        # text reply here means the user typed instead of sending one.
        await update.effective_message.reply_text("📸 I'm waiting for a photo, not text - send one, or /status to cancel.")
        return True

    if step in ("fastenroll_date", "fastenroll_time", "fastenroll_link"):
        return await _handle_fastenroll_wizard_reply(update, context, step, text)

    if step in ("task_text", "task_time"):
        return await _handle_task_wizard_reply(update, context, step, text)

    if step == "user_name":
        context.user_data.pop("pending_step", None)
        name = text.strip()[:50]
        if not name:
            await update.effective_message.reply_text("🤷 That didn't look like a name - try again from /status.")
            return True
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is not None:
            save_user_name(user_id, name)
        await update.effective_message.reply_text(f"👤 Got it - I'll call you {name}.", reply_markup=get_main_keyboard())
        return True

    if step == "weather_location":
        query_text = text.strip()[:100]
        if not query_text:
            await update.effective_message.reply_text("🤷 That didn't look like a place name - try again from /status.")
            return True
        resolved = await geocode_location(query_text)
        if not resolved:
            await update.effective_message.reply_text(f"🤷 Couldn't find \"{query_text}\" - try a more specific city name, or check the spelling.")
            return True
        saved = save_weather_location(resolved["name"], resolved["country"], resolved["latitude"], resolved["longitude"],
                                      user_id=update.effective_user.id)
        if not saved:
            await update.effective_message.reply_text(
                "⚠️ I couldn't save that location. Please try again or use /status to start over."
            )
            return True
        context.user_data.pop("pending_step", None)
        await update.effective_message.reply_text(
            f"📍 Got it - checking weather for {resolved['name']}, {resolved['country']} from now on.",
            reply_markup=get_main_keyboard(),
        )
        return True

    if step == "studip_msg_reply":
        target = context.user_data.pop("studip_reply_target", None)
        context.user_data.pop("pending_step", None)
        reply_body = text.strip()
        if not reply_body or not target or not target.get("recipient_id"):
            await update.effective_message.reply_text("⚠️ Reply cancelled or empty.", reply_markup=get_main_keyboard())
            return True
        status_msg = await update.effective_message.reply_text("📤 Sending reply on Stud.IP...")
        try:
            await send_studip_message(
                recipient_id=target["recipient_id"],
                subject=target.get("subject") or "Re: Message",
                message_body=reply_body,
                user_id=update.effective_user.id,
            )
            await status_msg.edit_text(
                f"✅ <b>Reply sent on Stud.IP!</b>\n"
                f"👤 <b>To:</b> {html.escape(target.get('sender', 'Recipient'))}\n"
                f"📘 <b>Subject:</b> {html.escape(target.get('subject', 'Re:'))}",
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.error("Failed to send Stud.IP reply: %s", exc)
            await status_msg.edit_text(f"❌ Failed to send reply on Stud.IP:\n<code>{html.escape(str(exc)[:200])}</code>", parse_mode="HTML")
        return True

    if step == "admin_broadcast_text":
        context.user_data.pop("pending_step", None)
        if not is_user_admin(update.effective_user.id if update.effective_user else None):
            # admin rights may have been taken away since the prompt
            await update.effective_message.reply_text("⛔ Only administrators can broadcast.",
                                                      reply_markup=get_main_keyboard())
            return True
        msg_text = text.strip()
        if not msg_text or msg_text.lower() in ("/cancel", "cancel"):
            await update.effective_message.reply_text("📢 Broadcast cancelled.", reply_markup=get_main_keyboard())
            return True
        announcement = (
            "📢 <b>Announcement from Administrator</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(msg_text)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Sent to all registered students.</i>"
        )
        result = await broadcast(
            context.bot, announcement, parse_mode="HTML", disable_notification=False,
            sender_user_id=update.effective_user.id,
        )
        from unibot.security_events import log_security_event
        sender_current = is_user_allowed(update.effective_user.id) and is_user_admin(update.effective_user.id)
        outcome = "revoked" if result.get("sender_revoked") or not sender_current else (
            "failed" if result["failed"] else "success"
        )
        log_security_event("admin_broadcast", actor_id=update.effective_user.id, outcome=outcome)
        if result.get("sender_revoked") or not sender_current:
            return True
        sent_count = len(result["sent"])
        failed_count = len(result["failed"])
        if failed_count:
            confirmation = (f"⚠️ <b>Partial delivery:</b> {sent_count} student(s) received the announcement; "
                            f"delivery failed for {failed_count} student(s). Check the bot logs for details.")
        else:
            confirmation = f"✅ <b>Broadcast delivered to {sent_count} active student(s).</b>"
        await update.effective_message.reply_text(
            confirmation,
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return True

    # Unknown/stale step - clear it so the user isn't stuck.
    context.user_data.pop("pending_step", None)
    return False


# Keyboard buttons, current and older (a phone may still show an old keyboard):
# a text counts as a button only when it carries a button emoji, or is nothing
# but a button's own word, so a sentence mentioning "exam" is left alone.
_BUTTON_EMOJI = (
    ("files", ("📁", "📚", "⬇️")), ("calendar", ("📅",)), ("tasks", ("✅",)), ("menu", ("🍽️",)),
    ("progress", ("📊",)), ("exams", ("📝",)), ("check", ("🔄", "🔁")), ("status", ("⚙️", "ℹ️")),
)
_BUTTON_WORDS = {
    "files": "files", "schedule": "calendar", "calendar": "calendar", "tasks": "tasks", "task": "tasks",
    "mensa": "menu", "progress": "progress", "exams": "exams", "exam": "exams", "check": "check",
    "status": "status", "start": "start",
}


def button_target(text: str) -> Optional[str]:
    """Which keyboard button a text is ("files", "calendar", ...), or None."""
    text = (text or "").strip()
    if len(text) > 30:
        return None
    for target, emojis in _BUTTON_EMOJI:
        if any(e in text for e in emojis):
            return target
    return _BUTTON_WORDS.get(text.lower())


async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Support both keyboard and inline callback
    query = update.callback_query
    message = update.message
    data = None

    if not query and message and message.text and await handle_pending_step(update, context):
        return

    if query:
        await query.answer()
        data = query.data.split("|")
        chat_id = query.message.chat_id
        sender: Any = query.message  # a CallbackQuery itself has no reply_text
    else:
        text = (message.text or "").strip()
        # Study buttons have their own status messages, so they skip the
        # generic loading message below. Matched exactly, before the loose
        # keyword checks ("Study Plan" etc. must not fall through to them).
        study_buttons = {
            "📘 Study Plan": study_plan_command,
            "📊 Progress": progress_command,
            "🎯 Courses": track_courses_command,
            "🎯 Track Courses": track_courses_command,
            "🎯 Tracking": track_courses_command,
        }
        if text in study_buttons:
            await study_buttons[text](update, context)
            return
        target = button_target(text)
        if target is None:
            # an ordinary sentence ("status of my exam?") is not a button
            if is_user_allowed(update.effective_user.id):
                await message.reply_text("👇 Use the buttons below, or /status for everything else.",
                                         reply_markup=get_main_keyboard())
            return
        data = [target]
        chat_id = message.chat_id
        sender = message

    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await sender.reply_text("Not authorized to use this bot.")
        return

    try:
        # Temporary loading message
        status_msg = await sender.reply_text("⏳ Please wait...")
        await _show_typing(sender)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Please wait...")

        try:
            if data[0] == "menu":
                await menu_command(update, context)
            elif data[0] == "calendar":
                logging.info("📅 Fetching today's schedule...")
                today = berlin_now().date()
                week_start = today - timedelta(days=today.weekday())
                user_sess = get_user_session(user_id)
                session = user_sess or state.global_session
                events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id)
                await send_daily_calendar(sender, events, today, week_start)
            elif data[0] == "tasks":
                await send_tasks_menu(sender)
            elif data[0] == "progress":
                await progress_command(update, context)
            elif data[0] == "exams":
                await exams_command(update, context)
            elif data[0] == "start":
                await start(update, context)
            elif data[0] == "files":
                await handle_files_browse(sender)
            elif data[0] == "check":
                await check_command(update, context)
            elif data[0] == "status":
                await status_command(update, context)
            else:
                await sender.reply_text("❓ Unknown command.")
        finally:
            await _stop_loading_animation(stop_evt, anim_task)

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except TelegramError:
            pass
    except Exception as e:
        logging.error("Button handler error: %s", e)
        try:
            await sender.reply_text(f"⚠️ An error occurred:\n{e}")
        except TelegramError:
            pass


# ── /start command ────────────────────────────────────────────────────────────
# ── /start command ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return

    # Send keyboard IMMEDIATELY - not conditional on anything
    await update.message.reply_text(
        "🎓 <b>Uni Oldenburg Assistant</b>\n\n"
        "Welcome! Your personal Stud.IP companion is ready.\n\n"
        "<b>Dashboard Navigation:</b>\n"
        "• 📁 <b>Files:</b> Browse course documents & materials\n"
        "• 📅 <b>Schedule:</b> Today's lecture timetable & calendar\n"
        "• ✅ <b>Tasks:</b> Manage personal tasks & deadlines\n"
        "• 🍽️ <b>Mensa:</b> Today's cafeteria food menu\n"
        "• 📘 <b>Study Plan:</b> Weekly study schedule & modules\n"
        "• 📊 <b>Progress:</b> Track study progress & completion\n"
        "• 📝 <b>Exams:</b> Open exam registrations & dates\n"
        "• 🔄 <b>Check:</b> Instantly scan for new files & grades\n"
        "• ⚙️ <b>Status:</b> Documents, tuition, WhatsApp & settings\n",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await _offer_setup_if_signed_in(context.bot, user_id)


async def _offer_setup_if_signed_in(bot, user_id: int) -> None:
    """A signed-in student who never did the first-run setup gets it here
    (one who isn't signed in gets it right after /login)."""
    if state.on_signed_in is None:
        return
    session = get_user_session(user_id)
    if session is None or not has_stored_login(session):
        return
    try:
        await state.on_signed_in(bot, user_id, session)
    except Exception:
        logging.warning("Setup offer after /start failed for %s", user_id, exc_info=True)


async def send_course_picker(message, context: ContextTypes.DEFAULT_TYPE):
    """List the student's currently-enrolled Stud.IP courses so they can pick one
    for the combined tasks+exam-info view."""
    uid = None
    if hasattr(message, "from_user") and message.from_user:
        uid = message.from_user.id
    elif hasattr(message, "effective_user") and message.effective_user:
        uid = message.effective_user.id
    try:
        courses = await list_courses(user_id=uid)
    except Exception as e:
        logging.error("send_course_picker: failed to fetch courses: %s", e)
        await message.reply_text("❌ Could not load your courses right now.")
        return

    if not courses:
        await message.reply_text("ℹ️ No enrolled courses found.")
        return

    user_data(context)["course_view_options"] = courses
    keyboard = [
        [InlineKeyboardButton(f"📘 {name[:40]}", callback_data=f"course_view|{i}")]
        for i, (name, cid) in enumerate(courses)
    ]
    await message.reply_text("📘 Pick a course:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_course_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show one course's tasks and upcoming exam info together."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return

    options = context.user_data.get("course_view_options", [])
    raw_idx = query.data.split("|", 1)[1]
    idx = int(raw_idx) if raw_idx.isdigit() else -1
    if not (0 <= idx < len(options)):
        await query.message.reply_text("⚠️ This course list has expired - tap 📘 By Course again.")
        return
    course_name, course_cid = options[idx]

    status_msg = await query.message.reply_text(f"📘 Loading {course_name}...", disable_notification=True)
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, f"Loading {course_name}...")

    lines = [f"📘 <b>{_h(course_name)}</b>", "━━━━━━━━━━━━━━━━━"]

    try:
        course_tasks = [t for t in user_tasks(user_id) if t.get("course") == course_name]
        if course_tasks:
            lines.append("📝 <b>Tasks</b>")
            for t in sorted(course_tasks, key=lambda t: t.get("due_time") or ""):
                due = task_due(t) if t.get("due_time") else None
                if due is not None:
                    lines.append(f"⏰ {due.strftime('%d.%m.%Y %H:%M')} - {_h(t['text'])}")
                else:
                    lines.append(f"• {_h(t['text'])}")
            lines.append("━━━━━━━━━━━━━━━━━")
        else:
            lines.append("📝 <b>Tasks:</b> none")
            lines.append("━━━━━━━━━━━━━━━━━")

        session = await login_studip()  # the acting user's own session
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
            return
        module_codes = await get_my_module_codes(session, [(course_name, course_cid)])
        if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
            return
        exams = await get_all_exam_dates(session)
        matched = filter_exams_by_module_codes(exams, module_codes)
        today = berlin_now().date()
        upcoming = [e for e in matched if datetime.fromisoformat(e["exam_date"]).date() >= today]
        upcoming.sort(key=lambda e: (e["exam_date"], e["title"]))
    except Exception as e:
        logging.error("handle_course_view: exam fetch failed: %s", e)
        upcoming = None
    finally:
        await _stop_loading_animation(stop_evt, anim_task)
        await _delete_status_message(status_msg)

    if not is_user_allowed(user_id) or not db.private_context_is_current(user_id):
        return

    if upcoming is None:
        lines.append("📅 <b>Exams:</b> could not load right now")
    elif not upcoming:
        lines.append("📅 <b>Exams:</b> none upcoming")
    else:
        lines.append("📅 <b>Exams</b>")
        for exam in upcoming:
            title = exam["title"]
            if exam.get("group_label"):
                title += f" ({exam['group_label']})"
            lines.append(f"{_h(title)} - {_h(exam['exam_date_display'])}")

    await query.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_course_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The module-name header rows in the grouped course list are buttons only
    so they render as visual separators; tapping one does nothing."""
    await update.callback_query.answer()


async def handle_set_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle set_user_name callback - notify that display name is automatically fetched from Stud.IP."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        profile = await get_studip_user_profile()
        name = profile.get("formatted_name") or profile.get("given_name")
    except Exception:
        logging.debug("Stud.IP profile lookup failed", exc_info=True)
        name = None
    if not name:
        name = get_user_name(user_id) or "Stud.IP"
    await query.message.reply_text(
        f"👤 Your name is automatically synchronized from Stud.IP: <b>{_h(name)}</b>",
        parse_mode="HTML",
    )


CHANGE_SEM_CONFIG = {
    "files": ("files_sem", "⬇️ Pick a semester to browse its courses' files:"),
    "enroll": ("course_sem", "📚 Pick a semester to see courses currently open for enrolment:"),
    "deenroll": ("deenroll_sem", "🚪 Pick a semester to see its enrolled courses:"),
}


async def handle_change_semester(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle change_sem|<files|enroll|deenroll> callback - the "📆 Change
    Semester" button at the bottom of the Files/Enroll/Sign Out course lists.
    Always shows the full semester picker for that list (ignoring any default
    semester set in /status), so the student can view a different semester
    just this once without having to clear their default."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    kind = query.data.split("|", 1)[1]
    callback_prefix, prompt = CHANGE_SEM_CONFIG[kind]
    await _show_semester_picker(query, callback_prefix, prompt)


# ── Fast Enroll guided wizard (date → time → course link) ──────────────────────
FASTENROLL_ASK_DATE = "Please type the enrollment date (DD.MM.YYYY), or 'today' / 'tomorrow':"
