"""Stud.IP JSON:API and HTML access: profile, courses, files, downloads, ZIP export.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import ast
import json
import logging
import posixpath
import re
import time
import weakref
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

import studip_oauth
from fast_enroll import current_semester_id, get_semester_options
from studip_utils import (
    TZ_BERLIN,
    berlin_now,
    redact_url,
    tag_attr,
)
from studip_utils import (
    safe_filename as _safe_filename,
)
from studip_utils import (
    safe_zip_path as _safe_zip_path,
)
from unibot import db, state
from unibot.auth import current_user_id, get_user_session, login_studip
from unibot.formatting import clean_html_text
from studip_http import UNI_HOSTS
from unibot.storage import (
    load_default_semester,
    save_studip_user_name,
)

from unibot.config import PRIMARY_ADMIN_ID  # noqa: E402
from unibot.preferences import get_pref  # noqa: E402

BASE_URL = "https://elearning.uni-oldenburg.de"
DOWNLOAD_CHUNK_SIZE = 256 * 1024
SEMESTER_CACHE_SECONDS = 6 * 60 * 60


class DownloadTooLargeError(ValueError):
    """Raised when a response exceeds the caller's in-memory byte limit."""


class LoginPageResponseError(RuntimeError):
    """Raised when a file URL returned a Stud.IP login page instead of a file."""


