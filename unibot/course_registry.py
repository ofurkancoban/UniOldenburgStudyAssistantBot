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
import math
import re
import time
import weakref
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN
from unibot import db, state
from unibot.auth import ACTIVE, has_stored_login, is_user_allowed, lifecycle_state
from unibot.formatting import TELEGRAM_HTML_LIMIT, _telegram_length, truncate_html
from unibot.preferences import DIGEST_HOUR, get_pref, in_quiet_hours, morning_send_time
from unibot.studip_api import BASE_URL
from unibot.storage import has_course_access
from unibot.telegram_ui import WA_FORWARD_CALLBACK_PREFIX, broadcast_targets, markup_for

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


def _session_active(uid: Optional[int]) -> bool:
    """Whether a scanner still belongs to an account allowed to do background work."""
    return uid is None or (is_user_allowed(uid) and lifecycle_state(uid) == ACTIVE)


def _candidate_uid(cid: str, session) -> Optional[int]:
    return next((uid for uid, candidate in _candidates.get(cid, []) if candidate is session), _uid_of(session))


def _healthy(uid: Optional[int], session, now: float) -> bool:
    if not _session_active(uid) or not has_stored_login(session):
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


def forget_user(user_id: int) -> None:
    """Remove a deleted student's session from every in-memory scan plan."""
    uid = int(user_id)
    for cid, candidates in list(_candidates.items()):
        remaining = [(candidate_uid, sess) for candidate_uid, sess in candidates
                     if candidate_uid != uid]
        if remaining:
            _candidates[cid] = remaining
        else:
            _candidates.pop(cid, None)
            _rotation.pop(cid, None)
    _failed_at.pop(uid, None)


def pick_scanner(cid: str, now: Optional[float] = None):
    """The next healthy member session for the course (round-robin), any
    member session when none is healthy, None when the course has none."""
    cands = _candidates.get(cid) or []
    eligible = [c for c in cands if _session_active(c[0])]
    if not eligible:
        return None
    now = now if now is not None else time.time()
    healthy = [c for c in eligible if _healthy(c[0], c[1], now)]
    pool = healthy or eligible
    turn = _rotation.get(cid, 0)
    _rotation[cid] = turn + 1
    return pool[turn % len(pool)][1]


def failover_order(cid: str, primary, now: Optional[float] = None) -> list:
    """The primary session first, then the course's other healthy members."""
    now = now if now is not None else time.time()
    order = [primary] if primary is not None and _session_active(_candidate_uid(cid, primary)) else []
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
        if not _session_active(_candidate_uid(cid, sess)):
            continue
        try:
            result = await fetch(sess)
        except Exception as e:
            if getattr(e, "quiet", False):
                # A host-wide circuit breaker is unrelated to this student's
                # credentials; another session would hit the same cooldown.
                raise
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


def sync_registry(titles: dict, members: dict, known_uids) -> dict:
    """Write the watch plan into the registry: every course into
    global_courses, and the followed courses of each user whose course list is
    known into user_courses. Returns the counters for the digest."""
    for cid, title in titles.items():
        db.upsert_global_course(cid, str(title))  # no semester: the only default known here is one student's
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


def _first_button(markup, allow_wa: bool = False) -> Optional[dict]:
    """The alert's main button (download, link) kept for the digest."""
    try:
        button = markup.inline_keyboard[0][0]
    except (AttributeError, IndexError, TypeError):
        return None
    if getattr(button, "url", None):
        return {"text": button.text, "url": button.url}
    data = getattr(button, "callback_data", None)
    if data and (allow_wa or not str(data).startswith(WA_FORWARD_CALLBACK_PREFIX)):
        # "Forward to WA" would forward the whole digest, so it's not kept
        return {"text": button.text, "callback_data": data}
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
                now: Optional[datetime] = None, *, urgent: bool = False,
                full_text: Optional[str] = None) -> None:
    scope = db.get_user_scope(uid)
    now = now or datetime.now(TZ_BERLIN)
    stored = scope.private(ALERT_QUEUE_KIND)
    queue = stored if isinstance(stored, list) else []
    item = {"kind": kind, "cid": cid, "title": title_text,
            "button": _first_button(markup, allow_wa=urgent),
            "reason": reason, "ts": now.timestamp()}
    if urgent and isinstance(full_text, str):
        # Urgent timetable changes may wait through quiet hours, but must keep
        # their full card and sound when they are eventually delivered.
        item.update({"urgent": True, "full_text": truncate_html(full_text)})
    queue.append(item)
    scope.save_private(ALERT_QUEUE_KIND, _prune_queue(queue, now))


