"""JSON cache files, stored settings and preferences, shared HTTP session, job locks.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import contextlib
import functools
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

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


def student_courses(user_id: Optional[int]) -> dict:
    """{cid: title} of the student's own courses: their fetched course list,
    else the courses the registry knows they follow (titles from the
    shared course table). Only the watcher session's own student falls back
    to the watcher's map. Never another student's courses."""
    if user_id is None:
        return {}
    own = state.user_courses_map.get(user_id)
    if own:
        return dict(own)
    try:
        cids = db.get_user_scope(int(user_id)).course_ids()
    except Exception:
        logging.debug("Course registry unavailable for a student", exc_info=True)
        cids = []
    if cids:
        titles = {}
        for cid in cids:
            row = None
            try:
                row = db.get_global_course(cid)
            except Exception:
                logging.debug("Course title unavailable", exc_info=True)
            titles[cid] = (row or {}).get("title") or state.courses_map.get(cid) or cid
        return titles
    watcher = state.global_session
    if watcher is not None and getattr(watcher, "user_id", None) == user_id:
        return dict(state.courses_map)
    return {}

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
    button such as "📊 Progress"), the second run would also
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


DEFAULT_SEMESTER_RECORD = "default_semester"


def _setting_owner(user_id=None) -> Optional[int]:
    """Whose setting: the given student, else the user the update acts for,
    else (background work) the primary admin, whose account the watcher uses."""
    if user_id is not None:
        return int(user_id)
    from unibot.auth import current_user_id
    uid = current_user_id()
    return uid if uid is not None else PRIMARY_ADMIN_ID


def load_default_semester(user_id=None) -> dict:
    """Return {"id": ..., "label": ...} for the semester the student picked
    via /status's "Set Default Semester" button, or {} if none has been set -
    in which case Files/Browse Courses/Sign Out each fall back to showing
    their own semester picker as before. Each student has their own."""
    uid = _setting_owner(user_id)
    if uid is None:
        return {}
    try:
        own = personal_record(uid, DEFAULT_SEMESTER_RECORD)
        if own:
            return own
        if uid == PRIMARY_ADMIN_ID and db.exists(DEFAULT_SEMESTER_CACHE_PATH):
            # the one shared setting from before was the bot owner's
            legacy = db.load(DEFAULT_SEMESTER_CACHE_PATH) or {}
            if legacy:
                save_personal_record(uid, DEFAULT_SEMESTER_RECORD, legacy)
            db.delete(DEFAULT_SEMESTER_CACHE_PATH)
            return legacy
    except Exception as e:
        logging.warning("Could not load default semester: %s", e)
    return {}


def save_default_semester(semester_id: str, label: str, user_id=None) -> None:
    uid = _setting_owner(user_id)
    if uid is None:
        return
    try:
        save_personal_record(uid, DEFAULT_SEMESTER_RECORD, {"id": semester_id, "label": label})
    except Exception as e:
        logging.error("Could not save default semester: %s", e)


def rebuild_watched_courses(user_id) -> None:
    """After a student changes what the watcher should follow (default
    semester, previous semester too): when they are the watcher session's
    own student, its course list is rebuilt on the next cycle. The map is
    kept until then rather than emptied, and another student's change
    leaves it alone."""
    watcher = state.global_session
    if watcher is not None and user_id is not None and getattr(watcher, "user_id", None) == int(user_id):
        state._courses_map_built_with_prev = None


SETUP_RECORD = "setup"
SETUP_ANSWERS_RECORD = "setup_answers"  # questions without a default answered in an unfinished setup


def setup_done(user_id) -> bool:
    """Whether the student went through the first-run setup (or already
    picked a semester before it existed), so it is offered only once. A
    semester picked inside a setup left unfinished doesn't count."""
    try:
        if personal_record(user_id, SETUP_RECORD):
            return True
        return bool(load_default_semester(user_id).get("id")) and personal_record(user_id, SETUP_ANSWERS_RECORD) is None
    except Exception:
        logging.debug("Setup state unavailable", exc_info=True)
        return True  # unsure: don't nag


