"""JSON cache files, stored settings and preferences, shared HTTP session, job locks.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import contextlib
import functools
import hmac
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from studip_utils import TZ_BERLIN, berlin_now
from unibot import db, state
from unibot.config import PRIMARY_ADMIN_ID

# navigation & cache
LINK_CACHE_PATH = "link_cache.json"


class _PersistentLinkCache(dict):
    """dict that writes itself to LINK_CACHE_PATH shortly after any change.

    Inline buttons (file downloads, folders) only carry a short id; keeping
    the id -> target map on disk means buttons in older messages still work
    after a restart. Writes are debounced, so rendering a folder with dozens
    of buttons costs one write, not dozens."""

    FLUSH_DELAY_SECONDS = 2.0

    def __init__(self, path: str, ttl_seconds: Optional[float] = None, max_items: Optional[int] = None):
        super().__init__()
        self._path = path
        self._ttl = ttl_seconds
        self._max = max_items
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        try:
            data = db.load(path)
            if isinstance(data, dict):
                super().update(data)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logging.warning("Could not load %s: %s", path, exc)

    def _changed(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._flush_handle is None:
            self._flush_handle = loop.call_later(self.FLUSH_DELAY_SECONDS, self.flush)

    def _prune(self) -> None:
        """Drop entries past the TTL and the oldest beyond max_items."""
        if self._ttl is None and self._max is None:
            return
        now = time.time()

        def ts(key):
            value = dict.get(self, key)
            return value.get("ts", now) if isinstance(value, dict) else now

        if self._ttl is not None:
            for key in [k for k in self if now - ts(k) > self._ttl]:
                super().pop(key, None)
        if self._max is not None and len(self) > self._max:
            for key in sorted(self, key=ts)[: len(self) - self._max]:
                super().pop(key, None)

    def flush(self) -> None:
        self._flush_handle = None
        self._prune()
        try:
            db.save(self._path, dict(self), indent=None)
        except Exception as exc:
            logging.warning("Could not save %s: %s", self._path, exc)

    def __setitem__(self, key, value):
        if isinstance(value, dict) and "ts" not in value:
            value = {**value, "ts": time.time()}
        super().__setitem__(key, value)
        self._changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._changed()

    def pop(self, key, *default):
        had = key in self
        result = super().pop(key, *default)
        if had:
            self._changed()
        return result


link_cache = _PersistentLinkCache(LINK_CACHE_PATH)  # short_id -> { url, cid, name?, action?, user_id?, current_url?, ts }

# Buttons on course alerts (shared course content, no personal data) keep
# working after a restart; entries go after CALLBACK_TTL_SECONDS.
CALLBACK_TTL_SECONDS = 14 * 86400
CALLBACK_MAX_ITEMS = 1000


def persistent_callback_cache(path: str) -> "_PersistentLinkCache":
    return _PersistentLinkCache(path, ttl_seconds=CALLBACK_TTL_SECONDS, max_items=CALLBACK_MAX_ITEMS)


# Buttons that act on one student's own data (exam registration actions,
# fired task reminders) are kept in that student's encrypted private record.
USER_CALLBACKS_KIND = "callbacks"
USER_CALLBACK_MAX_ITEMS = 300


def remember_user_callback(user_id: int, key: str, value: dict) -> None:
    try:
        scope = db.get_user_scope(int(user_id))
        data = dict(scope.private(USER_CALLBACKS_KIND) or {})
        now = time.time()
        data[key] = {**value, "ts": now}
        data = {k: v for k, v in data.items() if now - v.get("ts", now) <= CALLBACK_TTL_SECONDS}
        if len(data) > USER_CALLBACK_MAX_ITEMS:
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[-USER_CALLBACK_MAX_ITEMS:])
        scope.save_private(USER_CALLBACKS_KIND, data)
    except Exception:
        logging.warning("Could not keep a button action for user %s", user_id, exc_info=True)


def recall_user_callback(user_id: int, key: str, forget: bool = False) -> Optional[dict]:
    """The student's own stored button action, or None (expired or not theirs)."""
    try:
        scope = db.get_user_scope(int(user_id))
        data = dict(scope.private(USER_CALLBACKS_KIND) or {})
        value = data.get(key)
        if value is None or time.time() - value.get("ts", 0) > CALLBACK_TTL_SECONDS:
            return None
        if forget:
            data.pop(key, None)
            scope.save_private(USER_CALLBACKS_KIND, data)
        return value
    except Exception:
        logging.warning("Could not read a button action for user %s", user_id, exc_info=True)
        return None