async def read_response_limited(response, max_bytes: int) -> bytes:
    """Read a response with a hard memory cap and reject an expired-login page."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    length = getattr(response, "content_length", None)
    if length is not None and length > max_bytes:
        raise DownloadTooLargeError(f"response is {length} bytes; limit is {max_bytes}")
    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise DownloadTooLargeError(f"response exceeded {max_bytes} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)
    headers = getattr(response, "headers", {})
    content_type = (getattr(response, "content_type", None)
                    or headers.get("Content-Type", "")).lower()
    if "html" in content_type:
        preview = body[:128 * 1024].lower()
        if b"loginform" in preview or b"login notwendig" in preview:
            raise LoginPageResponseError("Stud.IP returned its login page instead of the requested file")
    return body

# my_courses keeps its semester selection in the login session. Serialize its
# HTML fallback per Stud.IP session so two concurrent semester listings cannot
# switch that shared filter between the set_semester and page fetch requests.
_course_filter_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _course_filter_lock(session):
    loop = asyncio.get_running_loop()
    existing = _course_filter_locks.get(session)
    if existing is None or existing[0] is not loop:
        existing = (loop, asyncio.Lock())
        _course_filter_locks[session] = existing
    return existing[1]


def _normalize_url(u: str | None) -> str | None:
    """Normalize a university URL and reject untrusted or insecure targets."""
    if not u:
        return None
    u = u.strip()
    # If it's just a 32-char hex ID, it's not a valid URL yet
    if len(u) == 32 and re.match(r"^[a-f0-9]{32}$", u.lower()):
        return None

    # Repair the legacy concatenation seen in malformed Stud.IP links.
    prefix = f"{BASE_URL}https://"
    if u.startswith(prefix):
        u = u[len(BASE_URL):]
    if re.match(r"^https:/*", u, re.IGNORECASE):
        u = re.sub(r"^https:/*", "https://", u, flags=re.IGNORECASE)

    if not re.match(r"^https?://", u, re.IGNORECASE) and not u.startswith("//"):
        u = urljoin(f"{BASE_URL}/", u)
    elif u.startswith("//"):
        u = "https:" + u

    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme.lower() not in ("http", "https") or host not in UNI_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 80, 443)):
        logging.warning("Rejected untrusted Stud.IP URL host: %s", host or "missing")
        return None

    # University credentials must never be sent over plaintext HTTP.
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _normalize_file_urls(items: list) -> list:
    """Make every file or folder link safe before it reaches a session request."""
    for item in items:
        if item.get("url"):
            item["url"] = _normalize_url(item["url"])
    return items


def _course_list_owner(session: Any = None, user_id: Optional[int] = None) -> Optional[int]:
    """The student a course listing belongs to: the given one, else the
    session's, else the one the update acts for (None only for background
    work on the watcher session)."""
    if user_id is not None:
        return user_id
    if session is not None and getattr(session, "user_id", None) is not None:
        return session.user_id
    return current_user_id()


def _record_course(cid: str, name: str, user_id: Optional[int] = None, semester_id: Optional[str] = None) -> None:
    """Remember a course from a current-semester listing in its student's
    own map. Listings of a picked semester (Files, enrolment) are only shown,
    never recorded, so browsing an old term doesn't make its courses the
    student's. The shared watcher map is the watcher session's own current
    course list: only that student's (or a background) listing adds to it,
    so a course they enrol in mid-semester is followed without a rebuild."""
    if semester_id is not None:
        return
    if user_id is not None:
        if user_id not in state.user_courses_map:
            state.user_courses_map[user_id] = {}
        state.user_courses_map[user_id][cid] = name
    if user_id is None or user_id == getattr(state.global_session, "user_id", None):
        state.courses_map[cid] = name


# ── course listing ────────────────────────────────────────────────────────────
async def list_courses(
    semester_id: Optional[str] = None,
    session: Any = None,
    user_id: Optional[int] = None,
):
    """[(name, cid), ...] of the user's courses in semester_id (the current
    semester when None), via the JSON:API with the my_courses page as
    fallback. Also records courses in user_courses_map and courses_map."""
    user_id = _course_list_owner(session, user_id)
    try:
        courses = await _list_courses_api(semester_id, session=session, user_id=user_id)
        if courses:
            for name, cid in courses:
                _record_course(cid, name, user_id=user_id, semester_id=semester_id)
            logging.info("✅ %s courses from the Stud.IP API.", len(courses))
            return courses
    except Exception as e:
        logging.warning("list_courses: API failed (%s), falling back to the my_courses page", e)
    return await _list_courses_html(semester_id, session=session, user_id=user_id)


async def _list_courses_html(
    semester_id: Optional[str] = None,
    session: Any = None,
    user_id: Optional[int] = None,
):
    if session is None:
        session = await login_studip(user_id=user_id)
    if session is None:
        raise StudipApiError("No Stud.IP session available for course listing")
    async with _course_filter_lock(session):
        return await _list_courses_html_unlocked(semester_id, session=session, user_id=user_id)


async def _list_courses_html_unlocked(
    semester_id: Optional[str] = None,
    session: Any = None,
    user_id: Optional[int] = None,
):
    """
    Extract courses from the 'my_courses' page.
    Uses multi-method strategy: Vuex Store analysis (modern) and DOM Scrapy (legacy).

    If semester_id is given, first switches the account's my_courses semester
    filter to that specific semester (via my_courses/set_semester?sem_select=
    <id>, the same request the page's own semester dropdown makes) so the
    listing that follows is scoped to it, instead of whatever the account's
    filter happened to be left on last.
    """
    if session is None:
        session = await login_studip(user_id=user_id)
    if session is None:
        logging.error("❌ Failed to get session for list_courses")
        return []

    if semester_id:
        async with await session.get(
            "https://elearning.uni-oldenburg.de/dispatch.php/my_courses/set_semester",
            params={"sem_select": semester_id},
        ) as r:
            await r.text()
            if r.status != 200:
                raise StudipApiError(f"failed to switch semester filter: HTTP {r.status}")

    all_courses_dict = {}

    # Try multiple attempts or varying URLs if needed. The "all semesters"
    # fallback would defeat a specific semester_id filter set just above, so
    # it's only used when no specific semester was requested.
    urls = [
        "https://elearning.uni-oldenburg.de/dispatch.php/my_courses",
    ] if semester_id else [
        "https://elearning.uni-oldenburg.de/dispatch.php/my_courses",
        "https://elearning.uni-oldenburg.de/dispatch.php/my_courses?semester_filter=all"
    ]

    for url in urls:
        logging.info("Attempting course extraction from: %s", redact_url(url))
        try:
            async with await session.get(url) as r:
                html_text = await r.text()
                if r.status != 200:
                    raise StudipApiError(f"course page answered HTTP {r.status}")
        except Exception as e:
            logging.error("Failed to fetch %s: %s", redact_url(url), e)
            if isinstance(e, StudipApiError):
                raise
            continue
        if "loginform" in html_text.lower():
            raise StudipApiError("course page returned the login form")
            
        # --- Method 1: JSON-based Vuex state (Comprehensive) ---
        # Stud.IP often embeds a JSON payload for its Vue.js components
        json_scripts = re.findall(r'<script\s+type="application/json"[^>]*>(.*?)</script>', html_text, re.S)
        for script_content in json_scripts:
            try:
                content = script_content.strip()
                # Sometimes it's wrapped in a JS call, sometimes raw
                if 'JSON.parse(' in content:
                    inner_match = re.search(
                        r'''JSON\.parse\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*\)''',
                        content,
                        re.S,
                    )
                    if inner_match:
                        # Decode JavaScript escapes without re-decoding raw UTF-8
                        # bytes as Latin-1, which turns names like "Müller" into
                        # mojibake. JSON.parse's argument is a JS string literal.
                        json_str = ast.literal_eval(inner_match.group(1))
                        full_data = json.loads(json_str)
                    else: continue
                else:
                    full_data = json.loads(content)

                # Direct deep-dive into known store paths
                # Path 1: vuexStoreData -> mycourses
                if isinstance(full_data, dict):
                    store_data = full_data.get("vuexStoreData", {})
                    mycourses = store_data.get("mycourses", {})
                    
                    # setCourses contains the flat map of all courses
                    courses_payload = mycourses.get("setCourses", {})
                    if courses_payload:
                        for cid, info in courses_payload.items():
                            if isinstance(info, dict) and "name" in info:
                                name = info.get("name", "").strip()
                                if name:
                                    all_courses_dict[cid] = name
                                    _record_course(cid, name, user_id=user_id, semester_id=semester_id)
                                    
                    # Fallback: Recursive search in this specific JSON block
                    if not courses_payload:
                        def find_courses_recursive(obj):
                            if not isinstance(obj, (dict, list)): return
                            if isinstance(obj, dict):
                                if "setCourses" in obj and isinstance(obj["setCourses"], dict):
                                    for k, v in obj["setCourses"].items():
                                        if isinstance(v, dict) and "name" in v:
                                            all_courses_dict[k] = v.get("name")
                                elif "id" in obj and "name" in obj and len(str(obj["id"])) == 32:
                                    all_courses_dict[str(obj["id"])] = obj["name"]
                                for v in obj.values(): find_courses_recursive(v)
                            elif isinstance(obj, list):
                                for v in obj: find_courses_recursive(v)
                        find_courses_recursive(full_data)
            except Exception as e:
                logging.debug("JSON extract failed for one script: %s", e)
                continue

        # --- Method 2: BeautifulSoup Scraper (Legacy/Traditional) ---
        soup = BeautifulSoup(html_text, "html.parser")
        
        # Pattern 1: Table rows with data-course-id (Common in desktop view)
        for row in soup.find_all(["tr", "div", "li"], attrs={"data-course-id": True}):
            cid = tag_attr(row, "data-course-id")
            # Try to find a name in the text contents or a title
            name = tag_attr(row, "title") or row.get_text(" ", strip=True)
            if cid and name and len(name) > 3:
                all_courses_dict[cid] = name
                _record_course(cid, name, user_id=user_id, semester_id=semester_id)

        # Pattern 2: Any link containing a course ID pattern
        course_links = soup.find_all("a", href=re.compile(r"(?:course_id=|to=|details\.php\?id=)([a-f0-9]{32})"))
        for a in course_links:
            href = tag_attr(a, "href")
            match = re.search(r"(?:course_id=|to=|details\.php\?id=)([a-f0-9]{32})", href)
            if match:
                cid = match.group(1)
                name = a.get_text(strip=True)
                if name and len(name) > 3:
                    if cid not in all_courses_dict:
                        all_courses_dict[cid] = name
                        _record_course(cid, name, user_id=user_id, semester_id=semester_id)
        
        # If we found courses on the first URL, we might still want to try the second 
        # to ensure we get "all" if the first was restricted to one semester.
    
    if all_courses_dict:
        logging.info("✅ Extracted %s unique courses.", len(all_courses_dict))
        # Ensure the map is updated for other functions
        for cid, name in all_courses_dict.items():
            _record_course(cid, name, user_id=user_id, semester_id=semester_id)
        # The bot expects (name, cid) tuples
        return [(name, cid) for cid, name in all_courses_dict.items()]

    logging.warning("⚠️ No courses found during extraction.")
    return []



async def _switch_semester_filter(session, semester_id: str) -> None:
    """Explicitly switch Stud.IP's own session-level "my_courses" semester
    filter - the exact same request list_courses(semester_id=...) makes as
    a side effect. Used to restore the filter back to a known semester
    after a temporary switch to fetch a *different* semester's data (e.g.
    the previous one, for the previous-semester enrolment/watch features),
    since leaving the shared, long-lived session pinned to the wrong
    semester would otherwise silently corrupt any later no-arg
    list_courses() call elsewhere that assumes it reflects "current"."""
    async with _course_filter_lock(session):
        try:
            async with await session.get(
                "https://elearning.uni-oldenburg.de/dispatch.php/my_courses/set_semester",
                params={"sem_select": semester_id},
            ) as r:
                await r.text()
            if r.status != 200:
                raise StudipApiError(f"failed to restore semester filter: HTTP {r.status}")
        except Exception as e:
            logging.warning("_switch_semester_filter: failed to switch to %s: %s", semester_id, e)
            raise


async def _ensure_watched_courses_map(session) -> None:
    """(Re)builds the shared `courses_map` global - used by all three
    background watchers (announcements, new files, forum posts) to decide
    which courses to check - to include the previous semester's courses
    too when that's enabled (see get_watch_previous_semester_enabled /
    /status's toggle). Only rebuilds when the map is empty or the toggle's
    state has changed since the last build, not on every watcher cycle.

    Deliberately resolves "current semester" explicitly (default_semester,
    falling back to the semester running today, see current_semester_id) rather than
    relying on a no-arg list_courses() call reflecting "current" - that
    depends on whatever semester some unrelated browsing interaction
    (Files/Enroll/Sign Out's own semester pickers) last left the shared
    session's filter pointed at, which isn't reliable here."""
    setting_owner = getattr(session, "user_id", None) or PRIMARY_ADMIN_ID
    want_prev = bool(get_pref(setting_owner, "watch_previous_semester"))
    refresh_after = 6 * 3600 if state.courses_map_complete else 15 * 60
    if (state.courses_map and state._courses_map_built_with_prev == want_prev
            and time.time() - state._courses_map_refreshed_at < refresh_after):
        return

    anchor_id = None
    semester_options = []
    try:
        default = load_default_semester(setting_owner)  # the watcher account's own setting
        semester_options = await get_semester_options(session)
        anchor_id = default.get("id") or current_semester_id(semester_options)
    except Exception as e:
        logging.warning("_ensure_watched_courses_map: failed to resolve current semester: %s", e)
        state.courses_map_complete = False
        return
    if not anchor_id:
        logging.warning("_ensure_watched_courses_map: no current semester could be identified")
        state.courses_map_complete = False
        return

    # always the given (watcher) session's own list, never the acting student's
    owner = getattr(session, "user_id", None)
    courses_list = await list_courses(semester_id=anchor_id, session=session, user_id=owner)
    merged = {cid: name for name, cid in courses_list}

    complete = True
    if want_prev and anchor_id:
        idx = next((i for i, (sid, _) in enumerate(semester_options) if sid == anchor_id), None)
        if idx is None:
            complete = False
            logging.warning("_ensure_watched_courses_map: anchor semester %s is missing from options", anchor_id)
        elif idx + 1 < len(semester_options):
            prev_id, prev_label = semester_options[idx + 1]
            try:
                prev_courses = await list_courses(semester_id=prev_id, session=session, user_id=owner)
                for name, cid in prev_courses:
                    merged.setdefault(cid, name)
                logging.info("👀 Also watching %s course(s) from %s", len(prev_courses), prev_label)
            except Exception as e:
                complete = False
                logging.warning("_ensure_watched_courses_map: previous-semester fetch failed: %s", e)
            finally:
                # Leave the shared session's semester filter back on the
                # current/anchor semester, not stuck on the previous one.
                await _switch_semester_filter(session, anchor_id)

    if setting_owner is not None:
        lifecycle = db.get_lifecycle(setting_owner)
        if lifecycle["state"] == "inactive" and lifecycle["reason"] == "deleted their data":
            return
    state.courses_map = merged
    state._courses_map_built_with_prev = want_prev
    state._courses_map_refreshed_at = time.time()
    state.courses_map_complete = complete


# ──────────────────────────────────────────────────────────────────────────────
#  FILE LISTING + ZIP CREATION + WATCHER
# ──────────────────────────────────────────────────────────────────────────────


# ── Stud.IP JSON:API ───────────────────────────────────────────────────────────
# Stud.IP's own REST API (JSON:API), authenticated with the same logged-in
# session cookie as the HTML pages, so it needs no extra registration.
# Courses, folders/files, announcements and messages come from here; each
# caller falls back to the old HTML scraping if the API call fails, and the
# API results are shaped exactly like the scraped ones (same keys, ids, URLs,
# size strings and timestamps), so existing caches keep matching.
STUDIP_API_URL = f"{BASE_URL}/jsonapi.php/v1"
STUDIP_API_PAGE_LIMIT = 100
STUDIP_STUDYGROUP_COURSE_TYPE = 99  # study groups, which my_courses lists separately
_FOLDER_ID_RE = re.compile(r"files/index/([0-9a-f]{32})")


class StudipApiError(Exception):
    pass


async def studip_api_get(path: str, params: Optional[dict] = None, session: Any = None,
                         user_id: Optional[int] = None, _oauth_retry: bool = True,
                         _session_retry: bool = True) -> dict:
    """GET one JSON:API resource ("/users/me" or a "/jsonapi.php/..." link).

    The bearer header is added by StudIPSession, so API traffic uses the
    stored OAuth token when there is one and the cookie session otherwise.
    On a 401 it refreshes OAuth once, then tries the cookie/SSO session once."""
    session = session or get_user_session(user_id) or await login_studip(user_id=user_id)
    url = BASE_URL + path if path.startswith("/jsonapi.php") else STUDIP_API_URL + path
    async with await session.get(url, params=params) as r:
        status, text = r.status, await r.text()
    if status == 401:
        eff_user_id = getattr(session, "user_id", None) or user_id
        use_oauth = studip_oauth.oauth_enabled()
        if _oauth_retry and use_oauth:
            try:
                has_tokens = studip_oauth.load_tokens(user_id=eff_user_id) is not None
                token = (await studip_oauth.get_access_token(force_refresh=True, user_id=eff_user_id)
                         if has_tokens else None)
            except Exception as e:
                logging.warning("OAuth token refresh failed; trying the cookie session: %s", type(e).__name__)
                token = None
            if token:
                # If the refreshed bearer token is also rejected, the next call
                # can still heal the independent cookie/SSO session.
                return await studip_api_get(path, params, session=session, user_id=user_id,
                                            _oauth_retry=False, _session_retry=_session_retry)
        # Self-healing silent SSO renewal attempt
        if _session_retry:
            from studip_session import cookie_auth_only
            with cookie_auth_only():
                if await session.heal_session():
                    return await studip_api_get(path, params, session=session, user_id=user_id,
                                                _oauth_retry=False, _session_retry=False)
                await session.login()  # respects the failed-login backoff
                return await studip_api_get(path, params, session=session, user_id=user_id,
                                            _oauth_retry=False, _session_retry=False)
    if status != 200:
        raise StudipApiError(f"{path}: HTTP {status}")
    try:
        document = json.loads(text)
    except ValueError as e:
        raise StudipApiError(f"{path}: response is not JSON") from e
    # A proxy error page or an unrelated JSON response can still arrive with
    # HTTP 200. Treat it as an API failure so callers use their normal
    # fallback instead of mistaking a missing `data` member for an empty
    # inbox, course, or announcement collection.
    if not isinstance(document, dict) or "data" not in document:
        raise StudipApiError(f"{path}: response is not a JSON:API resource document")
    return document


async def studip_api_get_all(
    path: str,
    params: Optional[dict] = None,
    session: Any = None,
    user_id: Optional[int] = None,
) -> tuple[list, list]:
    """Every page of a JSON:API collection, as (data, included)."""
    params = dict(params or {}, **{"page[limit]": STUDIP_API_PAGE_LIMIT})
    data: list[Any] = []
    included: list[Any] = []
    offset = 0
    page_fingerprints: set[str] = set()
    while True:
        params["page[offset]"] = offset
        page = await studip_api_get(path, params, session=session, user_id=user_id)
        page_data = page.get("data", [])
        if not isinstance(page_data, list):
            raise StudipApiError(f"{path}: JSON:API page data is not a list")
        if not page_data:
            return data, included

        fingerprint = json.dumps(page_data, sort_keys=True, ensure_ascii=False, default=str)
        if fingerprint in page_fingerprints:
            raise StudipApiError(f"{path}: pagination repeated a page at offset {offset}")
        page_fingerprints.add(fingerprint)
        data.extend(page_data)

        page_included = page.get("included", [])
        if not isinstance(page_included, list):
            raise StudipApiError(f"{path}: JSON:API page included is not a list")
        included.extend(page_included)
        offset += STUDIP_API_PAGE_LIMIT
        meta = page.get("meta")
        page_meta = meta.get("page") if isinstance(meta, dict) else None
        total_raw = page_meta.get("total") if isinstance(page_meta, dict) else None
        total: Optional[int] = None
        if isinstance(total_raw, (int, str)) and not isinstance(total_raw, bool):
            try:
                total = int(total_raw)
            except ValueError:
                pass
        # Some valid collection responses omit pagination metadata. Continue
        # fetching offsets until the API returns an empty page instead of
        # silently treating the unknown total as zero.
        if total is not None and total > 0 and offset >= total:
            return data, included


# Stud.IP profile per Telegram user (the admin's lives in state._studip_user_profile)
_user_profiles: dict[int, dict] = {}


def _uses_global_profile(user_id: Optional[int]) -> bool:
    """The admin session's profile is cached globally, everyone else's per user."""
    if user_id is None:
        return True
    sess = state.user_sessions.get(user_id)
    return sess is not None and sess is state.global_session


async def get_studip_user_profile(user_id: Optional[int] = None, session: Any = None) -> dict:
    """Fetch profile of the currently authenticated user from Stud.IP /users/me."""
    if user_id is None and session is None:
        user_id = current_user_id()
    if user_id is not None and not _uses_global_profile(user_id):
        cached = _user_profiles.get(user_id)
        if cached and cached.get("id"):
            return cached
    if _uses_global_profile(user_id) and session is None and state._studip_user_profile:
        return state._studip_user_profile
    try:
        res = await studip_api_get("/users/me", session=session, user_id=user_id)
        user = res.get("data", {})
        studip_id = user.get("id")
        attrs = user.get("attributes", {})
        formatted_name = (attrs.get("formatted-name") or "").strip()
        given_name = (attrs.get("given-name") or "").strip()
        family_name = (attrs.get("family-name") or "").strip()
        username = (attrs.get("username") or "").strip()
        display_name = formatted_name or given_name or username
        profile = {
            "id": studip_id,
            "username": username,
            "formatted_name": formatted_name,
            "given_name": given_name,
            "family_name": family_name,
            "display_name": display_name,
        }
        if _uses_global_profile(user_id):
            state._studip_api_user_id = studip_id
            state._studip_user_profile = profile
            if display_name:
                # the watcher session's own student, never simply the primary admin
                owner = user_id if user_id is not None else getattr(session or state.global_session, "user_id", None)
                if owner is not None:
                    save_studip_user_name(display_name, user_id=owner)
        elif studip_id and user_id is not None:
            _user_profiles[user_id] = profile
            if display_name:
                save_studip_user_name(display_name, user_id=user_id)
        return profile
    except Exception as exc:
        logging.warning("Could not fetch Stud.IP user profile: %s", exc)
        return (state._studip_user_profile or {}) if _uses_global_profile(user_id) else {}


def forget_user_profile(user_id: Optional[int]) -> None:
    """Drop a cached profile (after a logout or a login with another account)."""
    if user_id is not None:
        _user_profiles.pop(user_id, None)


async def studip_api_user_id(session: Any = None, user_id: Optional[int] = None) -> str:
    """The Stud.IP user id of the session's student. Raises StudipApiError
    when it is unknown, so no caller builds a /users//... path."""
    if user_id is None:
        user_id = getattr(session, "user_id", None) if session is not None else current_user_id()
    if user_id is not None and not _uses_global_profile(user_id):
        profile = await get_studip_user_profile(user_id=user_id, session=session)
        uid = profile.get("id") or ""
    else:
        if not state._studip_api_user_id:
            profile = await get_studip_user_profile()
            state._studip_api_user_id = profile.get("id")
        uid = state._studip_api_user_id or ""
    if not uid:
        raise StudipApiError("Stud.IP user id unknown")
    return uid


def _api_rel_id(resource: dict, name: str) -> Optional[str]:
    data = (resource.get("relationships", {}).get(name) or {}).get("data")
    return data.get("id") if isinstance(data, dict) else None


def _api_names(included: list) -> dict:
    """{user_id: formatted name} from a response's included users."""
    return {u["id"]: u["attributes"].get("formatted-name") or "Unknown" for u in included if u.get("type") == "users"}


def _api_html_to_text(content: Optional[str]) -> str:
    """Plain text of an API HTML field, same cleaning as the scraped pages."""
    return clean_html_text(BeautifulSoup(content or "", "lxml"))


def _api_berlin(iso: Optional[str]) -> Optional[datetime]:
    try:
        if not iso:
            return None
        # Python 3.10 does not accept RFC 3339's trailing "Z" in
        # fromisoformat; normalize it while retaining offsets and fractions.
        value = iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=TZ_BERLIN)
        return parsed.astimezone(TZ_BERLIN)
    except (TypeError, ValueError):
        return None


