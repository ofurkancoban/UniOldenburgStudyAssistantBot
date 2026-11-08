"""Watchers for announcements, files, forum posts and messages, plus 'show last' views.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import studip_oauth
from typing import Optional
from studip_utils import TZ_BERLIN, berlin_now, redact_url

from unibot import db
from unibot import state
from unibot.config import ALLOWED_USER_IDS
from unibot.storage import link_cache, load_files_cache, save_files_cache, single_flight
from unibot.formatting import (
    _course_abbrev,
    _short_id,
    _truncate_with_ellipsis,
    clean_for_html,
    clean_html_text,
    parse_date_safe,
)
from unibot.telegram_ui import _show_typing, _start_loading_animation, _stop_loading_animation, broadcast
from unibot.auth import act_as_bot, acting_as, current_user_id, get_user_session, has_stored_login, is_user_allowed, login_studip
from unibot.studip_api import (
    BASE_URL,
    STUDIP_API_URL,
    StudipApiError,
    _api_berlin,
    _api_html_to_text,
    _api_message,
    _api_names,
    _api_rel_id,
    _ensure_watched_courses_map,
    _parse_file_date_v2,
    list_files,
    studip_api_get,
    studip_api_get_all,
    studip_api_user_id,
)
from unibot.ai import transcribe_voice_message


global_message_watcher = None
global_announcement_watcher = None
task_suggestion_cache = {}  # short_id -> { text, due_time (ISO or None), course }
announcement_task_cache = {}  # short_id -> { course, subject, body } - raw announcement, converted to a task on demand when "📝 Add as Task" is tapped
watch_tasks = {}  # chat_id -> asyncio.Task
check_in_progress: set[int] = set()  # chat_id set to prevent concurrent checks


def message_cache_key(user_id: Optional[int] = None) -> str:
    """Inbox cache per Telegram user; the admin session keeps the original file."""
    if user_id is None:
        user_id = current_user_id()
    if user_id is None:
        return "messages_cache.json"
    sess = state.user_sessions.get(user_id)
    if sess is not None and sess is state.global_session:
        return "messages_cache.json"
    return f"messages_cache_{user_id}.json"


def load_message_cache(user_id: Optional[int] = None) -> list[dict]:
    """Load the acting user's message cache."""
    key = message_cache_key(user_id)
    try:
        if db.exists(key):
            return db.load(key)
    except Exception as e:
        logging.warning("load_message_cache failed: %s", e)
    return []


async def watch_loop(chat_id, context):
    """Legacy watch loop - now delegates to unified watcher (which uses context.application)"""
    from unibot.watcher import unified_watcher_controller
    if hasattr(context, "application"):
        await unified_watcher_controller(context.application)
    else:
        # Fallback if context doesn't have application
        logging.warning("watch_loop called without application context")


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
    query = update.callback_query
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

    all_files = []
    for cid, files in cache.items():
        if not isinstance(files, dict):
            continue
        for name, info in files.items():
            if not isinstance(info, dict):
                continue
            course = info.get("course") or state.courses_map.get(cid) or cid
            ts = info.get("modified_ts")
            dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.min
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