# link_cache limits. Entries live long because they're persisted: a stale
# download URL is refreshed on tap anyway (get_fresh_file_url).
LINK_CACHE_TTL_SECONDS = 14 * 86400
LINK_CACHE_MAX_ITEMS = 2000


def cleanup_link_cache():
    """Remove expired entries and cap the cache size."""
    try:
        now_ts = time.time()
        # Expire by TTL
        expired_keys = [k for k, v in link_cache.items() if (now_ts - v.get("ts", now_ts)) > LINK_CACHE_TTL_SECONDS]
        for k in expired_keys:
            link_cache.pop(k, None)
        # Cap size by removing oldest
        if len(link_cache) > LINK_CACHE_MAX_ITEMS:
            # Sort by ts ascending (oldest first)
            sorted_keys = sorted(link_cache.keys(), key=lambda k: link_cache[k].get("ts", 0.0))
            for k in sorted_keys[: max(0, len(link_cache) - LINK_CACHE_MAX_ITEMS)]:
                link_cache.pop(k, None)
    except Exception:
        # Do not allow cleanup to crash the bot
        logging.debug("Link cache pruning failed", exc_info=True)


# ── cache ─────────────────────────────────────────────────────────────────────

FILES_CACHE_PATH = "files_cache.json"
REMINDERS_CACHE_PATH = "reminders_cache.json"
GENERAL_CACHE_PATH = "general_cache.json"