async def _list_courses_api(
    semester_id: Optional[str] = None,
    session: Any = None,
    user_id: Optional[int] = None,
) -> list:
    """(name, cid) for the user's courses in semester_id (the current
    semester when None, which is what every no-argument caller means), like
    the my_courses page: study groups are left out, and a course without an
    end semester runs unlimited, is listed in every semester and gets
    " (unrestricted)" appended, exactly as that page names it."""
    if (state._studip_api_semester_starts is None
            or time.time() - state._studip_api_semesters_refreshed_at >= SEMESTER_CACHE_SECONDS):
        semesters, _ = await studip_api_get_all("/semesters", session=session, user_id=user_id)
        state._studip_api_semester_starts = {
            x["id"]: start
            for x in semesters
            if (start := _api_berlin(x.get("attributes", {}).get("start"))) is not None
        }
        state._studip_api_current_semester = next((x["id"] for x in semesters if x["attributes"].get("is-current")), None)
        state._studip_api_semesters_refreshed_at = time.time()
    starts = state._studip_api_semester_starts
    uid_str = await studip_api_user_id(session=session, user_id=user_id)
    courses, _ = await studip_api_get_all(f"/users/{uid_str}/courses", session=session, user_id=user_id)
    target_id = semester_id or state._studip_api_current_semester
    target = starts.get(target_id) if target_id else None
    if target is None:
        raise StudipApiError(f"Start date for semester {target_id or '(current)'} is unavailable")
    result = []
    for c in courses:
        if c["attributes"].get("course-type") == STUDIP_STUDYGROUP_COURSE_TYPE:
            continue
        start_id, end_id = _api_rel_id(c, "start-semester"), _api_rel_id(c, "end-semester")
        if target and end_id:
            if starts.get(start_id, target) > target or starts.get(end_id, target) < target:
                continue
        name = (c["attributes"].get("title") or "").strip()
        if name:
            result.append((name if end_id else f"{name} (unrestricted)", c["id"]))
    return result