def mark_setup_done(user_id) -> None:
    try:
        save_personal_record(user_id, SETUP_RECORD, {"done": int(time.time())})
    except Exception as e:
        logging.error("Could not save the setup state: %s", e)


def clear_default_semester(user_id=None) -> None:
    uid = _setting_owner(user_id)
    if uid is None:
        return
    try:
        delete_personal_record(uid, DEFAULT_SEMESTER_RECORD)
        if uid == PRIMARY_ADMIN_ID and db.exists(DEFAULT_SEMESTER_CACHE_PATH):
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


USER_NAMES_CACHE_PATH = "user_names.json"  # legacy: every name in one plain record
USER_NAME_RECORD = "display_name"     # each student's own name, encrypted
WATCHER_NAME_RECORD = "studip_name"   # the primary admin's Stud.IP name
_names_migrated: set = set()


def load_user_names() -> dict:
    """The legacy shared {"<user_id>": "Display Name"} record (names move to
    each student's own record on first use)."""
    if db.exists(USER_NAMES_CACHE_PATH):
        try:
            return db.load(USER_NAMES_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load user names: %s", e)
    return {}


def _take_legacy_name(key: str) -> Optional[str]:
    """Remove `key` from the legacy shared name record and return its value."""
    names = load_user_names()
    if key not in names:
        return None
    value = names.pop(key)
    try:
        if names:
            db.save(USER_NAMES_CACHE_PATH, names)
        else:
            db.delete(USER_NAMES_CACHE_PATH)
    except Exception as e:
        logging.error("Could not update the legacy name record: %s", e)
    return value


def _own_name(uid: int) -> Optional[str]:
    name = personal_record(uid, USER_NAME_RECORD)
    if not name and uid not in _names_migrated:
        _names_migrated.add(uid)
        name = _take_legacy_name(str(uid))
        if name:
            save_personal_record(uid, USER_NAME_RECORD, name)
    return name or None


def save_studip_user_name(name: str, user_id: Optional[int] = None) -> None:
    """Save the name Stud.IP reports for a student, unless they told the bot
    what to call them. Without a student it is the name of whoever the
    watcher session belongs to (any admin who signed in, not necessarily
    the primary one), and nobody's while that session has no owner."""
    if not name:
        return
    owner = user_id if user_id is not None else getattr(state.global_session, "user_id", None)
    if owner is None:
        return
    try:
        if not _own_name(int(owner)):
            save_personal_record(int(owner), USER_NAME_RECORD, name)
        if int(owner) == PRIMARY_ADMIN_ID:
            save_personal_record(PRIMARY_ADMIN_ID, WATCHER_NAME_RECORD, name)
    except Exception as e:
        logging.error("Could not save Stud.IP user name: %s", e)


def get_user_name(user_id=None) -> Optional[str]:
    """The student's own name, or None. Only the primary admin (or no
    student at all) falls back to the watcher account's Stud.IP name, so no
    student ever sees or hears someone else's name."""
    if user_id is not None:
        try:
            own = _own_name(int(user_id))
        except Exception:
            logging.debug("Name record unreadable", exc_info=True)
            own = None
        if own:
            return own
        if user_id != PRIMARY_ADMIN_ID:
            return None
    return get_primary_user_name()


def get_primary_user_name() -> Optional[str]:
    """The watcher account's (primary admin's) name synced from Stud.IP."""
    if PRIMARY_ADMIN_ID is None:
        return None
    try:
        name = personal_record(PRIMARY_ADMIN_ID, WATCHER_NAME_RECORD)
        if not name:
            name = _take_legacy_name("studip_name")
            if name:
                save_personal_record(PRIMARY_ADMIN_ID, WATCHER_NAME_RECORD, name)
        return name or _own_name(PRIMARY_ADMIN_ID)
    except Exception:
        logging.debug("Watcher name unreadable", exc_info=True)
        return None


def save_user_name(user_id, name: str) -> None:
    try:
        save_personal_record(int(user_id), USER_NAME_RECORD, name)
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


# ── personal records (per student, encrypted) ──────────────────────────────────
# These used to be sections of the shared, unencrypted general cache, keyed by
# the student's id. Each now lives in the student's own private record; the
# first read moves an old entry over and removes it from the shared cache.
PERSONAL_RECORD_PREFIX = "cache."
_legacy_checked: set = set()  # (user, section) pairs whose shared entry was already looked for


def personal_record(user_id, section: str, default: Any = None) -> Any:
    """The student's value of a former general-cache section (`default` if none)."""
    uid = int(user_id)
    scope = db.get_user_scope(uid)
    kind = PERSONAL_RECORD_PREFIX + section
    value = scope.private(kind)
    if value is None and (uid, section) not in _legacy_checked and not scope.has_private(kind):
        cache = load_general_cache()
        entries = cache.get(section)
        if isinstance(entries, dict) and str(uid) in entries:
            value = entries.pop(str(uid))
            scope.save_private(kind, value)
            save_general_cache(cache)
        _legacy_checked.add((uid, section))
    return default if value is None else value


def save_personal_record(user_id, section: str, value: Any) -> None:
    uid = int(user_id)
    db.get_user_scope(uid).save_private(PERSONAL_RECORD_PREFIX + section, value)
    if (uid, section) in _legacy_checked:
        return
    # a legacy entry must not come back on a later first read
    cache = load_general_cache()
    entries = cache.get(section)
    if isinstance(entries, dict) and str(uid) in entries:
        entries.pop(str(uid))
        save_general_cache(cache)
    _legacy_checked.add((uid, section))


def delete_personal_record(user_id, section: str) -> None:
    uid = int(user_id)
    db.get_user_scope(uid).delete_private(PERSONAL_RECORD_PREFIX + section)
    cache = load_general_cache()
    entries = cache.get(section)
    if isinstance(entries, dict) and str(uid) in entries:
        entries.pop(str(uid))
        save_general_cache(cache)


STUDY_PROGRESS_RECORD = "study_progress"
LEGACY_SHARED_PROGRESS = "study_progress.json"  # an admin's progress, mirrored in plain text


def load_student_progress(user_id) -> dict:
    """The student's /progress state from their encrypted record. The old
    plain documents (their own, and the shared admin mirror for the primary
    admin) move over on the first read and are deleted."""
    uid = int(user_id)
    data = personal_record(uid, STUDY_PROGRESS_RECORD)
    if data is not None:
        return data
    own = f"study_progress_{uid}.json"
    sources = [own] + ([LEGACY_SHARED_PROGRESS] if uid == PRIMARY_ADMIN_ID else [])
    for key in sources:
        try:
            if not db.exists(key):
                continue
            data = db.load(key) or {}
        except Exception as e:
            logging.warning("Could not read old study progress: %s", e)
            continue
        save_personal_record(uid, STUDY_PROGRESS_RECORD, data)
        for old in sources:
            if db.exists(old):
                db.delete(old)
        return data
    return {}


def save_student_progress(user_id, data: dict) -> None:
    save_personal_record(int(user_id), STUDY_PROGRESS_RECORD, data)


STUDY_BLOCK_LOG_DAYS = 35  # how long sent study blocks and their feedback are kept
STUDY_BLOCK_STATUSES = ("sent", "done", "skipped")


def record_study_block(user_id, start_ts: int, status: str, course: Optional[str] = None,
                       minutes: Optional[int] = None) -> None:
    """Log a study block: "sent" when its pack goes out, then "done" or
    "skipped" from the student's answer (course and minutes are kept from "sent")."""
    if status not in STUDY_BLOCK_STATUSES:
        raise ValueError(status)
    log = dict(personal_record(user_id, "study_blocks", {}))
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
    save_personal_record(user_id, "study_blocks", log)


def study_block_log(user_id, since_ts: float, until_ts: float) -> list[dict]:
    """[{"ts", "status", "course"}] of the student's blocks in [since, until), oldest first."""
    log = personal_record(user_id, "study_blocks", {}) or {}
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
    log = dict(personal_record(user_id, "task_outcomes", {}))
    entry = log.get(task["id"]) or {"due": task["due_time"], "text": str(task.get("text") or "")[:120]}
    if done_ts is not None and not entry.get("done"):
        entry["done"] = int(done_ts)
    log[task["id"]] = entry
    cutoff = time.time() - TASK_OUTCOME_KEEP_DAYS * 86400
    for key in [k for k, v in log.items() if _iso_ts(v.get("due")) < cutoff]:
        del log[key]
    save_personal_record(user_id, "task_outcomes", log)


def task_outcomes(user_id) -> list[dict]:
    """[{"due" (ISO), "done" (timestamp or None), "text"}] of the student's deadline tasks."""
    log = personal_record(user_id, "task_outcomes", {}) or {}
    return [{"id": k, **v} for k, v in log.items()]


def _iso_ts(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ── calendar feed and study buddy (per user) ──────────────────────────────────
# The token itself is in the student's private record; the shared cache only
# maps a hash of it to the student, so the feed URL can find its owner.
CALENDAR_TOKEN_INDEX = "calendar_token_index"


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _index_calendar_token(user_id, token: Optional[str], old: Optional[str] = None) -> None:
    cache = load_general_cache()
    index = dict(cache.get(CALENDAR_TOKEN_INDEX) or {})
    if old:
        index.pop(_token_digest(old), None)
    if token:
        index[_token_digest(token)] = int(user_id)
    cache[CALENDAR_TOKEN_INDEX] = index
    save_general_cache(cache)


def calendar_token(user_id, create: bool = True) -> Optional[str]:
    """The secret in the student's calendar feed link (made on first use)."""
    had_record = db.get_user_scope(int(user_id)).has_private(PERSONAL_RECORD_PREFIX + "calendar_tokens")
    token = personal_record(user_id, "calendar_tokens")
    if token is not None and not had_record:
        _index_calendar_token(user_id, token)  # moved over from the shared cache just now
    if token is None and create:
        token = secrets.token_urlsafe(24)
        save_personal_record(user_id, "calendar_tokens", token)
        _index_calendar_token(user_id, token)
    return token


def reset_calendar_token(user_id) -> str:
    """A new feed link; the old one stops working."""
    old = personal_record(user_id, "calendar_tokens")
    token = secrets.token_urlsafe(24)
    save_personal_record(user_id, "calendar_tokens", token)
    _index_calendar_token(user_id, token, old=old)
    return token


def forget_calendar_token(user_id) -> None:
    old = personal_record(user_id, "calendar_tokens")
    delete_personal_record(user_id, "calendar_tokens")
    if old:
        _index_calendar_token(user_id, None, old=old)


def user_for_calendar_token(token: str) -> Optional[int]:
    if not token:
        return None
    uid = (load_general_cache().get(CALENDAR_TOKEN_INDEX) or {}).get(_token_digest(token))
    if uid is None:
        # a link from before the move: its token is still in the shared cache
        for legacy_uid, known in (load_general_cache().get("calendar_tokens") or {}).items():
            if hmac.compare_digest(str(known), token):
                return int(legacy_uid) if calendar_token(int(legacy_uid), create=False) == token else None
        return None
    known = personal_record(uid, "calendar_tokens")
    return int(uid) if known and hmac.compare_digest(str(known), token) else None


def get_study_buddy_enabled(user_id) -> bool:
    """Opt in: only then are the student's common free windows shown to
    classmates who opted in too (never their timetable itself)."""
    return bool(personal_record(user_id, "study_buddy", False))


def set_study_buddy_enabled(user_id, enabled: bool) -> None:
    save_personal_record(user_id, "study_buddy", bool(enabled))


def get_ai_grades_enabled(user_id) -> bool:
    """Opt in: only then may a free question to the assistant include the
    student's grade average and module results (they leave the server for the
    AI provider)."""
    return bool(personal_record(user_id, "ai_grades", False))


def set_ai_grades_enabled(user_id, enabled: bool) -> None:
    save_personal_record(user_id, "ai_grades", bool(enabled))


AI_PERSONAL_OFF_NOTE = ("🔒 Your timetable, tasks and exams aren't shared with the AI service, so there is no "
                        "spoken version. You can allow it under /settings > 🔒 Privacy.")


def get_ai_personal_enabled(user_id) -> bool:
    """Opt in: only then do the student's timetable, tasks, registered exams,
    name and weather place go to the AI provider (OpenRouter), for the
    spoken morning summary, 🔊 Listen and spoken questions. Grades have
    their own switch (get_ai_grades_enabled)."""
    if user_id is None:
        return False
    return bool(personal_record(user_id, "ai_personal", False))


def set_ai_personal_enabled(user_id, enabled: bool) -> None:
    save_personal_record(user_id, "ai_personal", bool(enabled))


# former general cache sections keyed by the student's id (now personal
# records); "Delete my data" clears any entry still left in the shared cache
PERSONAL_CACHE_SECTIONS = ("study_blocks", "task_outcomes", "calendar_tokens", "study_buddy", "travel_modes",
                           "ai_grades", "personal_check_slots", "personal_check_last", "personal_check_manual",
                           "outfit_history", "outfit_today_cache", "pinned_morning_summary", "user_schedule_slots")


def forget_personal_cache(user_id) -> list[str]:
    """Drop the student's entries from every per-student cache section."""
    cache = load_general_cache()
    removed = []
    for section in PERSONAL_CACHE_SECTIONS:
        entries = cache.get(section)
        if isinstance(entries, dict) and str(user_id) in entries:
            entries.pop(str(user_id))
            removed.append(section)
    index = cache.get(CALENDAR_TOKEN_INDEX)
    if isinstance(index, dict):
        stale = [digest for digest, uid in index.items() if str(uid) == str(user_id)]
        for digest in stale:
            index.pop(digest)
        if stale:
            removed.append(CALENDAR_TOKEN_INDEX)
    if removed:
        save_general_cache(cache)
    return removed


TRAVEL_MODES = ("bike", "bus")


def get_travel_mode(user_id) -> str:
    """How the student gets between Haarentor and Wechloy: "bike" (default) or "bus"."""
    mode = personal_record(user_id, "travel_modes")
    return mode if mode in TRAVEL_MODES else "bike"


def set_travel_mode(user_id, mode: str) -> None:
    if mode not in TRAVEL_MODES:
        raise ValueError(mode)
    save_personal_record(user_id, "travel_modes", mode)


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
    cache = personal_record(user_id, "outfit_today_cache")
    if cache and cache.get("date") == datetime.now(TZ_BERLIN).date().isoformat():
        return cache
    return None


def save_cached_outfit_today(user_id, description: str, photo_file_id: Optional[str], color_tip: str = "", item_labels: Optional[list] = None) -> None:
    save_personal_record(user_id, "outfit_today_cache", {
        "date": datetime.now(TZ_BERLIN).date().isoformat(),
        "description": description,
        "color_tip": color_tip,
        "photo_file_id": photo_file_id,
        "item_labels": item_labels or [],
    })


def get_recently_worn_item_ids(user_id) -> set:
    """Item ids chosen in the last WARDROBE_RECENT_WORN_DAYS days, used to
    deprioritize (not hard-exclude - the wardrobe may simply have only one
    t-shirt) repeating the exact same outfit back-to-back."""
    history = personal_record(user_id, "outfit_history", {}) or {}
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
    user_history = dict(personal_record(user_id, "outfit_history", {}) or {})
    today_str = datetime.now(TZ_BERLIN).date().isoformat()
    user_history[today_str] = item_ids
    cutoff = datetime.now(TZ_BERLIN).date() - timedelta(days=WARDROBE_OUTFIT_HISTORY_DAYS)
    for date_str in list(user_history.keys()):
        try:
            if datetime.fromisoformat(date_str).date() < cutoff:
                del user_history[date_str]
        except ValueError:
            del user_history[date_str]
    save_personal_record(user_id, "outfit_history", user_history)
