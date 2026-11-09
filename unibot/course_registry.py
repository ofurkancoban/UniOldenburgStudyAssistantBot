"""Course registry and fan-out.

Every Stud.IP course is scanned once per cycle no matter how many users
follow it. The registry has two layers in SQLite: global_courses (the course
itself, its last scan and content hashes, no user data) and user_courses
(who follows it, with per-course notification preferences).

The scan of a course is done with one member's session, the designated
scanner. Scanners rotate round-robin among the members whose session is
signed in, so the request load is spread across students; when a scan fails
(cookie expired, password changed) the next member takes over in the same
cycle and the failed session is skipped for a while.

Alerts are fanned out: built once, then delivered to each follower in the
format they picked (full card with sound, title line only, or silent card),
skipped for muted courses and switched off alert kinds.
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN
from unibot import db, state
from unibot.auth import ACTIVE, has_stored_login, is_user_allowed, lifecycle_state
from unibot.preferences import DIGEST_HOUR, get_pref, in_quiet_hours, morning_send_time
from unibot.storage import load_default_semester
from unibot.studip_api import BASE_URL
from unibot.telegram_ui import broadcast_targets

SCANNER_FAILURE_COOLDOWN = 3600
KIND_COLUMN = {
    "files": "notify_files",
    "announcements": "notify_announcements",
    "forum": "notify_forum",
}
FORMAT_LABELS = {
    "full": "🔔 Full card",
    "title": "📝 Title only",
    "silent": "🔕 Silent card",
}
DEFAULT_FORMAT = "silent"  # what every alert did before per-course preferences

# cid -> [(uid, session)] members that can scan the course
_candidates: dict[str, list] = {}
# cid -> how many times a scanner was picked (the round-robin cursor)
_rotation: dict[str, int] = {}
# uid -> time of the last failed scan with that user's session
_failed_at: dict[int, float] = {}
# counters of the last sync, for the digest
last_stats: dict = {}
# scans handed over to another member since the last digest
failover_count = 0


class ScanFailed(Exception):
    """A scan result that means the session cannot read the course."""


def _uid_of(session) -> Optional[int]:
    return getattr(session, "user_id", None)


def _healthy(uid: Optional[int], session, now: float) -> bool:
    if not has_stored_login(session):
        return False
    failed = _failed_at.get(uid) if uid is not None else None
    return failed is None or now - failed > SCANNER_FAILURE_COOLDOWN


def report_failure(session, now: Optional[float] = None) -> None:
    uid = _uid_of(session)
    if uid is not None:
        _failed_at[uid] = now if now is not None else time.time()


def report_success(session) -> None:
    uid = _uid_of(session)
    if uid is not None:
        _failed_at.pop(uid, None)


def set_candidates(cid: str, candidates: list) -> None:
    """[(uid, session)] members whose sessions may scan the course."""
    seen = set()
    unique = []
    for uid, sess in candidates:
        if id(sess) in seen:
            continue
        seen.add(id(sess))
        unique.append((uid, sess))
    _candidates[cid] = unique


def pick_scanner(cid: str, now: Optional[float] = None):
    """The next healthy member session for the course (round-robin), any
    member session when none is healthy, None when the course has none."""
    cands = _candidates.get(cid) or []
    if not cands:
        return None
    now = now if now is not None else time.time()
    healthy = [c for c in cands if _healthy(c[0], c[1], now)]
    pool = healthy or cands
    turn = _rotation.get(cid, 0)
    _rotation[cid] = turn + 1
    return pool[turn % len(pool)][1]


def failover_order(cid: str, primary, now: Optional[float] = None) -> list:
    """The primary session first, then the course's other healthy members."""
    now = now if now is not None else time.time()
    order = [primary] if primary is not None else []
    for uid, sess in _candidates.get(cid) or []:
        if sess is not primary and _healthy(uid, sess, now):
            order.append(sess)
    return order