async def _list_files_api(cid, folder_url=None, session=None, user_id=None) -> list:
    """One folder level of a course in list_files' format: the course's root
    folder, or the folder a list_files folder "url" points to."""
    m = _FOLDER_ID_RE.search(folder_url) if folder_url else None
    if m:
        folder_id = m.group(1)
    elif folder_url and "dispatch.php/course/files" not in folder_url:
        raise StudipApiError(f"no folder id in {folder_url}")
    else:
        folders, _ = await studip_api_get_all(f"/courses/{cid}/folders", session=session, user_id=user_id)
        root = next((f for f in folders if f["attributes"].get("folder-type") == "RootFolder"), None) \
            or next((f for f in folders if not _api_rel_id(f, "parent")), None)
        if not root:
            return []
        folder_id = root["id"]

    def stamps(resource):
        dt = _api_berlin(resource["attributes"].get("chdate"))
        return (dt.strftime("%d.%m.%Y %H:%M"), dt.isoformat()) if dt else ("-", None)

    items = []
    subfolders, _ = await studip_api_get_all(f"/folders/{folder_id}/folders", session=session, user_id=user_id)
    for f in subfolders:
        modified, modified_iso = stamps(f)
        items.append({
            "type": "folder", "name": f["attributes"].get("name", "Unknown Folder"),
            "url": f"{BASE_URL}/dispatch.php/course/files/index/{f['id']}?cid={cid}",
            "size": "-", "modified": modified, "modified_iso": modified_iso,
        })
    file_refs, _ = await studip_api_get_all(f"/folders/{folder_id}/file-refs", session=session, user_id=user_id)
    for f in file_refs:
        modified, modified_iso = stamps(f)
        rs = f["attributes"].get("filesize")
        if isinstance(rs, int):
            size_str = f"{rs/(1024*1024):.1f} MB" if rs > 1024*1024 else f"{rs/1024:.1f} KB" if rs > 1024 else f"{rs} B"
        else:
            size_str = "-"
        items.append({
            "type": "file", "id": f["id"], "name": f["attributes"].get("name", "Unknown File"),
            # The meta download-url points at sendfile.php, which only accepts
            # the cookie session. The JSON:API content endpoint also works with
            # a bearer token, so use that instead.
            "url": f"{STUDIP_API_URL}/file-refs/{f['id']}/content"
                   if f["attributes"].get("is-downloadable", True) else None,
            "size": size_str, "modified": modified, "modified_iso": modified_iso,
        })
    return items


