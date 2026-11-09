"""Watchers for announcements, files, forum posts and messages, plus 'show last' views.

Layers: unibot.monitoring_fetch reads Stud.IP, unibot.monitoring_diff finds
changed files, unibot.monitoring_alerts builds and sends the alerts; this
module keeps the caches, runs the checks and serves the commands.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Any, Optional

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import studip_oauth
from studip_utils import TZ_BERLIN, berlin_now
from unibot import course_registry, db, state
from unibot.ai import transcribe_voice_message
from unibot.auth import (
    ACTIVE,
    act_as_bot,
    acting_as,
    current_user_id,
    get_user_session,
    has_stored_login,
    lifecycle_state,
    is_user_admin,
    is_user_allowed,
    login_studip,
    signed_in_accounts,
)
from unibot.config import PRIMARY_ADMIN_ID
from unibot.formatting import (
    _course_abbrev,
    _short_id,
    clean_for_html,
    clean_html_text,
    parse_date_safe,
)
from unibot.monitoring_alerts import (  # noqa: F401 - the notification layer, re-exported for callers
    _file_alert,
    _format_exam_dates_message,
    _forum_alert_markup,
    _forum_alert_text,
    _send_file_alerts,
    announcement_task_cache,
    announcement_title_text,
    course_members,
    forum_title_text,
    task_suggestion_cache,
)
from unibot.monitoring_diff import (  # noqa: F401 - change detection, re-exported for callers
    _diff_course_files,
    _file_events,
    _file_was_updated,
    _snapshot_course_files,
)
from unibot.monitoring_fetch import (  # noqa: F401 - the fetch layer, re-exported for callers
    JSONAPI_HEADERS,
    MESSAGE_CHECK_LIMIT,
    _course_announcements_api,
    _fetch_course_forum,
    _fetch_course_forum_strict,
    _fetch_messages_api,
    _fetch_messages_html,
    _forum_author_name,
    _forum_thread_entry,
    _friendly_forum_date,
    _html_to_text,
    _parse_ann_date,
    _post_author_id,
    fetch_message_body,
)
from fast_enroll import get_semester_options
from unibot.preferences import get_pref
from unibot.storage import (
    link_cache,
    load_default_semester,
    load_files_cache,
    personal_record,
    save_files_cache,
    save_personal_record,
    single_flight,
    student_courses,
)
from unibot.studip_api import (
    BASE_URL,
    STUDIP_API_URL,
    StudipApiError,
    _api_names,
    _api_rel_id,
    _ensure_watched_courses_map,
    list_courses,
    _list_courses_api,
    _list_files_api,
    _list_files_html,
    studip_api_get,
)
from unibot.telegram_ui import (
    markup_for,
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
)

global_message_watcher = None
global_announcement_watcher = None
check_in_progress: set[int] = set()  # chat_id set to prevent concurrent checks

_OLDEST = datetime.fromtimestamp(0, TZ_BERLIN)  # sorts files without a date first (no overflow when compared)


MESSAGES_RECORD = "studip_messages"  # the student's recent inbox, in their encrypted record


def _message_owner(user_id: Optional[int] = None) -> Optional[int]:
    """Whose inbox: the given or acting student, else the watcher session's."""
    if user_id is None:
        user_id = current_user_id()
    if user_id is None:
        user_id = getattr(state.global_session, "user_id", None) or PRIMARY_ADMIN_ID
    return user_id


def _legacy_message_keys(user_id: int) -> list[str]:
    """Plain documents the inbox used to be kept in."""
    keys = [f"messages_cache_{user_id}.json"]
    watcher_uid = getattr(state.global_session, "user_id", None) or PRIMARY_ADMIN_ID
    if user_id == watcher_uid:
        keys.append("messages_cache.json")
    return keys


def stored_messages(user_id: Optional[int] = None) -> Optional[list]:
    """The student's remembered inbox (None before their first check). An
    old plain document is moved into the encrypted record and deleted."""
    uid = _message_owner(user_id)
    if uid is None:
        return None
    messages = personal_record(uid, MESSAGES_RECORD)
    if messages is not None:
        return messages
    for key in _legacy_message_keys(uid):
        try:
            if db.exists(key):
                messages = db.load(key) or []
                save_personal_record(uid, MESSAGES_RECORD, messages)
                db.delete(key)
                return messages
        except Exception as e:
            logging.warning("Could not move an old message cache: %s", e)
    return None


def save_messages(user_id: Optional[int], messages: list) -> None:
    uid = _message_owner(user_id)
    if uid is not None:
        save_personal_record(uid, MESSAGES_RECORD, messages)


def load_message_cache(user_id: Optional[int] = None) -> list[dict]:
    """The acting (or given) student's remembered inbox."""
    try:
        return stored_messages(user_id) or []
    except Exception as e:
        logging.warning("load_message_cache failed: %s", e)
        return []


def get_show_last_keyboard():
    """Show Last keyboard with menu button"""
    kb = [
        [InlineKeyboardButton("📢 Show Last 5 Announcements", callback_data="show_last_announcements")],
        [InlineKeyboardButton("📨 Show Last 5 Messages", callback_data="show_last_messages")],
        [InlineKeyboardButton("📁 Show Last 5 Files", callback_data="show_last_files")],
        [InlineKeyboardButton("💬 Show Last 5 Forum Posts", callback_data="show_last_forum_posts")],
        [InlineKeyboardButton("⚡ Activity Stream", callback_data="activity_stream")],
    ]
    return InlineKeyboardMarkup(kb)