def _parse_ann_date(s: str) -> datetime:
    """Safely convert Stud.IP announcement date string (default: very old)."""
    if not s:
        return datetime.min
    s = s.strip()
    fmts = [
        "%d/%m/%y", "%d/%m/%Y",  # 17/09/25
        "%d.%m.%y", "%d.%m.%Y",  # 17.09.2025
        "%d %b %y", "%d %b %Y",  # 17 Sep 25
        "%d.%m.%y %H:%M", "%d.%m.%Y %H:%M",
        "%d/%m/%y %H:%M", "%d/%m/%Y %H:%M",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except (ValueError, TypeError):
            pass
    # If text comes and contains 2-digit year, rough estimate:
    try:
        parts = re.findall(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s)
        if parts:
            d, m, y = parts[0]
            y = int(y)
            if y < 100:  # 25 -> 2025 assumption
                y += 2000
            return datetime(int(y), int(m), int(d))
    except (ValueError, IndexError):
        pass
    return datetime.min


async def _course_announcements_api(cid, course_name) -> list:
    """The course's currently published announcements from the JSON:API, in
    the same shape (and with the same "cid:news_id" key) as the ones scraped
    from the course overview page."""
    news, included = await studip_api_get_all(f"/courses/{cid}/news", {"include": "author"})
    names = _api_names(included)
    now = datetime.now(TZ_BERLIN)
    result = []
    for n in news:
        a = n["attributes"]
        start, end = _api_berlin(a.get("publication-start")), _api_berlin(a.get("publication-end"))
        if (start and start > now) or (end and end < now):
            continue
        dt = _api_berlin(a.get("publication-start") or a.get("mkdate"))
        date_s = dt.strftime("%d/%m/%y") if dt else ""
        result.append({
            "key": f"{cid}:{n['id']}", "cid": cid, "course": course_name,
            "subject": a.get("title") or "(No subject)",
            "sender": names.get(_api_rel_id(n, "author"), "Unknown"),
            "date": date_s, "dt": _parse_ann_date(date_s).isoformat(),
            "body": _api_html_to_text(a.get("content")) or "(No content)",
        })
    return result


async def check_new_announcements_parallel(bot, chat_id, silent: bool = False):
    """Parallel announcement checking for new items"""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📢 Checking announcements...", disable_notification=True)

    try:
        CACHE_PATH = "announcement_cache.json"
        cache = {"seen": [], "history": []}
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

        await _ensure_watched_courses_map(session)
        courses = [(name, cid) for cid, name in state.courses_map.items()]

        all_found = []
        semaphore = asyncio.Semaphore(3)

        async def check_single_course_announcements(course_name, cid):
            async with semaphore:
                try:
                    return course_name, cid, await _course_announcements_api(cid, course_name)
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

        tasks = [check_single_course_announcements(name, cid) for name, cid in courses]
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
                await broadcast(bot, text[:4000], parse_mode="HTML", reply_markup=markup, cid=ann.get("cid"))
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


async def fetch_message_body(session, message_url):
    """Fetch message body from detail page with improved parsing."""
    try:
        async with await session.get(message_url) as resp:
            if resp.status != 200:
                return f"[HTTP Error: {resp.status}]"
            html = await resp.text()

        # lxml, not html.parser - see the comment in check_new_announcements_parallel
        # on the same inline-SVG-icon parse-tree corruption bug.
        soup = BeautifulSoup(html, "lxml")

        # Try different content containers
        content_selectors = [
            "div.formatted-content",
            "div.message-content",
            "div.news-content",
            "div.mail_content",
            ".formatted-content.ck-content"
        ]

        for selector in content_selectors:
            content = soup.select_one(selector)
            if content:
                # Remove unnecessary elements
                for elem in content.select("script, style, nav, header, footer"):
                    elem.decompose()

                text = clean_html_text(content)
                if text and len(text.strip()) > 10:  # Meaningful content check
                    return text

        # Fallback: extract text from entire page
        main_content = soup.select_one("main") or soup.select_one("article") or soup
        text = clean_html_text(main_content)

        # Truncate if too long
        if len(text) > 3000:
            text = text[:3000] + "..."

        return text if text.strip() else "No content found."

    except Exception as e:
        logging.error("fetch_message_body failed for %s: %s", redact_url(message_url), e)
        return f"[Error fetching content: {str(e)[:100]}]"


async def show_last_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 announcements (with labeled header)."""
    query = update.callback_query
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
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    CACHE_PATH = FORUM_CACHE_PATH

    if not db.exists(CACHE_PATH):
        await query.edit_message_text("⚠️ No forum history available.")
        return

    try:
        cache = db.load(CACHE_PATH)
    except Exception:
        await query.edit_message_text("⚠️ Error reading forum history.")
        return

    # Extract all posts from cache
    all_posts = []
    for post in cache.values():
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


def _snapshot_course_files(files: list) -> dict:
    """Files of one course listing keyed by name, with a modified_ts added."""
    current = {}
    for f in files:
        if f.get("type") != "file" or not f.get("name"):
            continue
        dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
        f["modified_ts"] = None if dt == datetime.min else int(dt.timestamp())
        current[f["name"]] = f
    return current


def _file_was_updated(old: dict, cur: dict) -> bool:
    """Whether a file seen before really changed.

    A size change always counts. With the same size, only a timestamp jump of
    more than 24 hours counts: smaller differences are the day-level date
    resolution of some listings or a re-upload of the same file, and a
    timestamp appearing or disappearing is just a format change.
    """
    old_size = str(old.get("size", "-")).strip()
    cur_size = str(cur.get("size", "-")).strip()
    if old_size != cur_size:
        return True
    old_ts, cur_ts = old.get("modified_ts"), cur.get("modified_ts")
    if old_ts is None or cur_ts is None:
        return False
    return abs(cur_ts - old_ts) > 86400


def _diff_course_files(current: dict, old: dict) -> tuple[list, list]:
    """(new files, updated files) of one course."""
    new_files = [f for name, f in current.items() if name not in old]
    changed = [f for name, f in current.items() if name in old and _file_was_updated(old.get(name, {}), f)]
    return new_files, changed


def _file_events(course_name: str, cid: str, new_files: list, changed_files: list) -> list[dict]:
    events = []
    for kind, flist in (("new", new_files), ("changed", changed_files)):
        for f in flist:
            events.append({
                "type": kind,
                "cid": cid,
                "course": course_name,
                "name": f.get("name"),
                "size": f.get("size", "-"),
                "modified": f.get("modified", "-"),
                "modified_iso": f.get("modified_iso"),
                "dt": _parse_file_date_v2(f.get("modified"), f.get("modified_iso")),
                "url": f.get("url"),
            })
    return events


def _file_alert(it: dict, sid: str) -> tuple[str, InlineKeyboardMarkup]:
    """Notification text and buttons (download, plus an audio summary for
    PDF/PPTX/HTML) for one new or updated file."""
    title = "🔥 🆕 <b>NEW FILE ADDED</b> 🔥" if it["type"] == "new" else "🔥 ♻️ <b>FILE UPDATED</b> 🔥"
    text = (
        f"{title}\n"
        "                 \n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📚 <b>Course:</b> {html.escape(str(it['course']))}\n"
        f"📄 <b>File:</b> {html.escape(str(it['name']))}\n"
        f"📅 <b>Date:</b> {html.escape(str(it['modified']))}\n"
        f"💾 <b>Size:</b> {html.escape(str(it['size']))}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    btn_rows = [[InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")]]
    if os.path.splitext(it.get("name") or "")[1].lower() in (".pdf", ".pptx", ".html"):
        btn_rows.append([InlineKeyboardButton("🎧 1-Min Audio Summary", callback_data=f"file_audio_sum|{sid}")])
    return text, InlineKeyboardMarkup(btn_rows)


async def _scan_course_files(course_name: str, cid: str, cache: dict) -> tuple[list, bool]:
    """(new/updated file events, cache changed) for one course.

    The cache always gets the fresh listing, even without a notification, so
    later comparisons use the latest timestamps.
    """
    try:
        files = await list_files(cid)
        if not files:
            return [], False
        current = _snapshot_course_files(files)
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


async def _send_file_alerts(bot, events: list) -> None:
    """Send file notifications in chronological order, each with download buttons."""
    for it in sorted(events, key=lambda x: (x["dt"], x["course"], x["name"])):
        try:
            sid = _short_id()
            link_cache[sid] = {
                "cid": it["cid"],
                "name": it["name"],
                "course": it["course"],
                "url": it.get("url"),
                "ts": time.time(),
                "user_id": next(iter(ALLOWED_USER_IDS), None),
            }
            text, keyboard = _file_alert(it, sid)
            await broadcast(bot, text, parse_mode="HTML", reply_markup=keyboard, cid=it.get("cid"))
            await asyncio.sleep(0.3)  # Rate limiting
        except Exception as e:
            logging.error("Error sending notification for file %s: %s", it.get('name'), e)


@single_flight
async def check_new_files_parallel(bot, chat_id, silent: bool = False):
    """Parallel file checking for better performance"""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📁 Checking files...", disable_notification=True)

    try:
        cache = load_files_cache()
        await _ensure_watched_courses_map(session)
        courses = [(name, cid) for cid, name in state.courses_map.items()]
        if not courses:
            return

        semaphore = asyncio.Semaphore(1)

        async def scan(course_name, cid):
            async with semaphore:
                res = await _scan_course_files(course_name, cid, cache)
                await asyncio.sleep(0.5)  # Pace queries politely
                return res

        results = await asyncio.gather(*(scan(name, cid) for name, cid in courses), return_exceptions=True)
        all_events = []
        updated = False
        for result in results:
            if isinstance(result, Exception):
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


# How many of the newest inbox messages each check compares against the
# cache (the overview page showed about this many).
MESSAGE_CHECK_LIMIT = 20
# The inbox is one small API request, so it's polled on its own short loop
# (see message_poll_loop) instead of once per multi-minute watcher cycle.
MESSAGE_POLL_SECONDS = 60


async def _fetch_messages_api(limit: int = MESSAGE_CHECK_LIMIT, session=None) -> list:
    """The newest `limit` inbox messages, newest first, bodies included."""
    uid = await studip_api_user_id(session=session)
    if not uid:
        raise StudipApiError("Stud.IP user id unknown")
    page = await studip_api_get(f"/users/{uid}/inbox", {"page[limit]": limit, "include": "sender"}, session=session)
    names = _api_names(page.get("included", []))
    return [_api_message(m, names) for m in page.get("data", [])]


async def _fetch_messages_html(session) -> list:
    """Overview-page rows (no bodies), the fallback when the API fails."""
    url = f"{BASE_URL}/dispatch.php/messages/overview"
    async with await session.get(url) as resp:
        html_text = await resp.text()

    # lxml, not html.parser: see the comment in check_new_announcements_parallel
    # on the same inline-SVG-icon parse-tree corruption bug.
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("table#messages tbody tr[id^='message_']")
    all_messages = []
    for tr in rows:
        link_tag = tr.select_one("td.title a[href*='dispatch.php/messages/read/']")
        if not link_tag: continue
        href = link_tag["href"].strip()
        title = link_tag.get_text(strip=True)
        sender_tag = tr.select_one("td:nth-of-type(3)")
        sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
        date_tag = tr.select_one("td:nth-of-type(4)")
        date = date_tag.get_text(strip=True) if date_tag else "Unknown"

        if not href.startswith('http'):
            href = f"{BASE_URL}/{href.lstrip('/')}"

        all_messages.append({
            "id": tr["id"].replace("message_", ""), "title": title, "sender": sender, "date": date, "url": href,
        })
    return all_messages


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
    admin_id = next(iter(ALLOWED_USER_IDS), None)
    last_user_poll: dict[int, float] = {}
    while True:
        await asyncio.sleep(MESSAGE_POLL_SECONDS)
        if state.global_watcher_paused:
            continue
        with acting_as(None):
            if state.global_session is not None and admin_id is not None and has_stored_login(state.global_session):
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
    cache_key = message_cache_key()
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📨 Checking messages...", disable_notification=True)

    try:
        try:
            all_messages = await _fetch_messages_api(session=session)
        except Exception as e:
            logging.warning("check_new_messages: API failed (%s), falling back to the overview page", e)
            all_messages = await _fetch_messages_html(session)

        if not db.exists(cache_key):
            # first check for this user: remember the inbox as it is instead
            # of sending every old message as "new"
            db.save(cache_key, all_messages[:100])
            if not silent:
                await bot.send_message(chat_id=chat_id, text="☑️ No new messages found.", disable_notification=True)
            return False

        old_messages = []
        try:
            old_messages = db.load(cache_key)
        except (OSError, ValueError):
            pass

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
        db.save(cache_key, all_messages_combined)

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
                markup = InlineKeyboardMarkup(btn_rows)
            # a Stud.IP message is private: only its recipient gets it
            await bot.send_message(chat_id=chat_id, text=text[:4000], parse_mode="HTML", reply_markup=markup)
            await asyncio.sleep(0.3)
        return True
    except Exception as e:
        logging.error("check_new_messages failed: %s", e)
        return False


FORUM_CACHE_PATH = "forum_cache.json"
JSONAPI_HEADERS = {"Accept": "application/vnd.api+json"}


def _forum_author_name(posts_data: dict, author_id) -> str:
    """Author name from the postings response's included forum members."""
    if author_id and "included" in posts_data:
        for inc in posts_data["included"]:
            if inc.get("type") == "forum-members" and inc.get("id") == author_id:
                return inc.get("attributes", {}).get("name", "Unknown")
    return "Unknown"


def _friendly_forum_date(iso_str) -> str:
    if not iso_str:
        return "Unknown date"
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(iso_str)


def _post_author_id(post: dict):
    return post.get("relationships", {}).get("author", {}).get("data", {}).get("id")


def _html_to_text(content_html: str) -> str:
    return BeautifulSoup(content_html, "html.parser").get_text(separator=" ", strip=True)


def _forum_thread_entry(course_name: str, cid: str, disc: dict, posts_data: dict) -> Optional[dict]:
    """Cache/notification entry for one discussion and its postings, or None
    when the discussion has no postings."""
    posts = posts_data.get("data", [])
    if not posts:
        return None
    title = disc.get("attributes", {}).get("title", "Unknown")
    disc_id = disc.get("id")

    # The whole conversation for context, at most the last 15 messages so
    # the message stays under Telegram's 4096 character limit.
    history = []
    for post in posts:
        attrs = post.get("attributes", {})
        p_html = attrs.get("content-html", "")
        p_body = _truncate_with_ellipsis(_html_to_text(p_html), 400) if p_html else "No content"
        p_author = _forum_author_name(posts_data, _post_author_id(post))
        history.append(f"👤 <b>{html.escape(p_author)}</b> <i>({_friendly_forum_date(attrs.get('mkdate', ''))})</i>:\n{html.escape(p_body)}")
    if len(history) > 15:
        history = ["<i>... (older messages omitted) ...</i>"] + history[-15:]

    last_post = posts[-1]
    last_attrs = last_post.get("attributes", {})
    time_str = _friendly_forum_date(last_attrs.get("mkdate", berlin_now().isoformat()))
    last_content_html = last_attrs.get("content-html", "")
    last_message_plain = _html_to_text(last_content_html) if last_content_html else ""
    if len(history) > 1:
        final_body = "💬 <b>Conversation History:</b>\n\n" + "\n\n".join(history)
    else:
        body = _truncate_with_ellipsis(last_message_plain, 3000) if last_message_plain else "No content"
        final_body = f"📩 <b>Last message:</b>\n{html.escape(body)}"

    return {
        "course": course_name,
        "thread": title,
        "author": _forum_author_name(posts_data, _post_author_id(last_post)),
        "date": time_str,
        "body": final_body,
        "last_message_plain": last_message_plain,
        "key": f"{cid}:{title}:{disc_id}",
        # A thread counts as new when Stud.IP reports unread postings
        "is_new": disc.get("meta", {}).get("unread-postings-count", 0) > 0,
        "timestamp": time_str,
    }


async def _fetch_course_forum(session, course_name: str, cid: str) -> list:
    """Entries for the 10 most recently updated discussions of one course."""
    try:
        url = f"{BASE_URL}/jsonapi.php/v1/courses/{cid}/forum-discussions"
        async with await session.get(url, headers=JSONAPI_HEADERS) as r:
            if r.status != 200:
                return []
            data = await r.json()
        entries = []
        for disc in data.get("data", [])[:10]:
            posts_url = f"{BASE_URL}/jsonapi.php/v1/forum-discussions/{disc.get('id')}/postings?include=author"
            async with await session.get(posts_url, headers=JSONAPI_HEADERS) as r2:
                if r2.status != 200:
                    continue
                posts_data = await r2.json()
            entry = _forum_thread_entry(course_name, cid, disc, posts_data)
            if entry:
                entries.append(entry)
        return entries
    except Exception as e:
        logging.warning("Error while checking forum API for %s: %s", course_name, e)
        return []


def _load_forum_cache() -> dict:
    """{key: entry}; empty when missing or unreadable."""
    if not db.exists(FORUM_CACHE_PATH):
        return {}
    try:
        return db.load(FORUM_CACHE_PATH)
    except Exception:
        logging.warning("⚠️ Failed to load forum cache, using empty.")
        return {}


def _forum_alert_text(p: dict) -> str:
    return (
        "🔥 💬 <b>NEW FORUM POST</b> 🔥\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🏫 <b>Course:</b> {html.escape(p.get('course', ''))}\n"
        f"📂 <b>Thread:</b> {html.escape(p.get('thread', ''))}\n"
        f"👤 <b>By:</b> {html.escape(p.get('author', ''))}\n"
        f"🕒 <b>Date:</b> {html.escape(p.get('date', '-'))}\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"{p['body']}\n"
        "━━━━━━━━━━━━━━━━━"
    )


async def _forum_alert_markup(p: dict) -> InlineKeyboardMarkup:
    """Forward button, plus an "Add as task" button when the AI finds a task."""
    from unibot.tasks import extract_task_suggestion
    buttons = [[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]]
    if p.get("last_message_plain"):
        try:
            suggestion = await extract_task_suggestion(p.get('course', ''), p.get('thread'), p['last_message_plain'])
        except Exception as e:
            logging.warning("Task suggestion extraction failed for forum post: %s", e)
            suggestion = None
        if suggestion:
            sid = _short_id()
            task_suggestion_cache[sid] = {**suggestion, "course": p.get('course')}
            buttons.append([InlineKeyboardButton(f"📝 Add as task: {suggestion['text'][:40]}", callback_data=f"tasksug_add|{sid}")])
    return InlineKeyboardMarkup(buttons)


async def check_new_forum_posts_parallel(bot, chat_id, silent: bool = False):
    """Parallel forum checking for new posts browser-less"""
    session = state.global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="💬 Checking forum posts...", disable_notification=True)

    try:
        # Structure: { key: { course, thread, author, date, body, ... } }
        cache = _load_forum_cache()
        await _ensure_watched_courses_map(session)
        courses = [(name, cid) for cid, name in state.courses_map.items()]

        semaphore = asyncio.Semaphore(3)  # Don't overload the server

        async def fetch(course_name, cid):
            async with semaphore:
                return await _fetch_course_forum(session, course_name, cid)

        results = await asyncio.gather(*(fetch(name, cid) for name, cid in courses), return_exceptions=True)
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
            await broadcast(bot, _forum_alert_text(p)[:4000], parse_mode="HTML", reply_markup=markup, cid=p.get("cid"))

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
    query = update.callback_query
    await query.answer()

    # First show "Loading..." message
    status_msg = await query.edit_message_text("📨 Loading last 5 messages...")
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading last 5 messages...")

    chat_id = query.message.chat_id

    try:
        # Load the tapping user's own message cache
        cache_key = message_cache_key(query.from_user.id)
        if not db.exists(cache_key):
            await _stop_loading_animation(stop_evt, anim_task)
            await query.edit_message_text("⚠️ No message history available.")
            return

        try:
            all_messages = db.load(cache_key)
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