async def list_files(cid, folder_url=None, session=None, user_id=None):
    """Files and folders of one course folder level (the root folder when
    folder_url is None), via the JSON:API with the HTML file table as
    fallback."""
    try:
        return _normalize_file_urls(await _list_files_api(cid, folder_url, session=session, user_id=user_id))
    except Exception as e:
        if getattr(e, "quiet", False):
            raise
        logging.warning("list_files: API failed for %s (%s), falling back to the HTML page", cid, e)
    return _normalize_file_urls(await _list_files_html(cid, folder_url, session=session, user_id=user_id))


async def _list_files_html(cid, folder_url=None, session=None, user_id=None):
    """Extract files/folders from Stud.IP file table browser-less."""
    url = _normalize_url(folder_url) if folder_url else f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
    logging.info(">>> list_files called with cid=%s, folder_url=%s => %s", cid, redact_url(folder_url), redact_url(url))

    active_session: Any = session or get_user_session(user_id)
    if active_session is None and user_id is not None:
        # Never use the admin's session as a fallback for a student's files.
        # A failed JSON:API request may be an expired student login; falling
        # through to the global session would show the wrong account's data.
        active_session = await login_studip(user_id=user_id)
    elif active_session is None:
        active_session = state.global_session or await login_studip()
    if active_session is None:
        raise RuntimeError(f"No Stud.IP session available for file listing of course {cid}")
    try:
        async with await active_session.get(url) as resp:
            html_text = await resp.text()
    except Exception as e:
        if getattr(e, "quiet", False):
            raise
        logging.error("Failed to fetch files: %s", e)
        raise

    if resp.status != 200:
        raise StudipApiError(f"course files page answered HTTP {resp.status}")
    if "loginform" in html_text.lower():
        raise StudipApiError("course files page returned the login form")

    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    
    # --- Method 1: JSON-based extraction (Robust) ---
    form = soup.select_one("form#files_table_form")
    if form:
        try:
            data_files_raw = tag_attr(form, "data-files")
            data_folders_raw = tag_attr(form, "data-folders")
            
            data_files = json.loads(data_files_raw) if data_files_raw else []
            data_folders = json.loads(data_folders_raw) if data_folders_raw else []
            
            # Process folders
            for folder in data_folders:
                name = folder.get("name", "Unknown Folder")
                furl = folder.get("url")
                if furl: furl = urljoin(BASE_URL, furl)
                
                # Modified date
                chdate = folder.get("chdate")
                modified = "-"
                modified_iso = None
                if chdate:
                    try:
                        dt = datetime.fromtimestamp(int(chdate), tz=timezone.utc).astimezone(TZ_BERLIN)
                        modified = dt.strftime("%d.%m.%Y %H:%M")
                        modified_iso = dt.isoformat()
                    except (ValueError, TypeError, OverflowError, OSError): pass
                
                items.append({
                    "type": "folder",
                    "name": name,
                    "url": furl,
                    "size": "-",
                    "modified": modified,
                    "modified_iso": modified_iso
                })
            
            # Process files
            for f in data_files:
                name = f.get("name", "Unknown File")
                durl = f.get("download_url")
                if durl: durl = urljoin(BASE_URL, durl)
                
                # Size formatting
                raw_size = f.get("size")
                size_str = "-"
                if raw_size:
                    try:
                        rs = int(raw_size)
                        if rs > 1024*1024: size_str = f"{rs/(1024*1024):.1f} MB"
                        elif rs > 1024: size_str = f"{rs/1024:.1f} KB"
                        else: size_str = f"{rs} B"
                    except (ValueError, TypeError): size_str = str(raw_size)
                
                # Modified date
                chdate = f.get("chdate")
                modified = "-"
                modified_iso = None
                if chdate:
                    try:
                        dt = datetime.fromtimestamp(int(chdate), tz=timezone.utc).astimezone(TZ_BERLIN)
                        modified = dt.strftime("%d.%m.%Y %H:%M")
                        modified_iso = dt.isoformat()
                    except (ValueError, TypeError, OverflowError, OSError): pass
                    
                items.append({
                    "type": "file",
                    "name": name,
                    "url": durl,
                    "size": size_str,
                    "modified": modified,
                    "modified_iso": modified_iso
                })
                
            logging.info("✅ Extracted %s items via JSON from %s", len(items), redact_url(url))
            return items
        except Exception as json_err:
            logging.warning("JSON file extraction failed, falling back to BS4: %s", json_err)

    # --- Method 2: BeautifulSoup Fallback (Legacy) ---
    table = soup.select_one("table.documents")
    if not table:
        raise StudipApiError("course files page did not contain a recognized file listing")

    # Subfolders
    for row in table.select("tbody.subfolders tr"):
        link_tag = row.select_one("td:nth-child(3) a")
        if not link_tag: continue
        name = link_tag.get_text(strip=True)
        href: Optional[str] = tag_attr(link_tag, "href") or None
        if href:
            href = urljoin(BASE_URL, href)
        
        time_tag = row.find("time")
        modified = time_tag.get_text(strip=True) if time_tag else "-"
        modified_iso = tag_attr(time_tag, "datetime") or None
        
        items.append({
            "type": "folder",
            "name": name,
            "url": href,
            "size": "-",
            "modified": modified,
            "modified_iso": modified_iso
        })

    # Files
    for row in table.select("tbody.files tr"):
        name_tag = row.select_one("td:nth-child(3) a span") or row.select_one("td:nth-child(3) a")
        if not name_tag: continue
        name = name_tag.get_text(strip=True)
        
        download_btn = row.select_one("a[title^='Download file'], a[href*='download/file']")
        href = tag_attr(download_btn, "href") or None
        if not href:
            href = tag_attr(row.select_one("td:nth-child(3) a"), "href") or None
            
        if href:
            href = urljoin(BASE_URL, href)
            
        time_tag = row.find("time")
        modified = time_tag.get_text(strip=True) if time_tag else "-"
        modified_iso = tag_attr(time_tag, "datetime") or None
        size_tag = row.select_one("td:nth-child(4) span")
        size = size_tag.get_text(strip=True) if size_tag else "-"
        
        items.append({
            "type": "file",
            "name": name,
            "url": href,
            "size": size,
            "modified": modified,
            "modified_iso": modified_iso
        })

    return items


