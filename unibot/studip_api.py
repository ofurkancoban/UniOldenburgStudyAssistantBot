"""Stud.IP JSON:API and HTML access: profile, courses, files, downloads, ZIP export.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import json
import logging
import posixpath
import re
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import studip_oauth
from fast_enroll import get_semester_options
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
from unibot import state
from unibot.auth import current_user_id, get_user_session, is_user_admin, login_studip
from unibot.formatting import clean_html_text
from unibot.storage import (
    load_default_semester,
    save_studip_user_name,
)

from unibot.config import PRIMARY_ADMIN_ID  # noqa: E402
from unibot.preferences import get_pref  # noqa: E402

BASE_URL = "https://elearning.uni-oldenburg.de"


def _normalize_url(u: str | None) -> str | None:
    """Ensure URLs from Stud.IP HTML/JSON are valid."""
    if not u:
        return None
    u = u.strip()
    # If it's just a 32-char hex ID, it's not a valid URL yet
    if len(u) == 32 and re.match(r"^[a-f0-9]{32}$", u.lower()):
        return None

    # normalize schema slashes
    u = re.sub(r"^https:/*", "https://", u)
    # fix double host concatenations
    if "https://elearning.uni-oldenburg.dehttps://" in u:
        u = u.split("https://elearning.uni-oldenburg.de")[-1]
        if not u.startswith("https://"):
            u = "https://elearning.uni-oldenburg.de" + u
    # prefix host if relative
    if not u.startswith("http://") and not u.startswith("https://"):
        if not u.startswith("/"):
            u = "/" + u
        u = f"https://elearning.uni-oldenburg.de{u}"
    return u


def _record_course(cid: str, name: str, user_id: Optional[int] = None) -> None:
    if user_id is not None:
        if user_id not in state.user_courses_map:
            state.user_courses_map[user_id] = {}
        state.user_courses_map[user_id][cid] = name
    if user_id is None or is_user_admin(user_id):
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
    try:
        courses = await _list_courses_api(semester_id, session=session, user_id=user_id)
        if courses:
            for name, cid in courses:
                _record_course(cid, name, user_id=user_id)
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
        try:
            async with await session.get(
                "https://elearning.uni-oldenburg.de/dispatch.php/my_courses/set_semester",
                params={"sem_select": semester_id},
            ) as r:
                await r.text()
        except Exception as e:
            logging.warning("list_courses: failed to switch semester filter: %s", e)

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
        except Exception as e:
            logging.error("Failed to fetch %s: %s", redact_url(url), e)
            continue
            
        # --- Method 1: JSON-based Vuex state (Comprehensive) ---
        # Stud.IP often embeds a JSON payload for its Vue.js components
        json_scripts = re.findall(r'<script\s+type="application/json"[^>]*>(.*?)</script>', html_text, re.S)
        for script_content in json_scripts:
            try:
                content = script_content.strip()
                # Sometimes it's wrapped in a JS call, sometimes raw
                if 'JSON.parse(' in content:
                    inner_match = re.search(r'JSON\.parse\(\s*(["\'])(.*?)\1\s*\)', content, re.S)
                    if inner_match:
                        # Handle escaped JSON string
                        json_str = inner_match.group(2).encode().decode('unicode_escape')
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
                                    _record_course(cid, name, user_id=user_id)
                                    
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
                _record_course(cid, name, user_id=user_id)

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
                        _record_course(cid, name, user_id=user_id)
        
        # If we found courses on the first URL, we might still want to try the second 
        # to ensure we get "all" if the first was restricted to one semester.
    
    if all_courses_dict:
        logging.info("✅ Extracted %s unique courses.", len(all_courses_dict))
        # Ensure the map is updated for other functions
        for cid, name in all_courses_dict.items():
            _record_course(cid, name, user_id=user_id)
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
    try:
        async with await session.get(
            "https://elearning.uni-oldenburg.de/dispatch.php/my_courses/set_semester",
            params={"sem_select": semester_id},
        ) as r:
            await r.text()
    except Exception as e:
        logging.warning("_switch_semester_filter: failed to switch to %s: %s", semester_id, e)


async def _ensure_watched_courses_map(session) -> None:
    """(Re)builds the shared `courses_map` global - used by all three
    background watchers (announcements, new files, forum posts) to decide
    which courses to check - to include the previous semester's courses
    too when that's enabled (see get_watch_previous_semester_enabled /
    /status's toggle). Only rebuilds when the map is empty or the toggle's
    state has changed since the last build, not on every watcher cycle.

    Deliberately resolves "current semester" explicitly (default_semester,
    falling back to the newest entry in get_semester_options) rather than
    relying on a no-arg list_courses() call reflecting "current" - that
    depends on whatever semester some unrelated browsing interaction
    (Files/Enroll/Sign Out's own semester pickers) last left the shared
    session's filter pointed at, which isn't reliable here."""
    owner = getattr(session, "user_id", None) or PRIMARY_ADMIN_ID
    want_prev = bool(get_pref(owner, "watch_previous_semester"))
    if state.courses_map and state._courses_map_built_with_prev == want_prev:
        return

    anchor_id = None
    semester_options = []
    try:
        default = load_default_semester()
        semester_options = await get_semester_options(session)
        anchor_id = default.get("id") or (semester_options[0][0] if semester_options else None)
    except Exception as e:
        logging.warning("_ensure_watched_courses_map: failed to resolve current semester: %s", e)

    courses_list = await list_courses(semester_id=anchor_id) if anchor_id else await list_courses()
    merged = {cid: name for name, cid in courses_list}

    if want_prev and anchor_id and semester_options:
        idx = next((i for i, (sid, _) in enumerate(semester_options) if sid == anchor_id), None)
        if idx is not None and idx + 1 < len(semester_options):
            prev_id, prev_label = semester_options[idx + 1]
            try:
                prev_courses = await list_courses(semester_id=prev_id)
                for name, cid in prev_courses:
                    merged.setdefault(cid, name)
                logging.info("👀 Also watching %s course(s) from %s", len(prev_courses), prev_label)
            except Exception as e:
                logging.warning("_ensure_watched_courses_map: previous-semester fetch failed: %s", e)
            finally:
                # Leave the shared session's semester filter back on the
                # current/anchor semester, not stuck on the previous one.
                await _switch_semester_filter(session, anchor_id)

    state.courses_map = merged
    state._courses_map_built_with_prev = want_prev


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


async def studip_api_get(path: str, params: Optional[dict] = None, session: Any = None, user_id: Optional[int] = None, _retry: bool = True) -> dict:
    """GET one JSON:API resource ("/users/me" or a "/jsonapi.php/..." link).

    The bearer header is added by StudIPSession, so API traffic uses the
    stored OAuth token when there is one and the cookie session otherwise.
    On a 401 it refreshes the token once, then falls back to a full login."""
    session = session or get_user_session(user_id) or await login_studip(user_id=user_id)
    url = BASE_URL + path if path.startswith("/jsonapi.php") else STUDIP_API_URL + path
    async with await session.get(url, params=params) as r:
        status, text = r.status, await r.text()
    if status == 401 and _retry:
        eff_user_id = getattr(session, "user_id", None) or user_id
        use_oauth = studip_oauth.oauth_enabled()
        if use_oauth and studip_oauth.load_tokens(user_id=eff_user_id) and await studip_oauth.get_access_token(force_refresh=True, user_id=eff_user_id):
            return await studip_api_get(path, params, session=session, user_id=user_id, _retry=False)
        # Self-healing silent SSO renewal attempt
        if await session.heal_session():
            return await studip_api_get(path, params, session=session, user_id=user_id, _retry=False)
        await session.login()  # respects the failed-login backoff
        return await studip_api_get(path, params, session=session, user_id=user_id, _retry=False)
    if status != 200:
        raise StudipApiError(f"{path}: HTTP {status}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise StudipApiError(f"{path}: response is not JSON") from e


async def studip_api_get_all(
    path: str,
    params: Optional[dict] = None,
    session: Any = None,
    user_id: Optional[int] = None,
) -> tuple[list, list]:
    """Every page of a JSON:API collection, as (data, included)."""
    params = dict(params or {}, **{"page[limit]": STUDIP_API_PAGE_LIMIT})
    data, included, offset = [], [], 0
    while True:
        params["page[offset]"] = offset
        page = await studip_api_get(path, params, session=session, user_id=user_id)
        data += page.get("data", [])
        included += page.get("included", [])
        offset += STUDIP_API_PAGE_LIMIT
        total = page.get("meta", {}).get("page", {}).get("total", 0)
        if not page.get("data") or offset >= total:
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
                save_studip_user_name(display_name)
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
        return datetime.fromisoformat(iso).astimezone(TZ_BERLIN) if iso else None
    except ValueError:
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
    if state._studip_api_semester_starts is None:
        semesters, _ = await studip_api_get_all("/semesters", session=session, user_id=user_id)
        state._studip_api_semester_starts = {x["id"]: datetime.fromisoformat(x["attributes"]["start"]) for x in semesters}
        state._studip_api_current_semester = next((x["id"] for x in semesters if x["attributes"].get("is-current")), None)
    starts = state._studip_api_semester_starts
    uid_str = await studip_api_user_id(session=session, user_id=user_id)
    courses, _ = await studip_api_get_all(f"/users/{uid_str}/courses", session=session, user_id=user_id)
    target = starts.get(semester_id or state._studip_api_current_semester)
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
            "type": "file", "name": f["attributes"].get("name", "Unknown File"),
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
        return await _list_files_api(cid, folder_url, session=session, user_id=user_id)
    except Exception as e:
        logging.warning("list_files: API failed for %s (%s), falling back to the HTML page", cid, e)
    return await _list_files_html(cid, folder_url, session=session, user_id=user_id)


async def _list_files_html(cid, folder_url=None, session=None, user_id=None):
    """Extract files/folders from Stud.IP file table browser-less."""
    url = _normalize_url(folder_url) if folder_url else f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
    logging.info(">>> list_files called with cid=%s, folder_url=%s => %s", cid, redact_url(folder_url), redact_url(url))
    
    active_session: Any = session or get_user_session(user_id) or state.global_session
    try:
        async with await active_session.get(url) as resp:
            html_text = await resp.text()
    except Exception as e:
        logging.error("Failed to fetch files: %s", e)
        return []

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
                
            if items:
                logging.info("✅ Extracted %s items via JSON from %s", len(items), redact_url(url))
                return items
        except Exception as json_err:
            logging.warning("JSON file extraction failed, falling back to BS4: %s", json_err)

    # --- Method 2: BeautifulSoup Fallback (Legacy) ---
    table = soup.select_one("table.documents")
    if not table:
        return []

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
ZIP_RAW_LIMIT_BYTES = 150 * 1024 * 1024


async def create_recursive_zip(cid, base_url, root_name: str, progress_callback=None, session=None, user_id=None):
    """Download all files (including subfolders) recursively browser-less.
    Stops at ZIP_RAW_LIMIT_BYTES (the returned total_size then exceeds it)
    and compresses off the event loop, so a huge folder can't freeze the bot."""
    # a student's own session only: never someone else's login
    active_session: Any = session or get_user_session(user_id) or (state.global_session if user_id is None else None)
    if active_session is None:
        return BytesIO(), 0, root_name, 0, 0

    async def _crawl(url, path_prefix, collected, empty_folders):
        try:
            items = await list_files(cid, url, session=active_session, user_id=user_id)
        except Exception as e:
            logging.warning("Failed to list %s: %s", path_prefix, e)
            return

        if not items:
            empty_folders.add(path_prefix)
            return

        for it in items:
            if it["type"] == "file":
                collected.append((it, path_prefix))
            elif it["type"] == "folder":
                sub_prefix = posixpath.join(path_prefix, it["name"])
                await _crawl(it["url"], sub_prefix, collected, empty_folders)

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
                        content = await resp.read()
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
    m_rel = re.search(r'(?:vor|ago)?\s*(\d+)\s*(?:Min|min|Std|hour|Stunde|M|h)', s, re.IGNORECASE)
    if m_rel:
        try:
            val = int(m_rel.group(1))
            unit = m_rel.group(0).lower()
            if 'h' in unit or 'std' in unit or 'stunde' in unit:
                return now - timedelta(hours=val)
            else:
                return now - timedelta(minutes=val)
        except ValueError:
            pass

    # "Just now" / "Gerade eben"
    if any(k in s.lower() for k in ["gerade", "just now", "now", "soeben"]):
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