async def _run_check_now(update, context):
    """Manual full check: messages → announcements → files → forum posts"""
    chat_id = update.effective_chat.id

    if chat_id in check_in_progress:
        await update.message.reply_text("⚠️ Another check is already running.")
        return

    try:
        check_in_progress.add(chat_id)
        state.global_watcher_paused = True  # ✅ Pause global watchers

        # Send a single "Checking..." message
        status_msg = await update.message.reply_text(
            "🔍 Starting full browser-less check...\n\n"
            "📨 Checking messages...\n"
            "📢 Checking announcements...\n"
            "📁 Checking files...\n"
            "💬 Checking forum posts...",
            disable_notification=True
        )

        # ♻️ Ensure session is alive
        if not state.global_session or not await state.global_session.is_logged_in():
            await status_msg.edit_text("🔄 Session expired, re-logging...")
            await login_studip()

        # ✅ Load courses ONCE if needed
        if not state.courses_map:
            await _ensure_watched_courses_map(state.global_session)

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
            has_new_announcements = await check_new_announcements_parallel(context.bot, chat_id, silent=True)
            results.append(("announcements", has_new_announcements))
        except Exception as e:
            results.append(("announcements", f"error: {e}"))

        # 📁 3. Check files
        try:
            await status_msg.edit_text("🔍 Checking files...")
            has_new_files = await check_new_files(context.bot, chat_id, silent=True)
            results.append(("files", has_new_files))
        except Exception as e:
            results.append(("files", f"error: {e}"))

        # 💬 4. Check forum
        try:
            await status_msg.edit_text("🔍 Checking forum posts...")
            has_new_forum = await check_new_forum_posts(context.bot, chat_id, silent=True)
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
    query = update.callback_query
    await query.answer()

    if not is_user_allowed(query.from_user.id):
        return

    parts = (query.data or "").split("|", 1)
    msg_id = parts[1].strip() if len(parts) > 1 else ""
    if not msg_id:
        await query.message.reply_text("⚠️ Invalid message ID.")
        return

    cached_msg = next((m for m in load_message_cache() if m.get("id") == msg_id), None)
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
    context.user_data["pending_step"] = "studip_msg_reply"
    context.user_data["studip_reply_target"] = {
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
    query = update.callback_query
    await query.answer("Reply cancelled")
    context.user_data.pop("pending_step", None)
    context.user_data.pop("studip_reply_target", None)
    await query.edit_message_text("❌ Stud.IP reply cancelled.")


def _format_exam_dates_message(header: str, exams: list[dict], show_status: bool = True) -> list[str]:
    """`show_status` controls the per-exam registration-status line (Registered /
    Not registered / Passed / Failed). Off for the full-curriculum list, where
    most entries are irrelevant to the student and that line is just noise."""
    from unibot.exams import _exam_registration_status_line, _registration_period_line
    lines = [header, "━━━━━━━━━━━━━━━━━"]
    for e in exams:
        title = e["title"]
        if e.get("group_label"):
            title += f" ({e['group_label']})"
        lines.append(f"📘 <b>{title}</b>")
        lines.append(f"📅 {e['exam_date_display']}")
        if e.get("room"):
            lines.append(f"📍 {e['room']}")
        period_line = _registration_period_line(e)
        if period_line:
            lines.append(period_line)
        if show_status:
            lines.append(_exam_registration_status_line(e))
        lines.append("━━━━━━━━━━━━━━━━━")
    return lines


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

    status_msg = await update.message.reply_text("🎙️ Transcribing...", disable_notification=True)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    ogg_path = os.path.join(tempfile.gettempdir(), f"voice_{update.message.voice.file_unique_id}.oga")
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        await file.download_to_drive(ogg_path)
        transcript = await transcribe_voice_message(ogg_path)
    except Exception as e:
        await status_msg.edit_text(f"⚠️ {e}")
        return

    # Google's free STT can mishear things, so confirm before it's actually used
    # to answer the wizard step (or classified as a fresh command) - nothing is
    # committed yet. `step` is None when there's no wizard waiting, which is
    # what tells handle_voice_confirm to run intent classification instead of
    # feeding straight into handle_pending_step.
    step = context.user_data.get("pending_step") or None

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

    context.user_data["pending_voice"] = {"step": step, "text": transcript, "intent_result": intent_result}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="voice_confirm"),
        InlineKeyboardButton("✏️ Try again", callback_data="voice_reject"),
    ]])
    await status_msg.edit_text(f"🎙️ Heard: “{transcript}”\n\nIs this correct?", reply_markup=keyboard)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return
    if chat_id in watch_tasks and not watch_tasks[chat_id].done():
        await update.message.reply_text("Watcher is already running.")
        return
    await update.message.reply_text("👀 Watcher started (checks every 2 hours).", disable_notification=True)
    task = asyncio.create_task(watch_loop(chat_id, context))
    watch_tasks[chat_id] = task