async def get_fresh_file_url(cid, filename, current_url=None, session=None, user_id=None):
    """Return a fresh, valid download URL for a given file name browser-less."""
    try:
        logging.info("🔄 Getting fresh URL for: %s in course %s", filename, cid)
        if not current_url:
            current_url = f"{BASE_URL}/dispatch.php/course/files?cid={cid}"

        files = await list_files(cid, current_url, session=session, user_id=user_id)

        # Find file with exact match
        for f in files:
            if f["type"] == "file" and f["name"] == filename:
                url = f["url"]
                if url:
                    normalized_url = _normalize_url(url)
                    logging.info("✅ Found URL for %s: %s", filename, redact_url(normalized_url))
                    return normalized_url

        # If exact match not found, check root page
        logging.info("🔍 File not found in current location, checking root...")
        root_url = f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
        if current_url != root_url:
            files = await list_files(cid, root_url, session=session, user_id=user_id)

            for f in files:
                if f["type"] == "file" and f["name"] == filename:
                    url = f["url"]
                    if url:
                        normalized_url = _normalize_url(url)
                        logging.info("✅ Found URL in root for %s: %s", filename, redact_url(normalized_url))
                        return normalized_url

        logging.warning("❌ File not found anywhere: %s", filename)
        return None

    except Exception as e:
        logging.error("❌ Error getting fresh URL for %s: %s", filename, e)
        return None