async def scan_with_failover(cid: str, primary, fetch: Callable[..., Awaitable]):
    """fetch(session) with the course's scanner, handing over to the next
    member when it raises. Returns (result, session that succeeded); re-raises
    the last error when every member failed."""
    global failover_count
    last_error: Optional[BaseException] = None
    for sess in failover_order(cid, primary):
        try:
            result = await fetch(sess)
        except Exception as e:
            last_error = e
            report_failure(sess)
            logging.warning("Scanner %s failed for course %s (%s), trying the next member",
                            _uid_of(sess), cid, e)
            continue
        report_success(sess)
        if sess is not primary:
            failover_count += 1
            logging.info("Course %s scanned by member %s after a failover", cid, _uid_of(sess))
        return result, sess
    if last_error is None:
        raise ScanFailed(f"no session can scan course {cid}")
    raise last_error


def content_hash(items) -> str:
    """Stable short hash of JSON-able scan content."""
    blob = json.dumps(items, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def record_scan(cid: str, session, *, file_hash: Optional[str] = None, forum_hash: Optional[str] = None) -> None:
    try:
        db.mark_course_scanned(cid, _uid_of(session), file_hash=file_hash, forum_hash=forum_hash)
    except Exception:
        logging.debug("Recording the scan of %s failed", cid, exc_info=True)


def _semester_label() -> Optional[str]:
    try:
        return load_default_semester().get("label")
    except Exception:
        logging.debug("Default semester unavailable", exc_info=True)
        return None


def sync_registry(titles: dict, members: dict, known_uids) -> dict:
    """Write the watch plan into the registry: every course into
    global_courses, and the followed courses of each user whose course list is
    known into user_courses. Returns the counters for the digest."""
    semester = _semester_label()
    for cid, title in titles.items():
        db.upsert_global_course(cid, str(title), semester)
    by_user: dict[int, set] = {uid: set() for uid in known_uids}
    for cid, uids in members.items():
        for uid in uids:
            if uid in by_user:
                by_user[uid].add(cid)
    for uid, cids in by_user.items():
        db.get_user_scope(uid).sync_courses(cids)
    pairs = sum(len(u) for u in members.values())
    last_stats.clear()
    last_stats.update({"courses": len(titles), "pairs": pairs, "ts": time.time()})
    return dict(last_stats)


def savings_line(stats: Optional[dict] = None) -> Optional[str]:
    """One digest line: course scans per cycle against one scan per follower."""
    stats = stats if stats is not None else last_stats
    courses, pairs = stats.get("courses") or 0, stats.get("pairs") or 0
    if not courses or not pairs:
        return None
    saved = max(0, pairs - courses)
    pct = round(100 * saved / pairs) if pairs else 0
    line = (f"🧭 Course registry: {courses} course scans per cycle for {pairs} follower pairs "
            f"({saved} saved, {pct}%)")
    if failover_count:
        line += f", {failover_count} scanner failovers"
    return line


def reset_digest_counters() -> None:
    global failover_count
    failover_count = 0


# ── fan-out ───────────────────────────────────────────────────────────────────

def delivery_format(uid: int, cid: Optional[str], kind: str) -> Optional[str]:
    """How this user gets an alert of `kind` for the course: "full",
    "title" or "silent", or None to skip it. Users that only track the course
    (no registry row) get the default."""
    if cid is None:
        return DEFAULT_FORMAT
    try:
        prefs = db.get_user_scope(uid).course_prefs(cid)
    except Exception:
        logging.debug("Course preferences unavailable", exc_info=True)
        prefs = None
    if prefs is None:
        return DEFAULT_FORMAT
    if prefs["is_muted"]:
        return None
    column = KIND_COLUMN.get(kind)
    if column and not prefs.get(column, True):
        return None
    fmt = prefs.get("format") or DEFAULT_FORMAT
    return fmt if fmt in FORMAT_LABELS else DEFAULT_FORMAT


ALERT_QUEUE_KIND = "alert_queue"
QUEUE_BUTTON_LIMIT = 8
QUEUE_MAX_ITEMS = 100                 # a student away for long keeps the newest
QUEUE_MAX_AGE_SECONDS = 3 * 86400     # older held alerts are dropped, not sent
DIGEST_TEXT_LIMIT = 3800              # below Telegram's 4096, whole lines only


def files_link_markup(cid: Optional[str]) -> Optional[InlineKeyboardMarkup]:
    """"Link only" file delivery: open the course's file area in Stud.IP."""
    if not cid:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔗 Open in Stud.IP", url=f"{BASE_URL}/dispatch.php/course/files?cid={cid}")]])


def _first_button(markup) -> Optional[dict]:
    """The alert's main button (download, link) kept for the digest."""
    try:
        button = markup.inline_keyboard[0][0]
    except (AttributeError, IndexError, TypeError):
        return None
    if getattr(button, "url", None):
        return {"text": button.text, "url": button.url}
    if getattr(button, "callback_data", None):
        return {"text": button.text, "callback_data": button.callback_data}
    return None


def _hold_reason(uid: int, now: datetime) -> Optional[str]:
    """Why an alert waits instead of going out now: "quiet" (the student's
    quiet hours) or "digest" (they get one evening digest), else None."""
    if in_quiet_hours(uid, now):
        return "quiet"
    if get_pref(uid, "delivery_mode") == "digest":
        return "digest"
    return None


def queue_alert(uid: int, kind: str, cid: Optional[str], title_text: str, markup, reason: str,
                now: Optional[datetime] = None) -> None:
    scope = db.get_user_scope(uid)
    now = now or datetime.now(TZ_BERLIN)
    queue = list(scope.private(ALERT_QUEUE_KIND) or [])
    queue.append({"kind": kind, "cid": cid, "title": title_text, "button": _first_button(markup),
                  "reason": reason, "ts": now.timestamp()})
    scope.save_private(ALERT_QUEUE_KIND, _prune_queue(queue, now))


def _prune_queue(queue: list, now: datetime) -> list:
    """Drop held alerts older than QUEUE_MAX_AGE_SECONDS and keep at most
    QUEUE_MAX_ITEMS (the newest)."""
    cutoff = now.timestamp() - QUEUE_MAX_AGE_SECONDS
    fresh = [item for item in queue if not isinstance(item.get("ts"), (int, float)) or item["ts"] >= cutoff]
    return fresh[-QUEUE_MAX_ITEMS:]


async def fan_out(bot, cid: Optional[str], kind: str, text: str, title_text: str, *,
                  reply_markup=None, members=None, parse_mode: str = "HTML",
                  send_full: Optional[Callable[..., Awaitable[bool]]] = None,
                  now: Optional[datetime] = None) -> dict:
    """Deliver one alert to the course's followers in their own way.

    Per course: muted, kind switched off, or the format (full card with
    sound, title line, silent card). Per student: during their quiet hours
    or in evening digest mode the alert is queued (see flush_alert_queues);
    for new files, their file delivery picks the file itself (send_full,
    which returns False to fall back to the card), the card with its
    download button, or a Stud.IP link only.
    Returns {"full"|"title"|"silent"|"skipped"|"queued": count}."""
    counts = {"full": 0, "title": 0, "silent": 0, "skipped": 0, "queued": 0}
    now = now or datetime.now(TZ_BERLIN)
    for uid in sorted(broadcast_targets(cid, members)):
        fmt = delivery_format(uid, cid, kind)
        if fmt is None:
            counts["skipped"] += 1
            continue
        files_mode = get_pref(uid, "file_delivery") if kind == "files" else "card"
        markup = files_link_markup(cid) if files_mode == "link" else reply_markup
        try:
            reason = _hold_reason(uid, now)
            if reason is not None:
                queue_alert(uid, kind, cid, title_text, markup, reason, now)
                counts["queued"] += 1
                continue
            if files_mode == "upload" and send_full is not None and await send_full(bot, uid):
                counts[fmt] += 1
                await asyncio.sleep(0.3)
                continue
            await bot.send_message(
                chat_id=uid,
                text=title_text if fmt == "title" else text,
                parse_mode=parse_mode,
                disable_notification=fmt == "silent",
                reply_markup=markup,
            )
            counts[fmt] += 1
            await asyncio.sleep(0.3)  # Telegram rate limit safety
        except Exception as e:
            logging.warning("Failed to deliver a %s alert to %s: %s", kind, uid, e)
    return counts


def _queue_due(uid: int, item: dict, now: datetime, with_morning: bool = False) -> bool:
    """A held alert goes out once the reason is over: right after the
    student's morning summary (with_morning, even inside quiet hours: they
    are up), else when quiet hours ended and the morning summary time passed
    (or quiet end without one); digest items at DIGEST_HOUR, never inside
    quiet hours."""
    local = now.astimezone(TZ_BERLIN)
    is_digest = item.get("reason") == "digest" and get_pref(uid, "delivery_mode") == "digest"
    if with_morning and not is_digest:
        return True
    if in_quiet_hours(uid, now):
        return False
    if is_digest:
        return local.hour >= DIGEST_HOUR
    morning = morning_send_time(uid)
    return morning is None or local.time() >= morning or local.hour >= DIGEST_HOUR


def digest_text(items: list) -> str:
    """The digest message for `items` (see digest_batch for the size limit)."""
    kinds = {"files": "📄", "announcements": "📢", "forum": "💬"}
    lines = [f"🗞️ <b>Course updates</b> ({len(items)})", ""]
    for item in items:
        lines.append(item.get("title") or f"{kinds.get(item.get('kind'), '•')} update")
    return "\n".join(lines)


def digest_batch(items: list) -> list:
    """The leading items whose digest fits DIGEST_TEXT_LIMIT. Whole lines
    only: cutting the text could split an HTML tag and Telegram would reject
    the message. The rest stays queued for the next digest."""
    batch: list = []
    for item in items:
        if batch and len(digest_text(batch + [item])) > DIGEST_TEXT_LIMIT:
            break
        batch.append(item)
    return batch


def _plain(text: str) -> str:
    """HTML digest text as plain text (fallback when Telegram rejects it)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def digest_markup(items: list) -> Optional[InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        button = item.get("button")
        if not button or len(rows) >= QUEUE_BUTTON_LIMIT:
            continue
        if button.get("url"):
            rows.append([InlineKeyboardButton(button["text"], url=button["url"])])
        elif button.get("callback_data"):
            rows.append([InlineKeyboardButton(button["text"], callback_data=button["callback_data"])])
    return InlineKeyboardMarkup(rows) if rows else None


async def flush_alert_queues(bot, now: Optional[datetime] = None, user_ids=None,
                             with_morning: bool = False) -> int:
    """Send each student's due held alerts as one digest message; returns
    how many students got one. `user_ids` limits it to those students and
    `with_morning` sends their quiet-hours alerts right after the morning
    summary. Paused students (not ACTIVE) keep their queue until /start."""
    from telegram.error import BadRequest

    now = now or datetime.now(TZ_BERLIN)
    sent = 0
    owners = db.private_owner_ids(ALERT_QUEUE_KIND)
    for uid in owners if user_ids is None else [u for u in owners if u in set(user_ids)]:
        if lifecycle_state(uid) != ACTIVE:
            continue
        scope = db.get_user_scope(uid)
        stored = list(scope.private(ALERT_QUEUE_KIND) or [])
        queue = _prune_queue(stored, now)
        if queue != stored:
            scope.save_private(ALERT_QUEUE_KIND, queue)
        batch = digest_batch([item for item in queue if _queue_due(uid, item, now, with_morning)])
        if not batch:
            continue
        text, markup = digest_text(batch), digest_markup(batch)
        try:
            try:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML",
                                       disable_notification=False, reply_markup=markup)
            except BadRequest as e:
                # a title Telegram can't parse must not hold the queue forever
                logging.warning("Course digest for %s rejected (%s); sending it as plain text", uid, e)
                await bot.send_message(chat_id=uid, text=_plain(text), disable_notification=False,
                                       reply_markup=markup)
        except Exception as e:
            logging.warning("Course digest for %s failed: %s", uid, e)
            continue
        scope.save_private(ALERT_QUEUE_KIND, [item for item in queue if item not in batch])
        sent += 1
        await asyncio.sleep(0.3)  # Telegram rate limit safety between students
    return sent


def course_title(cid: str) -> str:
    row = None
    try:
        row = db.get_global_course(cid)
    except Exception:
        logging.debug("Registry lookup failed", exc_info=True)
    if row and row.get("title"):
        return row["title"]
    for courses in (state.user_courses_map or {}).values():
        if cid in courses:
            return courses[cid]
    return state.courses_map.get(cid, cid)


# ── per-course alert settings (calert callbacks) ─────────────────────────────
_TOGGLES = {
    "m": ("is_muted", "Muted"),
    "f": ("notify_files", "Files"),
    "a": ("notify_announcements", "Announcements"),
    "p": ("notify_forum", "Forum"),
}


def _next_format(fmt: str) -> str:
    order = list(FORMAT_LABELS)
    return order[(order.index(fmt) + 1) % len(order)] if fmt in order else DEFAULT_FORMAT


def _course_summary(prefs: dict) -> str:
    if prefs["is_muted"]:
        return "🔇"
    kinds = "".join(icon for icon, col in (("📄", "notify_files"), ("📢", "notify_announcements"),
                                           ("💬", "notify_forum")) if prefs[col])
    return f"{FORMAT_LABELS.get(prefs['format'], '')[:1]}{kinds or '∅'}"


def course_alerts_list_view(user_id: int) -> tuple:
    cids = db.get_user_scope(user_id).course_ids()
    if not cids:
        return ("🔔 <b>Course alerts</b>\n\nNo followed courses yet. They appear after the next "
                "watcher cycle once you are signed in.", None)
    rows = []
    for cid in sorted(cids, key=lambda c: course_title(c).lower()):
        prefs = db.get_user_scope(user_id).course_prefs(cid) or {}
        label = f"{_course_summary(prefs)} {course_title(cid)}" if prefs else course_title(cid)
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"calert|c|{cid}")])
    text = ("🔔 <b>Course alerts</b>\n\nEach course is checked once for everyone who takes it; "
            "here you choose how its alerts reach you.\n"
            "🔔 full card with sound · 📝 title only · 🔕 silent card · 🔇 muted\n"
            "📄 files · 📢 announcements · 💬 forum")
    return text, InlineKeyboardMarkup(rows)


def course_alerts_course_view(user_id: int, cid: str) -> tuple:
    prefs = db.get_user_scope(user_id).course_prefs(cid)
    if prefs is None:
        return course_alerts_list_view(user_id)
    def mark(col: str) -> str:
        return "✅" if prefs[col] else "⬜"
    rows = [
        [InlineKeyboardButton(("🔇 Muted, tap to unmute" if prefs["is_muted"] else "🔔 Mute this course"),
                              callback_data=f"calert|t|m|{cid}")],
        [InlineKeyboardButton(f"{mark('notify_files')} Files", callback_data=f"calert|t|f|{cid}"),
         InlineKeyboardButton(f"{mark('notify_announcements')} Announcements", callback_data=f"calert|t|a|{cid}"),
         InlineKeyboardButton(f"{mark('notify_forum')} Forum", callback_data=f"calert|t|p|{cid}")],
        [InlineKeyboardButton(f"Format: {FORMAT_LABELS[prefs['format']]}", callback_data=f"calert|fmt|{cid}")],
        [InlineKeyboardButton("⬅️ All courses", callback_data="calert")],
    ]
    fmt_help = {
        "full": "the whole alert with sound",
        "title": "one line with the course and the title, with sound",
        "silent": "the whole alert without sound",
    }[prefs["format"]]
    row = db.get_global_course(cid) or {}
    scanned = row.get("last_scanned_at")
    scanned_line = (f"\n🕒 Last checked {time.strftime('%d.%m %H:%M', time.localtime(scanned))}"
                    if scanned else "")
    followers = len(db.course_followers(cid))
    text = (f"🔔 <b>{html.escape(course_title(cid))}</b>\n\n"
            f"Format: {FORMAT_LABELS[prefs['format']]} ({fmt_help})\n"
            f"👥 {followers} follower(s), one check for all{scanned_line}")
    return text, InlineKeyboardMarkup(rows)


async def handle_course_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """calert, calert|c|<cid>, calert|t|<m|f|a|p>|<cid>, calert|fmt|<cid>."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return
    parts = (query.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""
    cid = parts[-1] if action in ("c", "t", "fmt") else None
    if action == "t" and len(parts) == 4 and parts[2] in _TOGGLES and cid:
        prefs = db.get_user_scope(user_id).course_prefs(cid)
        if prefs is not None:
            column = _TOGGLES[parts[2]][0]
            db.get_user_scope(user_id).set_course_pref(cid, column, not prefs[column])
    elif action == "fmt" and cid:
        prefs = db.get_user_scope(user_id).course_prefs(cid)
        if prefs is not None:
            db.get_user_scope(user_id).set_course_pref(cid, "format", _next_format(prefs["format"]))
    await query.answer()
    text, markup = course_alerts_course_view(user_id, cid) if cid else course_alerts_list_view(user_id)
    if action:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