def _prune_queue(queue: list, now: datetime) -> list:
    """Drop held alerts older than QUEUE_MAX_AGE_SECONDS and keep at most
    QUEUE_MAX_ITEMS (the newest)."""
    if not isinstance(queue, list):
        return []
    cutoff = now.timestamp() - QUEUE_MAX_AGE_SECONDS
    fresh = []
    for item in queue:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            continue
        item = {**item, "title": truncate_html(item["title"], DIGEST_TEXT_LIMIT - 100)}
        if item.get("urgent") is True:
            if isinstance(item.get("full_text"), str):
                item["full_text"] = truncate_html(item["full_text"])
            else:
                # Malformed/legacy urgent rows cannot be sent as full cards.
                item.pop("urgent", None)
                item.pop("full_text", None)
        else:
            item.pop("urgent", None)
            item.pop("full_text", None)
        button = item.get("button")
        if button is not None:
            if not isinstance(button, dict) or not isinstance(button.get("text"), str) or not button.get("text"):
                item["button"] = None
            elif isinstance(button.get("url"), str) and button["url"]:
                item["button"] = {"text": button["text"], "url": button["url"]}
            elif isinstance(button.get("callback_data"), str) and button["callback_data"] \
                    and len(button["callback_data"].encode("utf-8")) <= 64:
                item["button"] = {"text": button["text"], "callback_data": button["callback_data"]}
            else:
                item["button"] = None
        stamp = item.get("ts")
        if isinstance(stamp, bool):
            continue
        if isinstance(stamp, int):
            if stamp >= cutoff:
                fresh.append(item)
        elif isinstance(stamp, float):
            if math.isfinite(stamp) and stamp >= cutoff:
                fresh.append(item)
        else:
            fresh.append(item)  # legacy records without a timestamp remain deliverable
    return fresh[-QUEUE_MAX_ITEMS:]