# ── recursive ZIP (downloads all files + subfolders) ───────────────────────────
# Downloads stop once this much raw data is collected: Telegram takes 50 MB at
# most, and the whole archive is built in memory.
ZIP_RAW_LIMIT_BYTES = 100 * 1024 * 1024


_zip_slot: Optional[asyncio.Semaphore] = None
_zip_slot_loop: object = None


def _zip_semaphore() -> asyncio.Semaphore:
    """One ZIP is built at a time: each holds up to ZIP_RAW_LIMIT_BYTES in
    memory, so parallel requests could exhaust a small server."""
    global _zip_slot, _zip_slot_loop
    loop = asyncio.get_running_loop()
    if _zip_slot is None or _zip_slot_loop is not loop:
        _zip_slot, _zip_slot_loop = asyncio.Semaphore(1), loop
    return _zip_slot


async def create_recursive_zip(cid, base_url, root_name: str, progress_callback=None, session=None, user_id=None):
    """Download all files (including subfolders) recursively browser-less.
    Stops at ZIP_RAW_LIMIT_BYTES (the returned total_size then exceeds it)
    and compresses off the event loop, so a huge folder can't freeze the bot."""
    # a student's own session only: never someone else's login
    active_session: Any = session or get_user_session(user_id) or (state.global_session if user_id is None else None)
    if active_session is None:
        return BytesIO(), 0, root_name, 0, 0
    async with _zip_semaphore():
        return await _build_zip(cid, base_url, root_name, progress_callback, active_session, user_id)