async def show_last_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 files with labeled header."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer()
    chat_id = query.message.chat_id
    FILES_CACHE_PATH = "files_cache.json"

    if not db.exists(FILES_CACHE_PATH):
        await query.edit_message_text("⚠️ No file history found.")
        return

    try:
        cache = db.load(FILES_CACHE_PATH)
    except Exception as e:
        logging.error("Failed to load files cache: %s", e)
        await query.edit_message_text("⚠️ Error reading file history.")
        return

    allowed = set(student_courses(query.from_user.id))  # the shared cache holds every student's courses
    all_files = []
    for cid, files in cache.items():
        if not isinstance(files, dict) or cid not in allowed:
            continue
        for name, info in files.items():
            if not isinstance(info, dict):
                continue
            course = info.get("course") or state.courses_map.get(cid) or cid
            ts = info.get("modified_ts")
            dt = datetime.fromtimestamp(ts, TZ_BERLIN) if isinstance(ts, (int, float)) else _OLDEST
            all_files.append({
                "cid": cid,
                "course": course,
                "name": name,
                "size": info.get("size", "-"),
                "modified": info.get("modified", "-"),
                "dt": dt
            })

    if not all_files:
        await query.edit_message_text("⚠️ No files in cache.")
        return

    all_files.sort(key=lambda x: (x["dt"], x["course"], x["name"]))
    last5 = all_files[-5:]

    await query.edit_message_text("📁 <b>Last 5 Files</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for f in last5:
        sid = _short_id()
        link_cache[sid] = {"cid": f["cid"], "name": f["name"], "course": f["course"], "ts": time.time()}

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>📁 File</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>📚 Course:</b> {html.escape(f['course'])}\n"
            f"<b>📄 Name:</b> {html.escape(f['name'])}\n"
            f"<b>📅 Date:</b> {html.escape(f['modified'])}\n"
            f"<b>💾 Size:</b> {html.escape(str(f['size']))}\n"
            "━━━━━━━━━━━━━━━━━"
        )

        btn_rows = [[InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")]]
        if os.path.splitext(f["name"] or "")[1].lower() in (".pdf", ".pptx", ".html"):
            btn_rows.append([InlineKeyboardButton("🎧 1-Min Audio Summary", callback_data=f"file_audio_sum|{sid}")])
        markup = InlineKeyboardMarkup(btn_rows)

        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


# ── course watch plan ─────────────────────────────────────────────────────────
# Every course of every signed-in user is watched, each fetched once with the
# session of one of its participants, and alerts go to its participants only.

COURSE_LIST_REFRESH_SECONDS = 6 * 3600
_course_list_fetched: dict[int, float] = {}
# students' previous-semester courses (only for those who opted in), {uid: {cid: name}}
_prev_semester_courses: dict[int, dict] = {}
_prev_semester_fetched: dict[int, float] = {}


async def _previous_semester_courses(uid: int, sess) -> dict:
    """{cid: name} of the student's courses in the semester before their
    current one, fetched with their own session through the JSON:API (no
    my_courses filter switching) and cached for COURSE_LIST_REFRESH_SECONDS."""
    if uid in _prev_semester_courses and \
            time.time() - _prev_semester_fetched.get(uid, 0.0) < COURSE_LIST_REFRESH_SECONDS:
        return _prev_semester_courses[uid]
    options = await get_semester_options(sess)
    anchor = load_default_semester().get("id") or (options[0][0] if options else None)
    index = next((i for i, (sid, _label) in enumerate(options) if sid == anchor), None)
    courses: dict = {}
    if index is not None and index + 1 < len(options):
        with acting_as(uid):
            listed = await _list_courses_api(options[index + 1][0], session=sess, user_id=uid)
        courses = {cid: name for name, cid in listed}
    _prev_semester_courses[uid] = courses
    _prev_semester_fetched[uid] = time.time()
    return courses


async def watched_courses() -> list:
    """[(course name, cid, session)], one entry per course across all users.

    The session is the course's designated scanner: members whose sessions
    are signed in take turns (course_registry.pick_scanner), and a failing one
    hands over to the next member during the scan. The watcher session's map
    also holds the previous semester's courses when that is enabled. Each
    user's own course list is refreshed every COURSE_LIST_REFRESH_SECONDS.
    state.course_members records who takes what, and the registry tables
    (global_courses, user_courses) are kept in step with it."""
    titles: dict = {}
    members: dict = {}
    candidates: dict = {}
    known_uids: set = set()
    watcher = state.global_session
    watcher_uid = getattr(watcher, "user_id", None) if watcher is not None else None
    if watcher is not None and has_stored_login(watcher) and lifecycle_state(watcher_uid) == ACTIVE:
        await _ensure_watched_courses_map(watcher)
        for cid, name in state.courses_map.items():
            titles.setdefault(cid, name)
            candidates.setdefault(cid, []).append((watcher_uid, watcher))
            if watcher_uid:
                members.setdefault(cid, set()).add(watcher_uid)
    for account in signed_in_accounts():
        uid, sess = account.user_id, account.session
        if time.time() - _course_list_fetched.get(uid, 0.0) > COURSE_LIST_REFRESH_SECONDS \
                or uid not in state.user_courses_map:
            try:
                with acting_as(uid):
                    await list_courses(session=sess, user_id=uid)
                _course_list_fetched[uid] = time.time()
            except Exception as e:
                logging.warning("Course list for user %s unavailable: %s", uid, e)
        if uid in state.user_courses_map:
            known_uids.add(uid)
        previous: dict = {}
        if uid != watcher_uid and get_pref(uid, "watch_previous_semester"):
            try:
                previous = await _previous_semester_courses(uid, sess)
            except Exception as e:
                logging.warning("Previous semester courses for user %s unavailable: %s", uid, e)
        for cid, name in {**previous, **(state.user_courses_map.get(uid) or {})}.items():
            titles.setdefault(cid, name)
            candidates.setdefault(cid, []).append((uid, sess))
            members.setdefault(cid, set()).add(uid)
    state.course_members = members
    try:
        course_registry.sync_registry(titles, members, known_uids)
    except Exception as e:
        logging.warning("Course registry sync failed: %s", e)
    plan = []
    for cid, name in titles.items():
        course_registry.set_candidates(cid, candidates.get(cid) or [])
        scanner = course_registry.pick_scanner(cid)
        if scanner is not None:
            plan.append((name, cid, scanner))
    return plan


async def check_new_announcements_parallel(bot, chat_id, silent: bool = False, courses=None):
    """Parallel announcement checking for new items; `courses` is a
    watched_courses() plan, the watcher session's courses otherwise."""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📢 Checking announcements...", disable_notification=True)

    try:
        CACHE_PATH = "announcement_cache.json"
        cache: dict[str, list] = {"seen": [], "history": []}
        if db.exists(CACHE_PATH):
            try:
                cache = db.load(CACHE_PATH)
            except json.JSONDecodeError as e:
                corrupt_path = CACHE_PATH + ".corrupt"
                try:
                    os.replace(CACHE_PATH, corrupt_path)
                except OSError:
                    pass
                logging.error("%s was corrupted (%s); moved to %s and starting fresh.", CACHE_PATH, e, corrupt_path)
                cache = {"seen": [], "history": []}
        seen = set(cache.get("seen", []))

        if courses is None:
            await _ensure_watched_courses_map(session)
            courses = [(name, cid, session) for cid, name in state.courses_map.items()]

        all_found = []
        semaphore = asyncio.Semaphore(3)

        async def check_single_course_announcements(course_name, cid, session):
            async with semaphore:
                try:
                    found, used = await course_registry.scan_with_failover(
                        cid, session, lambda s: _course_announcements_api(cid, course_name, session=s))
                    course_registry.record_scan(cid, used)
                    return course_name, cid, found
                except Exception as e:
                    logging.warning("Announcements API failed for %s (%s), falling back to the overview page", course_name, e)
                try:
                    url = f"{BASE_URL}/dispatch.php/course/overview?cid={cid}"
                    async with await session.get(url) as r:
                         html_text = await r.text()

                    if "loginform" in html_text:
                        logging.warning("🔐 Session expired while accessing %s.", course_name)
                        await session.login()
                        return course_name, cid, []

                    # lxml, not html.parser: this page embeds inline SVG icons with
                    # XML-style self-closing tags (e.g. <path .../>), which
                    # html.parser doesn't recognize as self-closing (it only knows
                    # a fixed list of HTML void elements) - it leaves them "open",
                    # corrupting the parse tree for everything after, including the
                    # announcement body's <br> tags further down the page. This
                    # silently truncated announcement text to just its first line.
                    soup = BeautifulSoup(html_text, "lxml")
                    ann_container = None
                    for art in soup.select("article.studip"):
                        h = art.select_one("header h1")
                        if h:
                            h_text = h.get_text(strip=True)
                            if "Announcements" in h_text or "Ankündigungen" in h_text:
                                ann_container = art
                                break

                    if not ann_container:
                        return course_name, cid, []

                    course_announcements = []
                    for art in ann_container.select("article.studip.toggle"):
                        ann_id = art.get("id") or ""
                        # Adjust selectors for direct HTML
                        title_tag = art.select_one("header h1 a") or art.select_one("header h1")
                        sender_tag = art.select_one(".news_user")
                        date_tag = art.select_one(".news_date")
                        body_tag = art.select_one(".formatted-content.ck-content")

                        title = title_tag.get_text(strip=True) if title_tag else "(No subject)"
                        sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
                        date_s = date_tag.get_text(strip=True) if date_tag else ""
                        body = clean_html_text(body_tag) if body_tag else "(No content)"

                        key = f"{cid}:{ann_id}" if ann_id else f"{cid}:{title}:{date_s}"
                        dt = _parse_ann_date(date_s)

                        announcement = {
                            "key": key, "cid": cid, "course": course_name, "subject": title,
                            "sender": sender, "date": date_s, "dt": dt.isoformat() if dt else None, "body": body,
                        }
                        course_announcements.append(announcement)
                    return course_name, cid, course_announcements
                except Exception as e:
                    logging.warning("Announcement parse error in %s: %s", course_name, e)
                    return course_name, cid, []

        tasks = [check_single_course_announcements(name, cid, sess) for name, cid, sess in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, tuple) and len(result) == 3:
                all_found.extend(result[2])

        if all_found:
            all_found.sort(key=lambda x: (x["dt"] or "", x["date"]))
        new_items = [a for a in all_found if a["key"] not in seen]

        if new_items:
            for ann in new_items:
                text = (
                    "🔥 <b>NEW ANNOUNCEMENT</b> 🔥\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"🏫 <b>Course:</b> {ann['course']}\n"
                    f"👤 <b>From:</b> {ann['sender']}\n"
                    f"📘 <b>Subject:</b> {ann['subject']}\n"
                    f"🕒 <b>Date:</b> {ann['date']}\n\n"
                    f"{ann['body']}\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                sid = _short_id()
                announcement_task_cache[sid] = {"course": ann['course'], "subject": ann['subject'], "body": ann['body']}
                buttons = [
                    [InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")],
                    [InlineKeyboardButton("📝 Add as Task", callback_data=f"ann_to_task|{sid}")],
                ]
                markup = InlineKeyboardMarkup(buttons)
                await course_registry.fan_out(bot, ann.get("cid"), "announcements", text[:4000],
                                              announcement_title_text(ann), reply_markup=markup,
                                              members=course_members(ann.get("cid")))
        elif not silent:
            await bot.send_message(chat_id=chat_id, text="☑️ No new announcements found.", disable_notification=True)

        seen.update(a["key"] for a in all_found)
        cache["seen"] = list(seen)
        merged = cache.get("history", [])
        existing_keys = {h.get("key") for h in merged}
        for a in all_found:
            if a["key"] not in existing_keys:
                merged.append(a)
        cache["history"] = merged[-100:]

        db.save(CACHE_PATH, cache)
    except Exception as e:
        logging.error("check_new_announcements_parallel fatal: %s", e)


async def show_last_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 announcements (with labeled header)."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer()
    chat_id = query.message.chat_id
    CACHE_PATH = "announcement_cache.json"

    if not db.exists(CACHE_PATH):
        await query.edit_message_text("⚠️ No announcement history available.")
        return

    try:
        cache = db.load(CACHE_PATH)
    except Exception as e:
        logging.error("Failed to load announcement cache: %s", e)
        await query.edit_message_text("⚠️ Error reading announcement history.")
        return

    history = cache.get("history", [])
    allowed = set(student_courses(query.from_user.id))  # the shared cache holds every student's courses
    if isinstance(history, list):
        history = [a for a in history
                   if isinstance(a, dict) and (a.get("cid") or str(a.get("key", "")).split(":", 1)[0]) in allowed]
    if not isinstance(history, list) or not history:
        await query.edit_message_text("⚠️ No announcements found in history.")
        return

    history.sort(key=lambda x: x.get("dt", ""))
    last5 = history[-5:]

    await query.edit_message_text("📢 <b>Last 5 Announcements</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for ann in last5:
        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>📢 Announcement</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>🏫 Course:</b> {html.escape(ann.get('course', 'Unknown'))}\n"
            f"<b>📘 Subject:</b> {html.escape(ann.get('subject', '(No subject)'))}\n"
            f"<b>👤 From:</b> {html.escape(ann.get('sender', 'Unknown'))}\n"
            f"<b>🕒 Date:</b> {html.escape(ann.get('date', '-'))}\n\n"
            f"{html.escape(ann.get('body', ''))}\n"
            "━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


async def show_last_forum_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 forum posts (with labeled header)."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer()
    chat_id = query.message.chat_id
    CACHE_PATH = FORUM_CACHE_PATH

    if not db.exists(CACHE_PATH):
        await query.edit_message_text("⚠️ No forum history available.")
        return

    try:
        cache = db.load(CACHE_PATH)
    except Exception:
        logging.warning("Forum history unreadable", exc_info=True)
        await query.edit_message_text("⚠️ Error reading forum history.")
        return

    # Extract all posts from cache
    allowed = set(student_courses(query.from_user.id))  # the shared cache holds every student's courses
    all_posts = []
    for key, post in cache.items():
        if isinstance(post, dict) and (post.get("cid") or str(key).split(":", 1)[0]) in allowed:
            all_posts.append(post)

    if not all_posts:
        await query.edit_message_text("⚠️ No forum posts found in history.")
        return

    # Sort by date (text sort is not ideal but sufficient for now, or use 'key' if it has timestamp)
    # The key format is cid:thread_id:post_id_or_date.
    # Let's try to trust the order they were added or just reverse check
    # We don't have a reliable timestamp in the dict unless we parsed it.
    # But for "Show Last", usually showing the most recently added to cache is good.
    # Python dicts preserve insertion order in recent versions.
    # So taking the last 5 items might work if we append new ones.
    # But let's just show them.

    last5 = list(all_posts)[-5:]
    last5.reverse()  # Newest first

    await query.edit_message_text("💬 <b>Last 5 Forum Posts</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for post in last5:
        # Get post data with fallbacks
        course = post.get('course', 'Unknown Course')
        thread = post.get('thread', 'Unknown Thread')
        author = post.get('author', 'Unknown')
        date = post.get('date') or post.get('timestamp', '-')
        body = post.get('body', '')

        # No blind truncation here because body contains HTML formatting.
        # It's better to manage length dynamically within the scraper.

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>💬 Forum Post</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"🏫 <b>Course:</b> {html.escape(course)}\n"
            f"📂 <b>Thread:</b> {html.escape(thread)}\n"
            f"👤 <b>By:</b> {html.escape(author)}\n"
            f"🕒 <b>Date:</b> {html.escape(date)}\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"{body}\n"
            "━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


async def check_new_files(bot, chat_id, silent: bool = False):
    """Wrapper for parallel file checking"""
    return await check_new_files_parallel(bot, chat_id, silent)


async def _scan_course_files(course_name: str, cid: str, cache: dict, session=None) -> tuple[list, bool]:
    """(new/updated file events, cache changed) for one course.

    The cache always gets the fresh listing, even without a notification, so
    later comparisons use the latest timestamps.
    """
    try:
        try:
            files, used = await course_registry.scan_with_failover(
                cid, session, lambda s: _list_files_api(cid, None, session=s))
        except Exception as e:
            logging.warning("File API failed for %s with every member (%s), falling back to the HTML page",
                            course_name, e)
            files, used = await _list_files_html(cid, None, session=session), session
        if not files:
            return [], False
        current = _snapshot_course_files(files)
        course_registry.record_scan(cid, used, file_hash=course_registry.content_hash(current))
        old = cache.get(cid, {})
        events = _file_events(course_name, cid, *_diff_course_files(current, old))
        if current == old:
            return events, False
        cache[cid] = {name: {**info, "course": course_name, "modified_ts": info.get("modified_ts")}
                      for name, info in current.items()}
        return events, True
    except Exception as e:
        logging.warning("Error while checking files for %s: %s", course_name, e)
        return [], False


@single_flight
async def check_new_files_parallel(bot, chat_id, silent: bool = False, courses=None):
    """Parallel file checking; `courses` is a watched_courses() plan."""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📁 Checking files...", disable_notification=True)

    try:
        cache = load_files_cache()
        if courses is None:
            await _ensure_watched_courses_map(session)
            courses = [(name, cid, session) for cid, name in state.courses_map.items()]
        if not courses:
            return

        semaphore = asyncio.Semaphore(1)

        async def scan(course_name, cid, sess):
            async with semaphore:
                res = await _scan_course_files(course_name, cid, cache, session=sess)
                await asyncio.sleep(0.5)  # Pace queries politely
                return res

        results = await asyncio.gather(*(scan(name, cid, sess) for name, cid, sess in courses),
                                       return_exceptions=True)
        all_events = []
        updated = False
        for result in results:
            if isinstance(result, BaseException):
                logging.error("Course check failed: %s", result)
                continue
            events, changed = result
            all_events.extend(events)
            updated = updated or changed

        try:
            if all_events:
                await _send_file_alerts(bot, all_events)
            else:
                if not silent:
                    await bot.send_message(chat_id=chat_id, text="☑️ No new or updated files found.", disable_notification=True)
                logging.info("📁 No new or updated files found.")
        finally:
            # Ensure cache is saved even if something fails during notification
            if updated:
                save_files_cache(cache)

    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Error during file check:\n{e}")
        logging.error("check_new_files_parallel fatal: %s", e)


# The inbox is one small API request, so it's polled on its own short loop
# (see message_poll_loop) instead of once per multi-minute watcher cycle.
MESSAGE_POLL_SECONDS = 60


# Other users' inboxes are polled less often: every poll is a request from
# the same server, and the admin's 60 s cadence times N users adds up.
MESSAGE_POLL_USER_SECONDS = 5 * 60


async def message_poll_loop(app) -> None:
    """Checks the admin inbox every MESSAGE_POLL_SECONDS (so a new Stud.IP
    message reaches Telegram within about a minute) and every other signed-in
    user's inbox every MESSAGE_POLL_USER_SECONDS, each with their own session
    and sent only to them. Skips while the watchers are paused."""
    import random

    act_as_bot()
    last_user_poll: dict[int, float] = {}
    while True:
        await asyncio.sleep(MESSAGE_POLL_SECONDS)
        if state.global_watcher_paused:
            continue
        # the fast poll reads the watcher session's inbox, so its messages go
        # to that session's own student (the primary admin before it has one)
        admin_id = getattr(state.global_session, "user_id", None) or PRIMARY_ADMIN_ID
        with acting_as(None):
            if state.global_session is not None and admin_id is not None and has_stored_login(state.global_session) \
                    and lifecycle_state(admin_id) == ACTIVE:
                try:
                    await check_new_messages(app.bot, admin_id, silent=True)
                except Exception as e:
                    logging.error("❌ Message poll failed: %s", e)

        now = time.time()
        for uid, sess in list(state.user_sessions.items()):
            if sess is state.global_session or uid == admin_id:
                continue
            if now - last_user_poll.get(uid, 0.0) < MESSAGE_POLL_USER_SECONDS:
                continue
            if time.time() - getattr(sess, "last_interactive_activity", 0.0) < 180 or not has_stored_login(sess):
                continue
            last_user_poll[uid] = now
            await asyncio.sleep(random.uniform(1.0, 4.0))  # spread users out
            with acting_as(uid):
                try:
                    await check_new_messages(app.bot, uid, silent=True)
                except Exception as e:
                    logging.warning("Message poll failed for user %s: %s", uid, e)


_message_check_lock = asyncio.Lock()


async def check_new_messages(bot, chat_id, silent: bool = False):
    """Fetch Stud.IP messages fully (content included) and send in order
    (newest last). Serialized, since the fast poll loop and the manual
    check can overlap and both rewrite the cache."""
    async with _message_check_lock:
        return await _check_new_messages(bot, chat_id, silent)


async def _check_new_messages(bot, chat_id, silent: bool = False):
    # the acting user's own inbox (the admin's outside of any user context)
    session = get_user_session()
    owner = _message_owner()
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📨 Checking messages...", disable_notification=True)

    try:
        try:
            all_messages = await _fetch_messages_api(session=session)
        except Exception as e:
            logging.warning("check_new_messages: API failed (%s), falling back to the overview page", e)
            all_messages = await _fetch_messages_html(session)

        old_messages = stored_messages(owner)
        if old_messages is None:
            # first check for this user: remember the inbox as it is instead
            # of sending every old message as "new"
            save_messages(owner, all_messages[:100])
            if not silent:
                await bot.send_message(chat_id=chat_id, text="☑️ No new messages found.", disable_notification=True)
            return False

        old_ids = {m["id"] for m in old_messages}
        new_messages = [m for m in all_messages if m["id"] not in old_ids]

        if not new_messages:
            if not silent:
                await bot.send_message(chat_id=chat_id, text="☑️ No new messages found.", disable_notification=True)
            return False

        # API messages already carry their body; scraped rows don't.
        for msg in new_messages:
            if "body" not in msg:
                msg["body"] = await fetch_message_body(session, msg["url"])

        # Update cache with new messages prepended or appended
        # Best to just store all recent ones
        all_messages_combined = (new_messages + old_messages)[:100]
        save_messages(owner, all_messages_combined)

        new_messages_sorted = sorted(new_messages, key=lambda m: parse_date_safe(m.get("date", "")))
        for msg in new_messages_sorted:
            body_text = msg.get("body", "📭 Content error").strip()
            text = (
                "🔥 📩 <b>NEW MESSAGE</b> 🔥\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>From:</b> {msg.get('sender', 'Unknown')}\n"
                f"📘 <b>Subject:</b> {msg.get('title', '(No subject)')}\n"
                f"🕒 <b>Date:</b> {msg.get('date', '-')}\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"{body_text}\n"
                "━━━━━━━━━━━━━━━━━"
            )
            # Auto-generated "Enrolment in course" notifications aren't the kind
            # of thing anyone forwards to the WhatsApp group, so skip the button
            # for those rather than offering an action that's never used.
            subject = (msg.get("title") or "").strip()
            sender_name = (msg.get("sender") or "").strip()
            can_reply = bool(
                msg.get("id")
                and sender_name.lower() not in ("", "unknown", "system", "stud.ip")
                and not subject.lower().startswith("enrolment in course")
            )
            if subject.lower().startswith("enrolment in course"):
                markup = None
            else:
                btn_rows = []
                if can_reply:
                    btn_rows.append([InlineKeyboardButton("↩️ Reply on Stud.IP", callback_data=f"msg_reply|{msg['id']}")])
                btn_rows.append([InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")])
                markup = markup_for(chat_id, InlineKeyboardMarkup(btn_rows))
            # a Stud.IP message is private: only its recipient gets it
            await bot.send_message(chat_id=chat_id, text=text[:4000], parse_mode="HTML", reply_markup=markup)
            await asyncio.sleep(0.3)
        return True
    except Exception as e:
        logging.error("check_new_messages failed: %s", e)
        return False


FORUM_CACHE_PATH = "forum_cache.json"


def _load_forum_cache() -> dict:
    """{key: entry}; empty when missing or unreadable."""
    if not db.exists(FORUM_CACHE_PATH):
        return {}
    try:
        return db.load(FORUM_CACHE_PATH)
    except Exception:
        logging.warning("⚠️ Failed to load forum cache, using empty.")
        return {}


async def check_new_forum_posts_parallel(bot, chat_id, silent: bool = False, courses=None):
    """Parallel forum checking; `courses` is a watched_courses() plan."""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="💬 Checking forum posts...", disable_notification=True)

    try:
        # Structure: { key: { course, thread, author, date, body, ... } }
        cache = _load_forum_cache()
        if courses is None:
            await _ensure_watched_courses_map(session)
            courses = [(name, cid, session) for cid, name in state.courses_map.items()]

        semaphore = asyncio.Semaphore(3)  # Don't overload the server

        async def fetch(course_name, cid, sess):
            async with semaphore:
                try:
                    entries, used = await course_registry.scan_with_failover(
                        cid, sess, lambda s: _fetch_course_forum_strict(s, course_name, cid))
                except Exception as e:
                    logging.warning("Forum of %s unavailable with every member: %s", course_name, e)
                    return []
                course_registry.record_scan(cid, used, forum_hash=course_registry.content_hash(
                    sorted((p.get("key"), p.get("date")) for p in entries)))
                return entries

        results = await asyncio.gather(*(fetch(name, cid, sess) for name, cid, sess in courses),
                                       return_exceptions=True)
        new_posts_found = []
        for res in results:
            if not isinstance(res, list):
                continue
            for p in res:
                # Notify for threads not seen before that have unread postings
                if p["key"] not in cache and p.get("is_new"):
                    new_posts_found.append(p)
                # Always cache, so "Show Last 5" works immediately
                cache[p["key"]] = p

        for p in new_posts_found:
            markup = await _forum_alert_markup(p)
            await course_registry.fan_out(bot, p.get("cid"), "forum", _forum_alert_text(p)[:4000],
                                          forum_title_text(p), reply_markup=markup,
                                          members=course_members(p.get("cid")))

        # Always save cache to bootstrap the "Last 5" feature
        db.save(FORUM_CACHE_PATH, cache)

        if not new_posts_found and not silent:
            await bot.send_message(chat_id=chat_id, text="☑️ No new forum posts found (cache updated).", disable_notification=True)

    except Exception as e:
        logging.error("check_new_forum_posts_parallel fatal: %s", e)


async def check_new_forum_posts(bot, chat_id, silent: bool = False):
    """Wrapper"""
    return await check_new_forum_posts_parallel(bot, chat_id, silent)


# ── watcher loop ───────────────────────────────────────────────────────────────


CACHE_FILE = "messages_cache.json"

# ───────────────────────────────────────────────
# MAIN MESSAGE CHECK (same logic as file watcher)
# ───────────────────────────────────────────────

# Simple helpers for messages_cache.json file
MSG_CACHE_FILE = "messages_cache.json"
MAX_CACHE_SIZE = 200  # upper limit to avoid bloating cache


# ───────────────────────────────────────────────
# SHOW LAST 5 MESSAGES (from JSON cache)
# ───────────────────────────────────────────────


async def show_last_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 messages with full content including message body."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer()

    # First show "Loading..." message
    status_msg = await query.edit_message_text("📨 Loading last 5 messages...")
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading last 5 messages...")

    chat_id = query.message.chat_id

    try:
        # Load the tapping user's own message cache
        try:
            stored = stored_messages(query.from_user.id)
            if stored is None:
                await _stop_loading_animation(stop_evt, anim_task)
                await query.edit_message_text("⚠️ No message history available.")
                return
            all_messages = stored
        except Exception as e:
            logging.error("Failed to load message cache: %s", e)
            await _stop_loading_animation(stop_evt, anim_task)
            await query.edit_message_text("⚠️ Error loading message history.")
            return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if not all_messages:
        await query.edit_message_text("ℹ️ No messages found in history.")
        return

    # Sort messages by date (oldest to newest)
    all_messages_sorted = sorted(all_messages, key=lambda m: parse_date_safe(m.get("date", "")))

    # Get last 5 messages (newest 5 messages)
    last_five = all_messages_sorted[-5:] if len(all_messages_sorted) >= 5 else all_messages_sorted

    # Show header message
    await query.edit_message_text("📨 <b>Last 5 Messages</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    # Show messages in correct order (1=old, 5=new)
    for i, msg in enumerate(last_five, 1):
        sender = clean_for_html(msg.get("sender", "Unknown"))
        subject = clean_for_html(msg.get("title", "(No subject)"))
        date = clean_for_html(msg.get("date", "-"))

        # Get message content
        body = msg.get("body", "")
        if not body or "[Error" in body:
            body = "📭 <i>Message content not available</i>"
        else:
            body = clean_for_html(body.strip())
            if len(body) > 1500:  # Truncate if too long
                body = body[:1500] + "...\n\n📄 <i>Message truncated</i>"

        text = (
            f"📨 <b>Message {i}</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>👤 Sender:</b> {sender}\n"
            f"<b>📘 Subject:</b> {subject}\n"
            f"<b>🕒 Date:</b> {date}\n"
            "                   \n"
            f"\n{body}\n"
            "━━━━━━━━━━━━━━━━━"
        )

        raw_sender = (msg.get("sender") or "").strip().lower()
        raw_subj = (msg.get("title") or "").strip().lower()
        msg_markup = None
        if msg.get("id") and raw_sender not in ("", "unknown", "system", "stud.ip") and not raw_subj.startswith("enrolment in course"):
            msg_markup = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply on Stud.IP", callback_data=f"msg_reply|{msg['id']}")]])

        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=msg_markup)
        await asyncio.sleep(0.3)  # Rate limiting

    # Show Quick Access button
    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


MANUAL_CHECK_COOLDOWN_SECONDS = 15 * 60  # a student's own /check
_manual_check_at: dict[int, float] = {}


async def _manual_check_plan(user_id: Optional[int]):
    """(courses plan, None) for a manual check, or (None, reason to refuse).
    An admin checks every watched course with its scanner; a student only
    their own courses, with their own session, at most every
    MANUAL_CHECK_COOLDOWN_SECONDS (the background checks cover the rest)."""
    if user_id is not None and is_user_admin(user_id):
        return await watched_courses(), None
    if user_id is None:
        return None, "⚠️ Unknown user."
    wait = int(_manual_check_at.get(user_id, 0.0) + MANUAL_CHECK_COOLDOWN_SECONDS - time.time())
    if wait > 0:
        return None, f"⏳ You can check again in {max(1, wait // 60)} min. New items also arrive on their own."
    session = get_user_session(user_id)
    if session is None or not await session.is_logged_in():
        return None, "🔐 Sign in first (/login), then you can check your courses."
    courses = student_courses(user_id)
    if not courses:
        try:
            with acting_as(user_id):
                await list_courses(session=session, user_id=user_id)
        except Exception as e:
            logging.warning("Course list for a manual check unavailable: %s", e)
        courses = student_courses(user_id)
    _manual_check_at[user_id] = time.time()
    return [(name, cid, session) for cid, name in courses.items()], None


async def _run_check_now(update, context):
    """Manual full check: messages → announcements → files → forum posts,
    for the asking student's own inbox and courses (every course for an admin)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    is_admin = user_id is not None and is_user_admin(user_id)

    if chat_id in check_in_progress:
        await update.message.reply_text("⚠️ Another check is already running.")
        return

    try:
        check_in_progress.add(chat_id)
        plan, refused = await _manual_check_plan(user_id)
        if refused:
            await update.message.reply_text(refused)
            return
        if is_admin:
            state.global_watcher_paused = True  # an admin's full check replaces the watchers' turn

        # Send a single "Checking..." message
        status_msg = await update.message.reply_text(
            "🔍 Starting full browser-less check...\n\n"
            "📨 Checking messages...\n"
            "📢 Checking announcements...\n"
            "📁 Checking files...\n"
            "💬 Checking forum posts...",
            disable_notification=True
        )


        results = []

        # ✉️ 1. Check messages
        try:
            await status_msg.edit_text("🔍 Checking messages...")
            has_new_messages = await check_new_messages(context.bot, chat_id, silent=True)
            results.append(("messages", has_new_messages))
        except Exception as e:
            results.append(("messages", f"error: {e}"))

        # 📢 2. Check announcements
        try:
            await status_msg.edit_text("🔍 Checking announcements...")
            has_new_announcements = await check_new_announcements_parallel(context.bot, chat_id, silent=True, courses=plan)
            results.append(("announcements", has_new_announcements))
        except Exception as e:
            results.append(("announcements", f"error: {e}"))

        # 📁 3. Check files
        try:
            await status_msg.edit_text("🔍 Checking files...")
            has_new_files = await check_new_files_parallel(context.bot, chat_id, silent=True, courses=plan)
            results.append(("files", has_new_files))
        except Exception as e:
            results.append(("files", f"error: {e}"))

        # 💬 4. Check forum
        try:
            await status_msg.edit_text("🔍 Checking forum posts...")
            has_new_forum = await check_new_forum_posts_parallel(context.bot, chat_id, silent=True, courses=plan)
            results.append(("forum", has_new_forum))
        except Exception as e:
            results.append(("forum", f"error: {e}"))

        # Summary with Last 5 buttons
        summary = "🔍 Full check completed:\n\n"
        for check, res in results:
            icon = "✅" if res is True or (isinstance(res, list) and len(res) > 0) else ("❌" if "error" in str(res) else "☑️")
            label = {"messages": "Messages", "announcements": "Announcements", "files": "Files", "forum": "Forum"}[check]
            summary += f"{icon} {label}\n"

        await status_msg.edit_text(summary, reply_markup=get_show_last_keyboard())
        state.global_scan_count += 1
        state.last_full_check_time = berlin_now()

    except Exception as e:
        await update.message.reply_text(f"❌ Fatal error: {e}")
        logging.exception("_run_check_now failed")
    finally:
        check_in_progress.discard(chat_id)
        if is_admin:
            state.global_watcher_paused = False


# ── Feature 3: Direct Stud.IP Message Reply (`POST /jsonapi.php/v1/messages`) ─

async def send_studip_message(recipient_id: str, subject: str, message_body: str, _retry: bool = True) -> dict:
    """Send a Stud.IP internal message via `POST /jsonapi.php/v1/messages`."""
    session = await login_studip()  # the acting user sends from their own account
    url = f"{STUDIP_API_URL}/messages"
    payload = {
        "data": {
            "type": "messages",
            "attributes": {
                "subject": subject,
                "message": message_body,
                "priority": "normal",
            },
            "relationships": {
                "recipients": {
                    "data": [{"type": "users", "id": recipient_id}],
                }
            },
        }
    }
    headers = {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }
    async with await session.post(url, data=json.dumps(payload), headers=headers) as r:
        status, resp_text = r.status, await r.text()
    if status == 401 and _retry:
        use_oauth = studip_oauth.oauth_enabled()
        if use_oauth and studip_oauth.load_tokens() and await studip_oauth.get_access_token(force_refresh=True):
            return await send_studip_message(recipient_id, subject, message_body, _retry=False)
        # Self-healing silent SSO renewal attempt
        if await session.heal_session():
            return await send_studip_message(recipient_id, subject, message_body, _retry=False)
        await session.login()  # respects the failed-login backoff
        return await send_studip_message(recipient_id, subject, message_body, _retry=False)
    if status not in (200, 201, 202, 204):
        raise StudipApiError(f"POST /messages failed: HTTP {status} ({resp_text[:180]})")
    try:
        return json.loads(resp_text) if resp_text.strip() else {"ok": True}
    except ValueError:
        return {"ok": True}


async def handle_studip_message_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `msg_reply|<msg_id>` callback to initiate a direct Stud.IP message reply."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer()

    if not is_user_allowed(query.from_user.id):
        return

    parts = (query.data or "").split("|", 1)
    msg_id = parts[1].strip() if len(parts) > 1 else ""
    if not msg_id:
        await query.message.reply_text("⚠️ Invalid message ID.")
        return

    cached_msg = next((m for m in load_message_cache(query.from_user.id) if m.get("id") == msg_id), None)
    sender_id = (cached_msg or {}).get("sender_id")
    sender_name = (cached_msg or {}).get("sender") or "Unknown"
    orig_subject = (cached_msg or {}).get("title") or "(No subject)"

    # If `sender_id` wasn't cached yet, resolve it live from `/messages/{msg_id}?include=sender`
    if not sender_id:
        try:
            doc = await studip_api_get(f"/messages/{msg_id}", {"include": "sender"})
            data = doc.get("data") or {}
            sender_id = _api_rel_id(data, "sender")
            names = _api_names(doc.get("included", []))
            if sender_id and sender_id in names:
                sender_name = names[sender_id]
            subj = (data.get("attributes") or {}).get("subject")
            if subj:
                orig_subject = subj
        except Exception as exc:
            logging.warning("Could not resolve sender_id for message %s: %s", msg_id, exc)

    if not sender_id:
        await query.message.reply_text("⚠️ Could not determine the Stud.IP sender for this message.")
        return

    reply_subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    user_data: Any = context.user_data
    user_data["pending_step"] = "studip_msg_reply"
    user_data["studip_reply_target"] = {
        "msg_id": msg_id,
        "recipient_id": sender_id,
        "sender": sender_name,
        "subject": reply_subject,
    }

    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Reply", callback_data="msg_reply_cancel")]])
    await query.message.reply_text(
        "↩️ <b>Reply on Stud.IP</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>To:</b> {html.escape(sender_name)}\n"
        f"📘 <b>Subject:</b> {html.escape(reply_subject)}\n\n"
        "✍️ Type your reply below (or send a voice message):",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )


async def handle_studip_message_reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending Stud.IP message reply."""
    query: Any = update.callback_query  # PTB's Optional/MaybeInaccessible types
    await query.answer("Reply cancelled")
    user_data = context.user_data or {}
    user_data.pop("pending_step", None)
    user_data.pop("studip_reply_target", None)
    await query.edit_message_text("❌ Stud.IP reply cancelled.")


# ── /check and /watch commands ───────────────────────────────────────────────
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and not is_user_allowed(update.effective_user.id):
        return
    await _run_check_now(update, context)


def render_weekly_study_plan_message(plan: dict, gaps: list, exams: list, rows: list) -> str:
    """Final HTML Telegram message: workload vs. free time, the
    behind-schedule summary, then the concrete sessions grouped by day
    (or, when there is nothing to schedule, a plain listing of the free
    slots), then upcoming exams."""
    from unibot.study_plan import render_behind_lines, render_workload_lines
    sessions = plan["sessions"]
    lines = ["📘 <b>Your Study Plan for Next Week</b>", "━━━━━━━━━━━━━━━━━━"]
    lines += render_workload_lines(plan["stats"])
    lines.append("")
    lines += render_behind_lines(rows)
    lines.append("━━━━━━━━━━━━━━━━━━")

    if sessions:
        current_day = None
        for i, s in enumerate(sessions, 1):
            if s["day"] != current_day:
                if current_day is not None:
                    lines.append("")
                current_day = s["day"]
                lines.append(f"<b>{s['day']} {datetime.fromisoformat(s['date']):%d.%m}</b>")
            lines.append(f"{i}. 🕑 {s['start']}–{s['end']}")
            last_unit = None
            for p in s["pieces"]:
                part = f" ({p['part']})" if p.get("part") else ""
                if (p["cid"], p["unit"]) != last_unit:
                    unit = p["unit_label"] if len(p["unit_label"]) <= 45 else p["unit_label"][:44] + "…"
                    lines.append(f"   <b>{_course_abbrev(p['course'])}</b> {html.escape(unit, quote=False)} <i>({p['reason']})</i>")
                    last_unit = (p["cid"], p["unit"])
                total = p.get("task_minutes", p["minutes"])
                amount = f"{p['minutes']} of {total} min" if p["minutes"] < total else f"{p['minutes']} min"
                lines.append(f"     • {html.escape(p['title'], quote=False)}{part}: {amount}")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("Tap a session's button when you've done it: its minutes count towards your progress. /progress shows the full overview.")
    else:
        lines.append("(no lecture material to schedule yet, here are your free windows)")
        for g in gaps:
            context = "fully free day" if g["after"] == g["before"] == "no classes that day" else f"between {html.escape(g['after'])} and {html.escape(g['before'])}"
            lines.append(f"{g['start_dt']:%A} {g['start_dt']:%H:%M}–{g['end_dt']:%H:%M} ({context})")
        lines.append("━━━━━━━━━━━━━━━━━━")

    for e in exams[:3]:
        lines.append(f"⏳ Exam in {e['days_until']} day(s): {html.escape(e['title'])} ({e.get('date')})")

    return "\n".join(lines)


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe an incoming voice note. If a wizard step is already pending
    (e.g. waiting for a task's due time), the
    transcription answers that step; otherwise - a fresh, unprompted voice
    note - it's routed by spoken intent (see classify_voice_intent /
    _route_voice_intent) instead of always assuming a new task."""
    from unibot.voice import _route_voice_intent, classify_voice_intent
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return
    message, chat, user_data = update.message, update.effective_chat, context.user_data
    if message is None or message.voice is None or chat is None or user_data is None:
        return

    status_msg = await message.reply_text("🎙️ Transcribing...", disable_notification=True)
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    ogg_path = os.path.join(tempfile.gettempdir(), f"voice_{message.voice.file_unique_id}.oga")
    try:
        file = await context.bot.get_file(message.voice.file_id)
        await file.download_to_drive(ogg_path)
        transcript = await transcribe_voice_message(ogg_path)
    except Exception as e:
        logging.warning("Voice message transcription failed", exc_info=True)
        await status_msg.edit_text(f"⚠️ {e}")
        return

    # Google's free STT can mishear things, so confirm before it's actually used
    # to answer the wizard step (or classified as a fresh command) - nothing is
    # committed yet. `step` is None when there's no wizard waiting, which is
    # what tells handle_voice_confirm to run intent classification instead of
    # feeding straight into handle_pending_step.
    step = user_data.get("pending_step") or None

    # For read-only, instantly-answerable intents (currently just "what
    # should I eat"), classify right away and skip the "Heard...Is this
    # correct?" / "Understanding..." confirmation entirely - there's no
    # destructive action or wizard step to protect here, and it reads
    # oddly to interrupt a direct answer with an extra confirm step.
    intent_result = None
    if step is None:
        intent_result = await classify_voice_intent(transcript)
        if intent_result["intent"] == "recommend_meal":
            await status_msg.delete()
            await _route_voice_intent(update, context, intent_result, transcript)
            return

    user_data["pending_voice"] = {"step": step, "text": transcript, "intent_result": intent_result}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="voice_confirm"),
        InlineKeyboardButton("✏️ Try again", callback_data="voice_reject"),
    ]])
    await status_msg.edit_text(f"🎙️ Heard: “{transcript}”\n\nIs this correct?", reply_markup=keyboard)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, chat = update.message, update.effective_chat
    if message is None or chat is None:
        return
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await message.reply_text("Not authorized to use this bot.")
        return
    if not is_user_admin(user_id):
        # one watcher serves everyone; a student's own checks run on their schedule
        await message.reply_text("👀 Your courses and inbox are checked automatically. "
                                 "/check looks right now, /stop pauses everything and /start resumes.")
        return
    # never a second watcher loop: start the one watcher if it isn't running, else resume it
    from unibot.watcher import start_unified_watcher
    state.global_watcher_paused = False
    state.message_watcher_paused = state.announcement_watcher_paused = state.file_watcher_paused = False
    if state.watcher_controller_running:
        await message.reply_text("👀 The watcher is running (paused checks resumed).", disable_notification=True)
        return
    await start_unified_watcher(context.application)
    await message.reply_text("👀 Watcher started.", disable_notification=True)