async def fan_out(bot, cid: Optional[str], kind: str, text: str, title_text: str, *,
                  reply_markup=None, members=None, parse_mode: str = "HTML",
                  send_full: Optional[Callable[..., Awaitable[bool]]] = None,
                  now: Optional[datetime] = None, urgent: bool = False,
                  recipients=None) -> dict:
    """Deliver one alert to the course's followers in their own way.

    Per course: muted, kind switched off, or the format (full card with
    sound, title line, silent card). Per student: during their quiet hours
    or in evening digest mode the alert is queued (see flush_alert_queues);
    for new files, their file delivery picks the file itself (send_full,
    which returns False to fall back to the card), the card with its
    download button, or a Stud.IP link only.
    An urgent alert (a session soon cancelled or moved) still respects a
    muted course and quiet hours, but always comes as a full card with sound
    and skips the evening digest.
    `recipients` (a set of students) replaces the course's usual audience.
    Returns {"full"|"title"|"silent"|"skipped"|"queued": count}."""
    counts = {"full": 0, "title": 0, "silent": 0, "skipped": 0, "queued": 0}
    if kind in KIND_COLUMN and not cid:
        logging.error("Refused %s alert without a course ID", kind)
        return counts
    now = now or datetime.now(TZ_BERLIN)
    # last line of defence against "message is too long" for every alert kind
    if parse_mode == "HTML":
        text, title_text = truncate_html(text), truncate_html(title_text)
    else:
        text, title_text = text[:TELEGRAM_HTML_LIMIT], title_text[:TELEGRAM_HTML_LIMIT]
    for uid in sorted(recipients if recipients is not None else broadcast_targets(cid, members)):
        with db.private_write_context(uid):
            if not db.private_context_is_current(uid):
                counts["skipped"] += 1
                continue
            if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                    or (cid is not None and not has_course_access(uid, cid))):
                counts["skipped"] += 1
                continue
            fmt = delivery_format(uid, cid, kind)
            if fmt is None:
                counts["skipped"] += 1
                continue
            if urgent:
                fmt = "full"
            files_mode = get_pref(uid, "file_delivery") if kind == "files" else "card"
            markup = markup_for(uid, files_link_markup(cid) if files_mode == "link" else reply_markup)
            try:
                reason = _hold_reason(uid, now)
                if urgent and reason == "digest":
                    reason = None
                if reason is not None:
                    queue_alert(uid, kind, cid, title_text, markup, reason, now,
                                urgent=urgent, full_text=text if urgent else None)
                    counts["queued"] += 1
                    continue
                # "title only" for this course means just a line, whatever the file delivery
                if files_mode == "upload" and fmt != "title" and send_full is not None:
                    sent_full = await send_full(bot, uid)
                    if not db.private_context_is_current(uid):
                        counts["skipped"] += 1
                        continue
                    if sent_full:
                        counts[fmt] += 1
                        await asyncio.sleep(0.3)
                        continue
                    # The file may have taken long enough to download that the
                    # recipient was blocked while it was in flight. Do not fall
                    # back to a smaller alert after that access change.
                    if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                            or (cid is not None and not has_course_access(uid, cid))):
                        counts["skipped"] += 1
                        continue
                body = title_text if fmt == "title" else text
                try:
                    await bot.send_message(chat_id=uid, text=body, parse_mode=parse_mode,
                                           disable_notification=fmt == "silent", reply_markup=markup)
                except BadRequest as e:
                    if parse_mode != "HTML" or "parse" not in str(e).lower():
                        raise
                    # markup Telegram can't parse must not cost the student the alert
                    logging.warning("A %s alert for %s was rejected (%s); sending it as plain text", kind, uid, e)
                    if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                            or (cid is not None and not has_course_access(uid, cid))):
                        counts["skipped"] += 1
                        continue
                    if not db.private_context_is_current(uid):
                        counts["skipped"] += 1
                        continue
                    await bot.send_message(chat_id=uid, text=_plain(body), disable_notification=fmt == "silent",
                                           reply_markup=markup)
                if not db.private_context_is_current(uid):
                    counts["skipped"] += 1
                    continue
                counts[fmt] += 1
                await asyncio.sleep(0.3)  # Telegram rate limit safety
            except Exception as e:
                logging.warning("Failed to deliver a %s alert to %s: %s", kind, uid, e)
                if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                        or not db.private_context_is_current(uid)):
                    counts["skipped"] += 1
                    continue
                try:
                    queue_alert(uid, kind, cid, title_text, markup, "retry", now,
                                urgent=urgent, full_text=text if urgent else None)
                    counts["queued"] += 1
                except Exception:
                    logging.error("Could not queue failed %s alert for %s", kind, uid, exc_info=True)
    return counts


def _queue_due(uid: int, item: dict, now: datetime, with_morning: bool = False) -> bool:
    """A held alert goes out once the reason is over: right after the
    student's morning summary (with_morning, even inside quiet hours: they
    are up), else when quiet hours ended and the morning summary time passed
    (or quiet end without one); digest items at DIGEST_HOUR, never inside
    quiet hours."""
    local = now.astimezone(TZ_BERLIN)
    is_digest = item.get("reason") == "digest" and get_pref(uid, "delivery_mode") == "digest"
    if item.get("reason") == "retry":
        return not in_quiet_hours(uid, now)
    if with_morning and not is_digest:
        return True
    if in_quiet_hours(uid, now):
        return False
    if is_digest:
        # once the first DIGEST_HOUR after it was queued has come: an alert
        # from 20:00 waits for tomorrow's digest instead of going out alone
        # minutes later (and a digest missed while the bot was down follows)
        last_digest = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if local < last_digest:
            last_digest -= timedelta(days=1)
        queued = item.get("ts")
        return not isinstance(queued, (int, float)) or queued < last_digest.timestamp()
    morning = morning_send_time(uid)
    return morning is None or local.time() >= morning or local.hour >= DIGEST_HOUR