async def _build_zip(cid, base_url, root_name, progress_callback, active_session, user_id):
    active_folder_urls: set[str] = set()

    async def _crawl(url, path_prefix, collected, empty_folders):
        normalized_url = _normalize_url(url)
        if not normalized_url:
            logging.warning("ZIP skipped folder with an invalid URL in course %s", cid)
            return
        if normalized_url in active_folder_urls:
            logging.warning("ZIP stopped a folder cycle in course %s at %s", cid, redact_url(normalized_url))
            return
        active_folder_urls.add(normalized_url)
        try:
            items = await list_files(cid, normalized_url, session=active_session, user_id=user_id)
            if not items:
                empty_folders.add(path_prefix)
                return

            for it in items:
                if it["type"] == "file":
                    collected.append((it, path_prefix))
                elif it["type"] == "folder":
                    sub_url = _normalize_url(it.get("url"))
                    if not sub_url:
                        logging.warning("ZIP skipped folder %r without a valid URL in course %s",
                                        it.get("name"), cid)
                        continue
                    sub_prefix = posixpath.join(path_prefix, it.get("name") or "folder")
                    await _crawl(sub_url, sub_prefix, collected, empty_folders)
        finally:
            active_folder_urls.remove(normalized_url)

    all_files: list[dict] = []
    empty_folders: set[str] = set()
    await _crawl(base_url, root_name, all_files, empty_folders)
    total = len(all_files)

    if total == 0:
        return BytesIO(), 0, root_name, 0, 0

    buf = BytesIO()
    total_size = 0
    success_count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zipf:
        for folder in sorted(empty_folders):
            if folder and not folder.endswith("/"):
                zipf.writestr(_safe_zip_path(folder) + "/", "")

        for i, (f, folder_path) in enumerate(all_files, start=1):
            filename = f.get("name") or "unnamed"
            # Names come from Stud.IP: keep "../" or absolute paths out of the
            # archive so extracting it can't write outside the target folder.
            zip_path = f"{_safe_zip_path(folder_path)}/{_safe_filename(filename)}"

            # Use URL already found during crawl
            file_url = f.get("url")
            if not file_url:
                logging.warning("⚠️ No URL found for %s, skipping.", filename)
                continue

            if total_size > ZIP_RAW_LIMIT_BYTES:
                logging.info("ZIP stopped at %s files: over %s bytes", i - 1, ZIP_RAW_LIMIT_BYTES)
                break
            try:
                file_url = _normalize_url(file_url)
                async with await active_session.get(file_url) as resp:
                    length = resp.content_length
                    if resp.status == 200 and length and total_size + length > ZIP_RAW_LIMIT_BYTES:
                        total_size += length  # too big: counted, not downloaded
                        continue
                    if resp.status == 200:
                        try:
                            content = await read_response_limited(resp, ZIP_RAW_LIMIT_BYTES - total_size)
                        except DownloadTooLargeError:
                            total_size = ZIP_RAW_LIMIT_BYTES + 1
                            logging.info("ZIP stopped at %s files: response exceeded remaining size limit", i - 1)
                            continue
                        total_size += len(content)
                        await asyncio.to_thread(zipf.writestr, zip_path, content)
                        success_count += 1
                    else:
                        logging.warning("⚠️ Failed to download %s (Status: %s)", filename, resp.status)
            except Exception as e:
                logging.warning("❌ Failed to download %s: %s", filename, e)

            if progress_callback:
                await progress_callback(total, i)

    buf.seek(0)
    return buf, total, root_name, success_count, total_size


# ── new and updated file detection ─────────────────────────────────────────────

def _parse_file_date_v2(modified_text: str | None, modified_iso: str | None):
    """
    Parses file modification date from ISO string or text.
    Supports:
    - ISO format (from datetime attribute)
    - German/English standard dates (dd.mm.yyyy, etc.)
    - Relative times (x min ago, vor x Min)
    - Time only (HH:MM -> assumes today)
    """
    # 1. ISO format priority
    if modified_iso:
        try:
            # JSON:API timestamps may include fractional seconds or a trailing
            # Z, neither of which every strptime format below accepts on 3.10.
            value = modified_iso[:-1] + "+00:00" if modified_iso.endswith(("Z", "z")) else modified_iso
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d"):
            try:
                return datetime.strptime(modified_iso, fmt)
            except (ValueError, TypeError):
                pass

    # 2. Text fallback
    s = (modified_text or "").strip()
    if not s:
        return datetime.min

    now = berlin_now()

    # A) Relative times (German & English)
    # "vor 5 Minuten", "vor 2 Std.", "10 minutes ago", "just now", "gerade eben"

    # Simple regex for "x unit ago" pattern
    # Matches: "vor 10 Min", "10 min ago", "vor 1 Std", "1 hour ago"
    # Only a "vor ..." or "... ago" phrase counts: a date like "12 Mai 2025"
    # must not be read as "12 minutes ago".
    m_rel = (re.search(r'\bvor\s+(\d+)\s*(min|std|stunde|h)', s, re.IGNORECASE)
             or re.search(r'\b(\d+)\s*(min|hour|hr|h)\w*\.?\s+ago\b', s, re.IGNORECASE))
    if m_rel:
        val = int(m_rel.group(1))
        if m_rel.group(2).lower() == "min":
            return now - timedelta(minutes=val)
        return now - timedelta(hours=val)

    # "Just now" / "Gerade eben"
    if any(k in s.lower() for k in ["gerade", "just now", "soeben"]) or s.lower() == "now":
        return now

    # B) Time only (HH:MM) -> Assume today
    # e.g. "14:30"
    if re.match(r'^\d{1,2}:\d{2}$', s):
        try:
            t = datetime.strptime(s, "%H:%M").time()
            return datetime.combine(now.date(), t)
        except ValueError:
            pass

    # C) Standard Date Formats
    fmts = (
        "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M",
        "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M",
        "%d.%m.%Y", "%d.%m.%y",
        "%d/%m/%Y", "%d/%m/%y",
        "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d. %b %Y", "%d %b %Y",  # 12. Jan 2025
    )
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass

    return datetime.min


def file_date_sort_key(dt: datetime) -> float:
    """A sortable number for a _parse_file_date_v2 result: listings mix naive
    dates and ISO stamps with an offset, which can't be compared directly.
    Naive dates are Berlin wall time, independent of the VPS timezone."""
    timestamp = file_date_timestamp(dt)
    return float("-inf") if timestamp is None else timestamp


def file_date_timestamp(dt: datetime) -> Optional[float]:
    """POSIX timestamp for a parsed file date; naive values are Berlin-local."""
    if dt == datetime.min:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_BERLIN)
    return dt.timestamp()


def _api_message(m: dict, names: dict) -> dict:
    """A JSON:API message in the shape the scraped overview rows had (plus
    its body), keeping the old read URL so cached entries stay comparable."""
    a = m["attributes"]
    dt = _api_berlin(a.get("mkdate"))
    sender_id = _api_rel_id(m, "sender")
    return {
        "id": m["id"], "title": a.get("subject") or "(No subject)",
        "sender": names.get(sender_id, "Unknown"),
        "sender_id": sender_id,
        "date": dt.strftime("%d/%m/%y %H:%M") if dt else "Unknown",
        "url": f"{BASE_URL}/dispatch.php/messages/read/{m['id']}/rec",
        "body": _api_html_to_text(a.get("message")) or "No content found.",
    }