def load_files_cache():
    """Safely load JSON cache of previous file states."""
    if db.exists(FILES_CACHE_PATH):
        try:
            return db.load(FILES_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load file cache: %s", e)
    return {}


def save_files_cache(data):
    """Safely save JSON cache of file states."""
    try:
        db.save(FILES_CACHE_PATH, data)
        logging.info("✅ files_cache.json successfully updated.")
    except Exception as e:
        logging.error("Failed to save file cache: %s", e)


STUDY_PLAN_CACHE_PATH = "study_plan_cache.json"


def load_study_plan_cache() -> dict:
    """Per-course, per-file topic-label cache for the weekly study plan
    feature - {cid: {filename: {"summary", "size", "modified_ts"}}}. Only
    files that were successfully summarized ever get an entry, so a failed
    summarization just means that file is retried cheaply next run rather
    than cached as a placeholder (see collect_course_topics)."""
    if db.exists(STUDY_PLAN_CACHE_PATH):
        try:
            return db.load(STUDY_PLAN_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load study plan cache: %s", e)
    return {}


def save_study_plan_cache(data: dict) -> None:
    try:
        db.save(STUDY_PLAN_CACHE_PATH, data)
    except Exception as e:
        logging.error("Failed to save study plan cache: %s", e)


def load_reminders_cache(user_id: Optional[int] = None):
    path = f"reminders_cache_{user_id}.json" if user_id else REMINDERS_CACHE_PATH
    if db.exists(path):
        try:
            return db.load(path)
        except Exception as e:
            logging.warning("Could not load reminders cache: %s", e)
    return {}


def save_reminders_cache(data, user_id: Optional[int] = None):
    path = f"reminders_cache_{user_id}.json" if user_id else REMINDERS_CACHE_PATH
    try:
        db.save(path, data)
    except Exception as e:
        logging.error("Could not save reminders cache: %s", e)


def load_general_cache():
    if db.exists(GENERAL_CACHE_PATH):
        try:
            return db.load(GENERAL_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load general cache: %s", e)
    return {}


@contextlib.asynccontextmanager
async def shared_http():
    """Shared aiohttp session for third-party HTTP calls (AI providers,
    weather, the local WhatsApp service, Telegram file fetches).

    Opening a new ClientSession per call threw away the connection pool, so
    every request paid a fresh TCP + TLS handshake. Used like a session
    context manager, but leaving the block does not close it. No cookie jar:
    one service's cookies must never be sent to another. Stud.IP requests
    keep using global_session, which owns the login cookies."""
    if state._shared_http_session is None or state._shared_http_session.closed:
        state._shared_http_session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    yield state._shared_http_session


async def close_shared_http() -> None:
    if state._shared_http_session is not None and not state._shared_http_session.closed:
        await state._shared_http_session.close()
    state._shared_http_session = None


_JOB_LOCKS: dict[str, asyncio.Lock] = {}


def single_flight(fn):
    """Run at most one instance of a background job at a time.

    These jobs read a cache file, await network work for a long time and
    write the file back. Started twice at once (the watcher cycle plus a
    button such as "📄 Scan Course Files Now"), the second run would also
    redo all the downloads and its save would overwrite the first one's.
    A later call waits for the running one instead."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        lock = _JOB_LOCKS.setdefault(fn.__name__, asyncio.Lock())
        async with lock:
            return await fn(*args, **kwargs)
    return wrapper


def update_general_cache(updates: dict) -> None:
    """Set only these keys in general_cache.json, on a fresh read.

    For code that awaits (network, Telegram) between reading the cache and
    writing it: saving its whole stale copy would silently undo whatever a
    button handler changed in the meantime (toggles, WhatsApp group, ...).
    The read-modify-write here contains no await, so it can't interleave
    with another coroutine."""
    data = load_general_cache()
    data.update(updates)
    save_general_cache(data)


def save_general_cache(data):
    try:
        db.save(GENERAL_CACHE_PATH, data)
    except Exception as e:
        logging.error("Could not save general cache: %s", e)


TASKS_CACHE_PATH = "tasks_cache.json"


# Tasks are personal: each student's list is a private record reached only
# through their own db.UserScope; there is no "all tasks" list any more.

def user_tasks(user_id) -> list:
    """The student's own tasks ([] without a student)."""
    if user_id is None:
        return []
    return db.get_user_scope(user_id).tasks()


def save_user_tasks(user_id, tasks: list) -> None:
    db.get_user_scope(user_id).save_tasks(tasks)


def add_user_task(user_id, task: dict) -> None:
    db.get_user_scope(user_id).add_task(task)


def remove_user_tasks(user_id, ids) -> list:
    """Drop the student's tasks with these ids; returns the removed ones."""
    return db.get_user_scope(user_id).remove_tasks(ids)


def students_with_tasks() -> list[int]:
    """Ids of the students with tasks, for the reminder job (ids only)."""
    return db.task_owner_ids()


DEFAULT_SEMESTER_CACHE_PATH = "default_semester.json"


def load_default_semester() -> dict:
    """Return {"id": ..., "label": ...} for the semester picked via /status's
    "Set Default Semester" button, or {} if none has been set - in which case
    Files/Browse Courses/Sign Out each fall back to showing their own semester
    picker as before."""
    if db.exists(DEFAULT_SEMESTER_CACHE_PATH):
        try:
            return db.load(DEFAULT_SEMESTER_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load default semester: %s", e)
    return {}


def save_default_semester(semester_id: str, label: str) -> None:
    try:
        db.save(DEFAULT_SEMESTER_CACHE_PATH, {"id": semester_id, "label": label})
    except Exception as e:
        logging.error("Could not save default semester: %s", e)


def clear_default_semester() -> None:
    try:
        db.delete(DEFAULT_SEMESTER_CACHE_PATH)
    except Exception as e:
        logging.error("Could not clear default semester: %s", e)


def get_scan_previous_semester_enabled() -> bool:
    """Whether "📚 Enroll Course" should also include the semester right
    before the one being browsed (a module can still show open enrolment
    for a prior term's course instance, e.g. a retake slot) - off by
    default, toggled via a button on the open-courses view itself (see
    handle_toggle_prev_sem_scan)."""
    return bool(load_general_cache().get("scan_previous_semester"))


def set_scan_previous_semester_enabled(enabled: bool) -> None:
    cache = load_general_cache()
    cache["scan_previous_semester"] = enabled
    save_general_cache(cache)


def get_watch_previous_semester_enabled() -> bool:
    """Whether the background watchers (announcements, forum posts, new
    files) should also monitor the previous semester's courses, not just
    the current one - off by default, toggled via /status (see
    handle_toggle_watch_prev_sem). Distinct from
    get_scan_previous_semester_enabled, which only affects the "➕ Enroll
    Course" browsing list, not what's being actively watched for new
    activity."""
    return bool(load_general_cache().get("watch_previous_semester"))


def set_watch_previous_semester_enabled(enabled: bool) -> None:
    cache = load_general_cache()
    cache["watch_previous_semester"] = enabled
    save_general_cache(cache)


USER_NAMES_CACHE_PATH = "user_names.json"


def load_user_names() -> dict:
    """Return {"<user_id>": "Display Name"} for users, synced automatically
    from Stud.IP via /users/me."""
    if db.exists(USER_NAMES_CACHE_PATH):
        try:
            return db.load(USER_NAMES_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load user names: %s", e)
    return {}


def save_studip_user_name(name: str, user_id: Optional[int] = None) -> None:
    """Save the name Stud.IP reports for a student, unless they told the bot
    what to call them. Without a student it is the watcher account's name
    (the primary admin's), kept as "studip_name"."""
    if not name:
        return
    names = load_user_names()
    owner = user_id if user_id is not None else PRIMARY_ADMIN_ID
    if owner is not None and not names.get(str(owner)):
        names[str(owner)] = name
    if user_id is None:
        names["studip_name"] = name
    try:
        db.save(USER_NAMES_CACHE_PATH, names)
    except Exception as e:
        logging.error("Could not save Stud.IP user name: %s", e)


def get_user_name(user_id=None) -> Optional[str]:
    """The student's own name, or None. Only the primary admin (or no
    student at all) falls back to the watcher account's Stud.IP name, so no
    student ever sees or hears someone else's name."""
    names = load_user_names()
    if user_id is not None:
        own = names.get(str(user_id))
        if own:
            return own
        if user_id != PRIMARY_ADMIN_ID:
            return None
    return get_primary_user_name()


def get_primary_user_name() -> Optional[str]:
    """The watcher account's (primary admin's) name synced from Stud.IP."""
    names = load_user_names()
    return names.get("studip_name") or (names.get(str(PRIMARY_ADMIN_ID)) if PRIMARY_ADMIN_ID else None)


def save_user_name(user_id, name: str) -> None:
    names = load_user_names()
    names[str(user_id)] = name
    try:
        db.save(USER_NAMES_CACHE_PATH, names)
    except Exception as e:
        logging.error("Could not save user name: %s", e)


WARDROBE_CACHE_PATH = "wardrobe.json"  # the old shared document, moved into private records
WARDROBE_KIND = "wardrobe"


def _empty_wardrobe() -> dict:
    return {"items": [], "reference_photo_file_id": None}


def _migrate_shared_wardrobe() -> None:
    """Move the old shared {"<user_id>": entry} document into each owner's
    encrypted private record once (database row only, never a disk file)."""
    try:
        if not db.exists(WARDROBE_CACHE_PATH):
            return
        shared = db.load(WARDROBE_CACHE_PATH) or {}
        if not shared:
            return
    except Exception as e:
        logging.warning("Could not read the shared wardrobe: %s", e)
        return
    for key, entry in shared.items():
        try:
            scope = db.get_user_scope(int(key))
        except ValueError:
            continue
        if not scope.has_private(WARDROBE_KIND):
            scope.save_private(WARDROBE_KIND, entry)
    db.save(WARDROBE_CACHE_PATH, {})
    logging.info("Moved %s wardrobe(s) into private records", len(shared))


def get_wardrobe_entry(user_id) -> dict:
    """The student's own wardrobe: Telegram file_ids and tags, never image
    copies on disk; kept in their encrypted private record, so no other
    student's code path can read it."""
    _migrate_shared_wardrobe()
    entry = db.get_user_scope(user_id).private(WARDROBE_KIND)
    return entry if isinstance(entry, dict) else _empty_wardrobe()


def _save_wardrobe_entry(user_id, entry: dict) -> None:
    try:
        db.get_user_scope(user_id).save_private(WARDROBE_KIND, entry)
    except Exception as e:
        logging.error("Could not save wardrobe: %s", e)


def add_wardrobe_item(user_id, tags: dict, file_id: Optional[str] = None, image_url: Optional[str] = None) -> dict:
    """Catalog one wardrobe item, backed by either a Telegram file_id (the
    normal photo-upload flow, see handle_photo_message) or a direct
    image_url (used for bulk-importing items from an external source,
    e.g. an order history page) - exactly one of the two should be set.
    See _fetch_wardrobe_item_bytes for how either is resolved back to
    bytes when the item is actually needed for tagging/try-on."""
    entry = get_wardrobe_entry(user_id)
    item = {
        "id": f"w_{uuid.uuid4().hex[:10]}",
        "file_id": file_id,
        "image_url": image_url,
        "type": tags.get("type", "other"),
        "subtype": tags.get("subtype"),
        "color": tags.get("color", ""),
        "warmth": tags.get("warmth", "medium"),
        "label": tags.get("label", "clothing item"),
        "description": tags.get("description"),
        "added_at": berlin_now().isoformat(),
    }
    entry["items"].append(item)
    _save_wardrobe_entry(user_id, entry)
    return item


def save_wardrobe_item_descriptions(descriptions: dict) -> None:
    """Store {item_id: description} on the matching items, in whichever
    student's wardrobe holds them (each opened in its own scope)."""
    _migrate_shared_wardrobe()
    for uid in db.private_owner_ids(WARDROBE_KIND):
        entry = get_wardrobe_entry(uid)
        changed = False
        for it in entry.get("items", []):
            if it["id"] in descriptions:
                it["description"] = descriptions[it["id"]]
                changed = True
        if changed:
            _save_wardrobe_entry(uid, entry)


def remove_wardrobe_item(user_id, item_id: str) -> None:
    entry = get_wardrobe_entry(user_id)
    entry["items"] = [it for it in entry["items"] if it["id"] != item_id]
    _save_wardrobe_entry(user_id, entry)


def clear_wardrobe_items(user_id) -> None:
    entry = get_wardrobe_entry(user_id)
    entry["items"] = []
    _save_wardrobe_entry(user_id, entry)


def set_wardrobe_reference_photo(user_id, file_id: str) -> None:
    entry = get_wardrobe_entry(user_id)
    entry["reference_photo_file_id"] = file_id
    _save_wardrobe_entry(user_id, entry)


def get_wardrobe_feature_enabled() -> bool:
    return bool(load_general_cache().get("wardrobe_feature_enabled"))


def set_wardrobe_feature_enabled(enabled: bool) -> None:
    cache = load_general_cache()
    cache["wardrobe_feature_enabled"] = enabled
    save_general_cache(cache)


def get_weekly_study_plan_enabled() -> bool:
    return bool(load_general_cache().get("weekly_study_plan_enabled"))


def set_weekly_study_plan_enabled(enabled: bool) -> None:
    cache = load_general_cache()
    cache["weekly_study_plan_enabled"] = enabled
    save_general_cache(cache)


STUDY_BLOCK_LOG_DAYS = 35  # how long sent study blocks and their feedback are kept
STUDY_BLOCK_STATUSES = ("sent", "done", "skipped")


def record_study_block(user_id, start_ts: int, status: str, course: Optional[str] = None,
                       minutes: Optional[int] = None) -> None:
    """Log a study block: "sent" when its pack goes out, then "done" or
    "skipped" from the student's answer (course and minutes are kept from "sent")."""
    if status not in STUDY_BLOCK_STATUSES:
        raise ValueError(status)
    cache = load_general_cache()
    log = cache.setdefault("study_blocks", {}).setdefault(str(user_id), {})
    entry = log.get(str(start_ts), {})
    entry["status"] = status
    if course is not None:
        entry["course"] = course
    if minutes is not None:
        entry["minutes"] = int(minutes)
    log[str(start_ts)] = entry
    cutoff = time.time() - STUDY_BLOCK_LOG_DAYS * 86400
    for key in [k for k in log if int(k) < cutoff]:
        del log[key]
    save_general_cache(cache)


def study_block_log(user_id, since_ts: float, until_ts: float) -> list[dict]:
    """[{"ts", "status", "course"}] of the student's blocks in [since, until), oldest first."""
    log = (load_general_cache().get("study_blocks") or {}).get(str(user_id)) or {}
    return [{"ts": int(k), **v} for k, v in sorted(log.items(), key=lambda kv: int(kv[0]))
            if since_ts <= int(k) < until_ts]


# ── task deadlines kept or missed (tasks are deleted once done) ─────────────
TASK_OUTCOME_KEEP_DAYS = 120


def record_task_outcome(user_id, task: dict, done_ts: Optional[float] = None) -> None:
    """Remember a deadline task: when it was due and, once the student marks
    it done, when that was. The first due time is kept, so a snoozed task
    finished later still counts as late."""
    if not task.get("due_time") or not task.get("id"):
        return
    cache = load_general_cache()
    log = cache.setdefault("task_outcomes", {}).setdefault(str(user_id), {})
    entry = log.get(task["id"]) or {"due": task["due_time"], "text": str(task.get("text") or "")[:120]}
    if done_ts is not None and not entry.get("done"):
        entry["done"] = int(done_ts)
    log[task["id"]] = entry
    cutoff = time.time() - TASK_OUTCOME_KEEP_DAYS * 86400
    for key in [k for k, v in log.items() if _iso_ts(v.get("due")) < cutoff]:
        del log[key]
    save_general_cache(cache)


def task_outcomes(user_id) -> list[dict]:
    """[{"due" (ISO), "done" (timestamp or None), "text"}] of the student's deadline tasks."""
    log = (load_general_cache().get("task_outcomes") or {}).get(str(user_id)) or {}
    return [{"id": k, **v} for k, v in log.items()]


def _iso_ts(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ── calendar feed and study buddy (per user) ──────────────────────────────────
def calendar_token(user_id, create: bool = True) -> Optional[str]:
    """The secret in the student's calendar feed link (made on first use)."""
    cache = load_general_cache()
    tokens = cache.setdefault("calendar_tokens", {})
    token = tokens.get(str(user_id))
    if token is None and create:
        token = tokens[str(user_id)] = secrets.token_urlsafe(24)
        save_general_cache(cache)
    return token


def reset_calendar_token(user_id) -> str:
    """A new feed link; the old one stops working."""
    cache = load_general_cache()
    cache.setdefault("calendar_tokens", {}).pop(str(user_id), None)
    save_general_cache(cache)
    return calendar_token(user_id) or ""


def user_for_calendar_token(token: str) -> Optional[int]:
    for uid, known in (load_general_cache().get("calendar_tokens") or {}).items():
        if token and hmac.compare_digest(known, token):
            return int(uid)
    return None


def get_study_buddy_enabled(user_id) -> bool:
    """Opt in: only then are the student's common free windows shown to
    classmates who opted in too (never their timetable itself)."""
    return bool((load_general_cache().get("study_buddy") or {}).get(str(user_id)))


def set_study_buddy_enabled(user_id, enabled: bool) -> None:
    cache = load_general_cache()
    cache.setdefault("study_buddy", {})[str(user_id)] = bool(enabled)
    save_general_cache(cache)


def get_ai_grades_enabled(user_id) -> bool:
    """Opt in: only then may a free question to the assistant include the
    student's grade average and module results (they leave the server for the
    AI provider)."""
    return bool((load_general_cache().get("ai_grades") or {}).get(str(user_id)))


def set_ai_grades_enabled(user_id, enabled: bool) -> None:
    cache = load_general_cache()
    cache.setdefault("ai_grades", {})[str(user_id)] = bool(enabled)
    save_general_cache(cache)


# general cache sections keyed by the student's id; "Delete my data" clears them
PERSONAL_CACHE_SECTIONS = ("study_blocks", "task_outcomes", "calendar_tokens", "study_buddy", "travel_modes",
                           "ai_grades", "personal_check_slots", "personal_check_last", "personal_check_manual")


def forget_personal_cache(user_id) -> list[str]:
    """Drop the student's entries from every per-student cache section."""
    cache = load_general_cache()
    removed = []
    for section in PERSONAL_CACHE_SECTIONS:
        entries = cache.get(section)
        if isinstance(entries, dict) and str(user_id) in entries:
            entries.pop(str(user_id))
            removed.append(section)
    if removed:
        save_general_cache(cache)
    return removed


TRAVEL_MODES = ("bike", "bus")


def get_travel_mode(user_id) -> str:
    """How the student gets between Haarentor and Wechloy: "bike" (default) or "bus"."""
    mode = (load_general_cache().get("travel_modes") or {}).get(str(user_id))
    return mode if mode in TRAVEL_MODES else "bike"


def set_travel_mode(user_id, mode: str) -> None:
    if mode not in TRAVEL_MODES:
        raise ValueError(mode)
    cache = load_general_cache()
    cache.setdefault("travel_modes", {})[str(user_id)] = mode
    save_general_cache(cache)


WARDROBE_OUTFIT_HISTORY_DAYS = 7  # how far back record_worn_items prunes to
WARDROBE_RECENT_WORN_DAYS = 2  # how far back get_recently_worn_item_ids looks when deprioritizing repeats


def get_cached_outfit_today(user_id) -> Optional[dict]:
    """Return today's already-generated outfit result for this user, if
    "Suggest Outfit Now" or the morning summary already produced one
    earlier today - {"description", "photo_file_id" (None if the image
    step failed and only text went out)} - so a second trigger the same
    day resends it instantly instead of burning another round of (quota-
    limited, free-tier) image-generation calls. None if nothing cached
    yet today."""
    cache = load_general_cache().get("outfit_today_cache", {}).get(str(user_id))
    if cache and cache.get("date") == datetime.now(TZ_BERLIN).date().isoformat():
        return cache
    return None


def save_cached_outfit_today(user_id, description: str, photo_file_id: Optional[str], color_tip: str = "", item_labels: Optional[list] = None) -> None:
    cache = load_general_cache()
    all_cached = cache.setdefault("outfit_today_cache", {})
    all_cached[str(user_id)] = {
        "date": datetime.now(TZ_BERLIN).date().isoformat(),
        "description": description,
        "color_tip": color_tip,
        "photo_file_id": photo_file_id,
        "item_labels": item_labels or [],
    }
    save_general_cache(cache)


def get_recently_worn_item_ids(user_id) -> set:
    """Item ids chosen in the last WARDROBE_RECENT_WORN_DAYS days, used to
    deprioritize (not hard-exclude - the wardrobe may simply have only one
    t-shirt) repeating the exact same outfit back-to-back."""
    history = load_general_cache().get("outfit_history", {}).get(str(user_id), {})
    cutoff = datetime.now(TZ_BERLIN).date() - timedelta(days=WARDROBE_RECENT_WORN_DAYS)
    recent_ids = set()
    for date_str, item_ids in history.items():
        try:
            if datetime.fromisoformat(date_str).date() >= cutoff:
                recent_ids.update(item_ids)
        except ValueError:
            continue
    return recent_ids


def record_worn_items(user_id, item_ids: list) -> None:
    """Record today's chosen item ids and prune entries older than
    WARDROBE_OUTFIT_HISTORY_DAYS so this cache doesn't grow forever."""
    cache = load_general_cache()
    history = cache.setdefault("outfit_history", {})
    user_history = history.setdefault(str(user_id), {})
    today_str = datetime.now(TZ_BERLIN).date().isoformat()
    user_history[today_str] = item_ids
    cutoff = datetime.now(TZ_BERLIN).date() - timedelta(days=WARDROBE_OUTFIT_HISTORY_DAYS)
    for date_str in list(user_history.keys()):
        try:
            if datetime.fromisoformat(date_str).date() < cutoff:
                del user_history[date_str]
        except ValueError:
            del user_history[date_str]
    save_general_cache(cache)