def digest_text(items: list) -> str:
    """The digest message for `items` (see digest_batch for the size limit)."""
    kinds = {"files": "📄", "announcements": "📢", "forum": "💬", "schedule": "📅"}
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
        if batch and _telegram_length(digest_text(batch + [item])) > DIGEST_TEXT_LIMIT:
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


_alert_delivery_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _queued_course_allowed(uid: int, item: dict) -> bool:
    return ((item.get("cid") is None and item.get("kind") not in KIND_COLUMN)
            or has_course_access(uid, item.get("cid")))


async def flush_alert_queues(bot, now: Optional[datetime] = None, user_ids=None,
                             with_morning: bool = False) -> int:
    """Serialize each student's queue delivery across watcher and morning jobs."""
    locks = _alert_delivery_locks.setdefault(asyncio.get_running_loop(), {})
    selected = None if user_ids is None else set(user_ids)
    sent = 0
    for uid in db.private_owner_ids(ALERT_QUEUE_KIND):
        if selected is not None and uid not in selected:
            continue
        async with locks.setdefault(uid, asyncio.Lock()):
            sent += await _flush_alert_queues_unlocked(bot, now, [uid], with_morning)
    return sent


async def _flush_alert_queues_unlocked(bot, now: Optional[datetime] = None, user_ids=None,
                             with_morning: bool = False) -> int:
    """Send each student's due held alerts as one digest message; returns
    how many students got one. `user_ids` limits it to those students and
    `with_morning` sends their quiet-hours alerts right after the morning
    summary. Paused students (not ACTIVE) keep their queue until /start."""
    now = now or datetime.now(TZ_BERLIN)
    sent = 0
    owners = db.private_owner_ids(ALERT_QUEUE_KIND)
    selected_uids = None if user_ids is None else set(user_ids)
    for uid in owners if selected_uids is None else [u for u in owners if u in selected_uids]:
        # Keep the queue snapshot, its delivery, and follow-up writes bound to
        # one deletion generation. A user who restarts soon after deleting
        # their data must not receive alerts from an older queue snapshot.
        with db.private_write_context(uid):
            if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                    or lifecycle_state(uid) != ACTIVE):
                continue
            scope = db.get_user_scope(uid)
            raw = scope.private(ALERT_QUEUE_KIND)
            stored = raw if isinstance(raw, list) else []
            queue = [item for item in _prune_queue(stored, now)
                     if _queued_course_allowed(uid, item)]
            if queue != stored and db.private_context_is_current(uid):
                scope.save_private(ALERT_QUEUE_KIND, queue)
            due = [item for item in queue if _queue_due(uid, item, now, with_morning)]
            urgent_items = [item for item in due if item.get("urgent") is True and item.get("full_text")]
            batch = digest_batch([item for item in due if item not in urgent_items])
            if not urgent_items and not batch:
                continue
            if not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE:
                continue

            def remove_sent(scope, sent_items: list, now: datetime) -> None:
                # Re-read after sending so alerts appended during a Telegram await
                # are not overwritten by this worker's stale queue snapshot.
                current_raw = scope.private(ALERT_QUEUE_KIND)
                current = _prune_queue(current_raw if isinstance(current_raw, list) else [], now)
                remaining = list(current)
                for sent_item in sent_items:
                    try:
                        remaining.remove(sent_item)
                    except ValueError:
                        pass  # another worker already removed this exact entry
                scope.save_private(ALERT_QUEUE_KIND, remaining)

            student_sent = False
            # Urgent timetable changes are separate full cards with sound, rather
            # than title lines folded into the ordinary digest.
            for item in urgent_items:
                if not _queued_course_allowed(uid, item):
                    continue
                if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                        or lifecycle_state(uid) != ACTIVE):
                    break
                try:
                    await bot.send_message(chat_id=uid, text=item["full_text"], parse_mode="HTML",
                                           disable_notification=False, reply_markup=digest_markup([item]))
                    if not db.private_context_is_current(uid):
                        break
                    remove_sent(scope, [item], now)
                except BadRequest as e:
                    if "parse" not in str(e).lower():
                        logging.warning("Urgent course alert for %s failed: %s", uid, e)
                        continue
                    logging.warning("Urgent course alert for %s rejected (%s); sending it as plain text", uid, e)
                    if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                            or lifecycle_state(uid) != ACTIVE):
                        break
                    try:
                        await bot.send_message(chat_id=uid, text=_plain(item["full_text"]),
                                               disable_notification=False, reply_markup=digest_markup([item]))
                        if not db.private_context_is_current(uid):
                            break
                        remove_sent(scope, [item], now)
                    except Exception as fallback_error:
                        logging.warning("Urgent course alert fallback for %s failed: %s", uid, fallback_error)
                        continue
                except Exception as e:
                    logging.warning("Urgent course alert for %s failed: %s", uid, e)
                    continue
                student_sent = True
                await asyncio.sleep(0.3)

            batch = [item for item in batch if _queued_course_allowed(uid, item)]
            if (batch and db.private_context_is_current(uid) and is_user_allowed(uid)
                    and lifecycle_state(uid) == ACTIVE):
                text, markup = digest_text(batch), digest_markup(batch)
                try:
                    try:
                        await bot.send_message(chat_id=uid, text=text, parse_mode="HTML",
                                               disable_notification=False, reply_markup=markup)
                        if not db.private_context_is_current(uid):
                            continue
                        remove_sent(scope, batch, now)
                    except BadRequest as e:
                        # a title Telegram can't parse must not hold the queue forever
                        logging.warning("Course digest for %s rejected (%s); sending it as plain text", uid, e)
                        if (not db.private_context_is_current(uid) or not is_user_allowed(uid)
                                or lifecycle_state(uid) != ACTIVE):
                            continue
                        await bot.send_message(chat_id=uid, text=_plain(text), disable_notification=False,
                                               reply_markup=markup)
                        if not db.private_context_is_current(uid):
                            continue
                        remove_sent(scope, batch, now)
                except Exception as e:
                    logging.warning("Course digest for %s failed: %s", uid, e)
                    continue
                student_sent = True
            if student_sent:
                sent += 1
                await asyncio.sleep(0.3)  # Telegram rate safety between students
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
    scanned_line = (f"\n🕒 Last checked {datetime.fromtimestamp(scanned, TZ_BERLIN):%d.%m %H:%M}"
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
    saved = True
    if action == "t" and len(parts) == 4 and parts[2] in _TOGGLES and cid:
        prefs = db.get_user_scope(user_id).course_prefs(cid)
        if prefs is not None:
            column = _TOGGLES[parts[2]][0]
            saved = db.get_user_scope(user_id).set_course_pref(cid, column, not prefs[column])
    elif action == "fmt" and cid:
        prefs = db.get_user_scope(user_id).course_prefs(cid)
        if prefs is not None:
            saved = db.get_user_scope(user_id).set_course_pref(
                cid, "format", _next_format(prefs["format"])
            )
    if not saved:
        await query.answer("This course setting could not be saved. Please reopen the course list.",
                           show_alert=True)
        return
    await query.answer()
    text, markup = course_alerts_course_view(user_id, cid) if cid else course_alerts_list_view(user_id)
    if action:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except BadRequest as e:  # the same course opened again: nothing changed
            if "not modified" not in str(e).lower():
                raise
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
