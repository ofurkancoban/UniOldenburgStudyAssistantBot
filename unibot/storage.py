"""JSON cache files, stored settings and preferences, shared HTTP session, job locks.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import contextlib
import functools
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from studip_utils import TZ_BERLIN, berlin_now
from unibot import db, state
from unibot.config import ALLOWED_USER_IDS

# navigation & cache
LINK_CACHE_PATH = "link_cache.json"


class _PersistentLinkCache(dict):
    """dict that writes itself to LINK_CACHE_PATH shortly after any change.

    Inline buttons (file downloads, folders) only carry a short id; keeping
    the id -> target map on disk means buttons in older messages still work
    after a restart. Writes are debounced, so rendering a folder with dozens
    of buttons costs one write, not dozens."""

    FLUSH_DELAY_SECONDS = 2.0

    def __init__(self, path: str):
        super().__init__()
        self._path = path
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

    def flush(self) -> None:
        self._flush_handle = None
        try:
            db.save(self._path, dict(self), indent=None)
        except Exception as exc:
            logging.warning("Could not save %s: %s", self._path, exc)

    def __setitem__(self, key, value):
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


def load_tasks() -> list:
    if db.exists(TASKS_CACHE_PATH):
        try:
            return db.load(TASKS_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load tasks cache: %s", e)
    return []


def save_tasks(tasks: list) -> None:
    try:
        db.save(TASKS_CACHE_PATH, tasks)
    except Exception as e:
        logging.error("Could not save tasks cache: %s", e)


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


def save_studip_user_name(name: str) -> None:
    """Save the name fetched from Stud.IP for all allowed users so get_user_name()
    and get_primary_user_name() automatically return it."""
    if not name:
        return
    names = load_user_names()
    for uid in ALLOWED_USER_IDS:
        names[str(uid)] = name
    names["studip_name"] = name
    try:
        db.save(USER_NAMES_CACHE_PATH, names)
    except Exception as e:
        logging.error("Could not save Stud.IP user name: %s", e)


def get_user_name(user_id=None) -> Optional[str]:
    names = load_user_names()
    if user_id is not None and str(user_id) in names:
        return names[str(user_id)]
    return names.get("studip_name") or get_primary_user_name()


def get_primary_user_name() -> Optional[str]:
    """Return name synced from Stud.IP across ALLOWED_USER_IDS."""
    names = load_user_names()
    if names.get("studip_name"):
        return names["studip_name"]
    for uid in ALLOWED_USER_IDS:
        name = names.get(str(uid))
        if name:
            return name
    return None


def save_user_name(user_id, name: str) -> None:
    names = load_user_names()
    names[str(user_id)] = name
    try:
        db.save(USER_NAMES_CACHE_PATH, names)
    except Exception as e:
        logging.error("Could not save user name: %s", e)


WARDROBE_CACHE_PATH = "wardrobe.json"


def load_wardrobe() -> dict:
    """Return {"<user_id>": {"items": [...], "reference_photo_file_id": ...}}
    for the "👔 My Wardrobe" feature. Stores Telegram file_ids, not local image
    copies - they're free to re-resolve via bot.get_file() whenever an item or
    the reference photo needs to be downloaded transiently for a tagging/
    try-on API call, so this cache never needs its own media directory."""
    if db.exists(WARDROBE_CACHE_PATH):
        try:
            return db.load(WARDROBE_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load wardrobe: %s", e)
    return {}


def save_wardrobe(data: dict) -> None:
    try:
        db.save(WARDROBE_CACHE_PATH, data)
    except Exception as e:
        logging.error("Could not save wardrobe: %s", e)


def get_wardrobe_entry(user_id) -> dict:
    return load_wardrobe().get(str(user_id), {"items": [], "reference_photo_file_id": None})


def add_wardrobe_item(user_id, tags: dict, file_id: Optional[str] = None, image_url: Optional[str] = None) -> dict:
    """Catalog one wardrobe item, backed by either a Telegram file_id (the
    normal photo-upload flow, see handle_photo_message) or a direct
    image_url (used for bulk-importing items from an external source,
    e.g. an order history page) - exactly one of the two should be set.
    See _fetch_wardrobe_item_bytes for how either is resolved back to
    bytes when the item is actually needed for tagging/try-on."""
    wardrobe = load_wardrobe()
    entry = wardrobe.setdefault(str(user_id), {"items": [], "reference_photo_file_id": None})
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
    save_wardrobe(wardrobe)
    return item


def save_wardrobe_item_descriptions(descriptions: dict) -> None:
    """Store {item_id: description} on the matching items, any user."""
    wardrobe = load_wardrobe()
    for entry in wardrobe.values():
        for it in entry.get("items", []):
            if it["id"] in descriptions:
                it["description"] = descriptions[it["id"]]
    save_wardrobe(wardrobe)


def remove_wardrobe_item(user_id, item_id: str) -> None:
    wardrobe = load_wardrobe()
    entry = wardrobe.get(str(user_id))
    if entry:
        entry["items"] = [it for it in entry["items"] if it["id"] != item_id]
        save_wardrobe(wardrobe)


def clear_wardrobe_items(user_id) -> None:
    wardrobe = load_wardrobe()
    entry = wardrobe.get(str(user_id))
    if entry:
        entry["items"] = []
        save_wardrobe(wardrobe)


def set_wardrobe_reference_photo(user_id, file_id: str) -> None:
    wardrobe = load_wardrobe()
    entry = wardrobe.setdefault(str(user_id), {"items": [], "reference_photo_file_id": None})
    entry["reference_photo_file_id"] = file_id
    save_wardrobe(wardrobe)


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


def record_study_block(user_id, start_ts: int, status: str, course: Optional[str] = None) -> None:
    """Log a study block: "sent" when its pack goes out, then "done" or
    "skipped" from the student's answer (the course is kept from "sent")."""
    if status not in STUDY_BLOCK_STATUSES:
        raise ValueError(status)
    cache = load_general_cache()
    log = cache.setdefault("study_blocks", {}).setdefault(str(user_id), {})
    entry = log.get(str(start_ts), {})
    entry["status"] = status
    if course is not None:
        entry["course"] = course
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
