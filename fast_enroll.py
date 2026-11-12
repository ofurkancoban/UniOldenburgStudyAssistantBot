"""Precise-time course enrollment for Stud.IP.

Reproduces the confirm-dialog flow (GET the apply dialog for its CSRF
security_token, then POST apply=1&yes=&security_token=... at the exact
target time) without a browser, reusing an authenticated StudIPSession.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit
from typing import Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from studip_http import UNSPACED, set_request_priority
from studip_utils import tag_attr

BASE_URL = "https://elearning.uni-oldenburg.de"
TZ_BERLIN = ZoneInfo("Europe/Berlin")
PENDING_FILE = "fastenroll_pending.json"

TOKEN_RE = re.compile(r'name="security_token"\s+value="([^"]+)"')
TITLE_RE = re.compile(r'<title>([^<]*)</title>')
CLOCK_RE = re.compile(r'data-timestamp="([^"]+)"')

# How long before the target time to fetch a fresh CSRF token.
TOKEN_REFRESH_LEAD = 5.0
# Busy-wait window: stop sleeping and spin-check the clock this close to target.
SPIN_WINDOW = 0.3


class EnrollError(Exception):
    pass


def _user_action_is_current(user_id: Optional[int]) -> bool:
    """Stop an interactive course mutation when its owner loses access."""
    if user_id is None:
        return True
    from unibot import db
    from unibot.auth import ACTIVE, is_user_allowed, lifecycle_state
    return (db.private_context_is_current(user_id) and is_user_allowed(user_id)
            and lifecycle_state(user_id) == ACTIVE)


def load_pending() -> dict:
    """Load pending fast-enroll jobs keyed by str(chat_id) -> {sem_id -> {sem_id, target_time_str, target_date_str}}."""
    from unibot import (
        db,  # the bot's SQLite document store (imports the old JSON file once)
    )
    if not db.exists(PENDING_FILE):
        return {}
    try:
        data = db.load(PENDING_FILE)
    except (ValueError, OSError):
        logging.warning("fast_enroll: could not read %s, starting empty", PENDING_FILE, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logging.warning("fast_enroll: %s must contain an object, starting empty", PENDING_FILE)
        return {}

    # Migrate legacy single-job-per-chat format ({chat_id: {sem_id, ...}}) to
    # the multi-job format ({chat_id: {sem_id: {sem_id, ...}}}).
    migrated = {}
    for chat_id_str, value in data.items():
        if not isinstance(value, dict):
            logging.warning("fast_enroll: ignoring malformed pending jobs for chat %r", chat_id_str)
            continue
        if "sem_id" in value and isinstance(value.get("sem_id"), str) and all(
            not isinstance(v, dict) for v in value.values()
        ):
            migrated[chat_id_str] = {value["sem_id"]: value}
        else:
            jobs = {sem_id: job for sem_id, job in value.items() if isinstance(job, dict)}
            if len(jobs) != len(value):
                logging.warning("fast_enroll: ignored malformed jobs for chat %r", chat_id_str)
            if jobs:
                migrated[chat_id_str] = jobs
    return migrated


def _save_pending(pending: dict):
    from unibot import db
    db.save(PENDING_FILE, pending)


def list_pending_jobs(chat_id) -> dict:
    """Return {sem_id: {sem_id, target_time_str, target_date_str}} for one chat."""
    return load_pending().get(str(chat_id), {})


def save_pending_job(chat_id, sem_id: str, target_time_str: str, target_date_str: Optional[str]):
    pending = load_pending()
    pending.setdefault(str(chat_id), {})[sem_id] = {
        "sem_id": sem_id,
        "target_time_str": target_time_str,
        "target_date_str": target_date_str,
    }
    _save_pending(pending)


def clear_pending_job(chat_id, sem_id: Optional[str] = None):
    """Remove one job (by sem_id) for a chat, or all of that chat's jobs if sem_id is None."""
    pending = load_pending()
    chat_key = str(chat_id)
    if chat_key not in pending:
        return
    if sem_id is None:
        del pending[chat_key]
        _save_pending(pending)
        return
    if pending[chat_key].pop(sem_id, None) is not None:
        if not pending[chat_key]:
            del pending[chat_key]
        _save_pending(pending)


CLOCK_RE_BYTES = re.compile(rb'data-timestamp="([^"]+)"')
# Stop reading the response as soon as we've seen this many bytes without a
# match; the clockip template appears well within the first ~16KB in practice.
CLOCK_READ_CAP = 32_768

# How many quick round-trips to sample per offset measurement, and how many of
# the lowest-latency ones to keep (their symmetric-RTT assumption holds best).
OFFSET_SAMPLES = 5
OFFSET_KEEP_BEST = 2


async def _measure_offset_once(session) -> tuple:
    """One round-trip: return (offset, round_trip_time). Reads only as much of
    the response as needed to find the clock, then aborts the rest instead of
    downloading the full ~240KB page - this keeps the round-trip time (and thus
    the offset estimate) as close to a pure network measurement as possible."""
    t0 = time.time()
    resp = await (await session.get(f"{BASE_URL}/dispatch.php/start"))
    try:
        buf = b""
        match = None
        async for chunk in resp.content.iter_chunked(4096):
            buf += chunk
            match = CLOCK_RE_BYTES.search(buf)
            if match or len(buf) >= CLOCK_READ_CAP:
                break
        t1 = time.time()
    finally:
        resp.close()  # abandon any unread remainder rather than draining it

    if not match:
        raise EnrollError("Could not read the site's clock widget (page layout may have changed).")

    site_dt = datetime.fromisoformat(match.group(1).decode())
    if site_dt.tzinfo is None:
        # The clock widget is rendered in Stud.IP's Europe/Berlin timezone.
        # A bare ISO value is a Berlin wall time, not the VPS's local time.
        site_dt = site_dt.replace(tzinfo=TZ_BERLIN)
    request_midpoint = (t0 + t1) / 2
    return site_dt.timestamp() - request_midpoint, t1 - t0


async def _site_clock_offset(session) -> float:
    """Return (site_clock_time - local_time) in seconds, read from Stud.IP's own
    rendered clock widget (data-timestamp on the #clockip template) rather than
    the HTTP Date header, so we track the exact clock shown on the site.

    Takes several quick back-to-back samples and averages the ones with the
    lowest round-trip time, since a fast round trip is the best evidence that
    the request/response network delay was symmetric (the assumption the
    midpoint estimate relies on)."""
    samples = []
    for _ in range(OFFSET_SAMPLES):
        offset, rtt = await _measure_offset_once(session)
        samples.append((rtt, offset))
    samples.sort(key=lambda s: s[0])
    best = samples[:OFFSET_KEEP_BEST]
    return sum(offset for _, offset in best) / len(best)


async def _fetch_security_token(session, sem_id: str) -> str:
    """Fetch the Apply dialog's security_token.

    The apply URL redirects rather than showing a dialog in two cases that both
    need to be told apart from "genuinely open": already enrolled (redirects to
    course/overview, which happens to contain an unrelated security_token of its
    own - a plain page-wide token search would false-positive as still open) and
    not currently enrollable (redirects to course/details with no apply form).
    Only the actual Apply form's own token is used, never one from elsewhere on
    the page.
    """
    apply_url = f"{BASE_URL}/dispatch.php/course/enrolment/apply/{sem_id}"
    async with await session.get(apply_url, allow_redirects=True) as resp:
        html = await resp.text()
        final_url = str(resp.url)

    if "course/overview" in final_url:
        raise EnrollError("Already enrolled in this course.")

    soup = BeautifulSoup(html, "html.parser")
    apply_form = next(
        (f for f in soup.find_all("form") if "enrolment/apply" in (f.get("action") or "")),
        None,
    )
    if apply_form is None:
        raise EnrollError("Could not find security_token in enrolment dialog (course may already be full, closed, or already enrolled).")

    token_input = apply_form.find("input", attrs={"name": "security_token"})
    if not token_input or not token_input.get("value"):
        raise EnrollError("Could not find security_token in enrolment dialog (course may already be full, closed, or already enrolled).")
    return tag_attr(token_input, "value")


async def _submit_enrollment(session, sem_id: str, security_token: str):
    apply_url = f"{BASE_URL}/dispatch.php/course/enrolment/apply/{sem_id}?apply=1"
    payload = {"security_token": security_token, "apply": "1", "yes": ""}
    async with await session.post(apply_url, data=payload) as resp:
        html = await resp.text()
        status = resp.status
    return status, html


def resolve_target(target_time_str: str, target_date_str: Optional[str], now: datetime) -> datetime:
    """Parse 'HH:MM:SS' and an optional 'DD.MM.YYYY' into a Berlin-time datetime.

    If target_date_str is given, that exact date+time is used (may be in the past,
    caller should validate). If omitted, returns the next occurrence of that time
    of day (today if still upcoming, otherwise tomorrow)."""
    hh, mm, ss = (int(p) for p in target_time_str.split(":"))
    if target_date_str:
        day, month, year = (int(p) for p in target_date_str.split("."))
        candidate = datetime(year, month, day, hh, mm, ss, tzinfo=TZ_BERLIN)
    else:
        local_now = now.replace(tzinfo=TZ_BERLIN) if now.tzinfo is None else now.astimezone(TZ_BERLIN)
        now_ts = local_now.timestamp()
        # Compare instants, not same-zone datetime values: Python compares
        # their wall-clock fields and ignores `fold`, so 02:30 fold=0 can
        # appear later than 02:15 fold=1 even though it happened an hour ago.
        # Try both occurrences on today and tomorrow, keeping only real wall
        # times strictly in the future, then take the nearest instant.
        for day_offset in (0, 1):
            target_day = local_now.date() + timedelta(days=day_offset)
            candidates = []
            for fold in (0, 1):
                option = datetime(target_day.year, target_day.month, target_day.day, hh, mm, ss,
                                  tzinfo=TZ_BERLIN, fold=fold)
                round_trip = option.astimezone(timezone.utc).astimezone(TZ_BERLIN)
                if (round_trip.replace(tzinfo=None) == option.replace(tzinfo=None)
                        and option.timestamp() > now_ts):
                    candidates.append(option)
            if candidates:
                return min(candidates, key=lambda option: option.timestamp())
        raise ValueError("target time does not exist in Europe/Berlin")
    # ZoneInfo permits constructing a wall time that never occurred during
    # the spring DST jump. Reject it instead of silently scheduling at the
    # different instant produced by datetime.timestamp(). For the repeated
    # hour in autumn, fold=0 deliberately selects its first occurrence.
    round_trip = candidate.astimezone(timezone.utc).astimezone(TZ_BERLIN)
    if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        if target_date_str:
            raise ValueError("target time does not exist in Europe/Berlin")
        # A time-only request means the next occurrence. If today's wall
        # time is skipped by the spring clock change, its next occurrence is
        # tomorrow at the requested wall time.
        candidate += timedelta(days=1)
        round_trip = candidate.astimezone(timezone.utc).astimezone(TZ_BERLIN)
        if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            raise ValueError("target time does not exist in Europe/Berlin")
    return candidate


async def run_fast_enroll(session, sem_id: str, target_time_str: str, target_date_str: Optional[str] = None,
                         on_progress=None, user_id: Optional[int] = None):
    """Wait until the target Europe/Berlin date+time then submit the enrollment.

    Returns (success: bool, message: str).
    """
    def account_is_active() -> bool:
        if user_id is None:
            return True
        from unibot.auth import ACTIVE, is_user_allowed, lifecycle_state
        from unibot import db
        return (db.private_context_is_current(user_id) and is_user_allowed(user_id)
                and lifecycle_state(user_id) == ACTIVE)

    if not account_is_active():
        return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."

    # clock sampling and the submit itself must not wait for request spacing
    set_request_priority(UNSPACED)

    async def notify(msg: str):
        logging.info("[fast_enroll] %s", msg)
        if on_progress:
            await on_progress(msg)

    now = datetime.now(TZ_BERLIN)
    target = resolve_target(target_time_str, target_date_str, now)
    if target.timestamp() <= now.timestamp():
        return False, f"Target time {target.isoformat()} is already in the past."
    await notify(f"Scheduled enrollment for sem_id={sem_id} at {target.isoformat()}")

    target_ts = target.timestamp()

    def local_now_as_site() -> float:
        return time.time() + offset

    # Coarse wait, re-measuring the site's own clock periodically so we track
    # its drift rather than trusting a single early reading.
    offset = await _site_clock_offset(session)
    await notify(f"Site clock offset: {offset:+.3f}s")
    while True:
        if not account_is_active():
            return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."
        remaining = target_ts - local_now_as_site()
        if remaining <= 60:
            break
        await asyncio.sleep(min(remaining - 60, 120))
        offset = await _site_clock_offset(session)

    # Re-sync right before the critical window, then fetch a fresh token.
    while True:
        if not account_is_active():
            return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."
        remaining = target_ts - local_now_as_site()
        if remaining <= TOKEN_REFRESH_LEAD:
            break
        await asyncio.sleep(remaining - TOKEN_REFRESH_LEAD)
    offset = await _site_clock_offset(session)
    await notify(f"Re-synced site clock offset: {offset:+.3f}s")
    if not account_is_active():
        return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."

    try:
        security_token = await _fetch_security_token(session, sem_id)
    except EnrollError as e:
        return False, str(e)
    if not account_is_active():
        return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."
    await notify("Fetched fresh security_token, waiting for exact moment...")

    # Coarse sleep, then spin for the final stretch to minimize drift.
    while True:
        if not account_is_active():
            return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."
        remaining = target_ts - local_now_as_site()
        if remaining <= SPIN_WINDOW:
            break
        await asyncio.sleep(remaining - SPIN_WINDOW)

    # a 1 ms sleep instead of a busy loop: the loop spun the one event loop
    # for up to SPIN_WINDOW, stalling every other student's request meanwhile
    while local_now_as_site() < target_ts:
        await asyncio.sleep(min(0.001, max(0.0, target_ts - local_now_as_site())))

    # /stop and re-auth state changes can happen while the task is waiting;
    # never submit the enrollment after the account has stopped being active.
    if not account_is_active():
        return False, "Fast enroll paused because your account needs attention. Sign in and schedule it again."

    status, html = await _submit_enrollment(session, sem_id, security_token)
    if not account_is_active():
        # The POST may already have reached Stud.IP, so report an uncertain
        # result to callers without making another account-scoped request.
        return False, "Account paused during submission; check the course list manually."

    title_match = TITLE_RE.search(html)
    page_title = title_match.group(1).strip() if title_match else ""

    if status == 200:
        await notify(f"Submitted (HTTP {status}). Page title: {page_title!r}; checking the result.")
        still_open = await _is_enrollment_open(session, sem_id)
        if still_open is False:
            return True, f"Enrolled successfully at {target.strftime('%H:%M:%S')} (Europe/Berlin)."
        if still_open is True:
            return False, "The course is still open for enrollment; Stud.IP did not confirm the request. Please check your course list."
        return False, "The request was submitted, but Stud.IP did not confirm the result. Please check your course list manually."
    return False, f"Unexpected HTTP status {status} when submitting enrollment."


# ── Browse & enroll (immediate, within the student's own degree programme) ─────
STUDENTMODULE_URL = f"{BASE_URL}/plugins.php/studienmodulplugin/studentmodule"


async def get_semester_options(session) -> list:
    """Return [(semester_id, label), ...] from the student's module overview page
    (Stud.IP's own degree-programme module directory), newest first. Semesters
    starting before 2022 are dropped - old enough to be irrelevant clutter in
    every semester picker (browse/enroll and sign-out)."""
    async with await session.get(STUDENTMODULE_URL) as r:
        html = await r.text()
        if r.status != 200:
            raise EnrollError(f"Could not load the semester list (HTTP {r.status}).")
    if "loginform" in html.lower() or "login notwendig" in html.lower():
        raise EnrollError("Stud.IP session expired while loading semesters.")
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", attrs={"name": "semester_id"})
    if not select:
        raise EnrollError("Could not find the semester selector on the Stud.IP page.")
    options = [(opt.get("value"), opt.get_text(strip=True)) for opt in select.find_all("option") if opt.get("value")]

    def starts_before_2022(label: str) -> bool:
        start = semester_start(label)
        return start is not None and start.year < 2022

    return [(sid, label) for sid, label in options if not starts_before_2022(label)]


# German and English Stud.IP labels: "Sommersemester 2026", "Winter semester 2026/27", "SoSe 26", "WS 2026/27"
_SEMESTER_LABEL_RE = re.compile(r"\b(sommer|summer|winter|sose|wise|ss|ws)\w*\.?\s*(?:semester\s*)?(\d{2,4})", re.IGNORECASE)


def semester_start(label: str):
    """The date a semester label's term starts (summer: 1 April, winter:
    1 October of its first year), or None when the label isn't one."""
    m = _SEMESTER_LABEL_RE.search(label or "")
    if not m:
        return None
    year = int(m.group(2))
    if year < 100:
        year += 2000
    summer = m.group(1).lower().startswith(("sommer", "summer", "sose", "ss"))
    return datetime(year, 4 if summer else 10, 1).date()


SUMMER_ICON = "☀️"
WINTER_ICON = "❄️"


def semester_display(label: str) -> str:
    """A semester label for buttons and messages, with a sun for a summer
    and a snowflake for a winter term. The stored label stays as Stud.IP
    gives it."""
    start = semester_start(label)
    if start is None or not label:
        return label
    return f"{SUMMER_ICON if start.month == 4 else WINTER_ICON} {label}"


def current_semester_id(options: list, today=None) -> Optional[str]:
    """The id of the semester running today among get_semester_options()'s
    [(id, label)]: the latest one that has already started. The list is
    newest first and Stud.IP offers semesters years ahead, so its first entry
    is a future term, not the current one. Falls back to the first entry
    when no label can be read."""
    today = today or datetime.now(ZoneInfo("Europe/Berlin")).date()
    started = [(start, sid) for sid, label in options
               if (start := semester_start(label)) is not None and start <= today]
    if started:
        return max(started)[1]
    return options[0][0] if options else None


def _parse_module_links(html: str) -> list:
    """Extract (module_label, verzeichnis_url) pairs from the student module
    overview page: each module with an offering in the selected semester links
    to its course directory (title text is already "code - name")."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    results = []
    for row in table.find_all("tr"):
        for a in row.find_all("a"):
            href = a.get("href") or ""
            if "veranstaltungsverzeichnis/verzeichnis/overview" in href:
                results.append((a.get_text(strip=True), href))
    return results


MODULE_COMPONENT_TYPE_RE = re.compile(r"course/details\?sem_id=([a-f0-9]{32})")

# Each module's overview table has one row per component type (a module can
# require e.g. both a Vorlesung *and* a Seminar/Übung, each its own separate
# course with its own sem_id and its own enrolment) - the type label lives in
# that row's first cell, right next to the component's course link(s).
COMPONENT_TYPE_TRANSLATIONS = {
    "Vorlesung": "Lecture",
    "Übung": "Exercise",
    "Praktikum": "Practical",
    "Tutorium": "Tutorial",
    "Kolloquium": "Colloquium",
    "Projekt": "Project",
    "Exkursion": "Excursion",
    "Vorlesung oder Seminar": "Lecture/Seminar",
    "Vorlesung ggf. mit Übung": "Lecture (+Exercise)",
}


async def _fetch_module_courses(session, verzeichnis_url: str) -> list:
    """Return [(component_type, course_title, sem_id, has_open_access), ...]
    of the actual course instance(s) offered under one module (e.g. its
    Lecture and, separately, its Seminar/Exercise), in the semester the
    verzeichnis link points to. Each is its own independent enrolment.

    has_open_access reflects the row's own "Uneingeschränkter Zugang"
    (unrestricted access) badge - a green status icon Stud.IP already shows
    per component on this listing page - used by get_open_courses() to tell
    which components offer self-service enrolment at all, entirely from this
    one already-fetched, read-only page. This deliberately replaces an older
    approach that checked each course's Apply dialog directly: for some
    modules that GET wasn't side-effect-free - merely checking could complete
    the enrolment for that course, so browsing "open courses" could silently
    enrol you. Reading this page's own badge instead makes browsing wholly
    read-only; only the explicit "Enroll" -> "Yes" tap ever posts anything.
    """
    async with await session.get(verzeichnis_url) as r:
        html = await r.text()
        if r.status != 200:
            raise EnrollError(f"Could not load a module's courses (HTTP {r.status}).")
    if "loginform" in html.lower() or "login notwendig" in html.lower():
        raise EnrollError("Stud.IP session expired while loading module courses.")
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise EnrollError("The module page did not contain its expected course listing.")

    seen = set()
    results = []
    for table in tables:
        for row in table.find_all("tr"):
            tds = row.find_all("td", recursive=False)
            if len(tds) != 3:
                continue  # only component rows have this exact shape
            component_type = tds[0].get_text(strip=True)
            if not component_type:
                continue
            if component_type in COMPONENT_TYPE_TRANSLATIONS:
                component_type = COMPONENT_TYPE_TRANSLATIONS[component_type]
            elif len(component_type) > 25 or ":" in component_type:
                # An occasional row's first cell picks up adjacent, unrelated
                # text (e.g. a merged/irregular table layout for that module)
                # rather than a clean type label - fall back to a generic one
                # instead of showing the garbled text.
                component_type = "Component"
            has_open_access = "Uneingeschränkter Zugang" in tds[1].get_text(" ", strip=True)
            for a in tds[1].find_all("a", href=True):
                m = MODULE_COMPONENT_TYPE_RE.search(tag_attr(a, "href"))
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    results.append((component_type, a.get_text(strip=True), m.group(1), has_open_access))
    return results


async def _is_enrollment_open(session, sem_id: str) -> Optional[bool]:
    """A course's Apply dialog only contains a security_token while its
    enrolment period is actually open (and the student isn't already
    enrolled/it isn't full). True means still open, False means the explicit
    already-enrolled page was reached, and None means the result is unknown."""
    try:
        await _fetch_security_token(session, sem_id)
        return True
    except EnrollError as e:
        # Only the explicit course-overview redirect proves that the student
        # is enrolled. A missing form can also mean a closed/full course or an
        # expired login, so it cannot safely be treated as success.
        if str(e).startswith("Already enrolled in this course"):
            return False
        return None
    except Exception as e:
        logging.warning("fast_enroll: open-check failed for %s: %s", sem_id, e)
        return None


async def _get_module_course_candidates(session, semester_id: Optional[str] = None) -> dict:
    """Walk the student's own degree-programme modules for the given semester
    and return every course component found under them, keyed by sem_id:
    {sem_id: {"modules": [...], "type": "Lecture"/"Exercise"/"Seminar"/...,
    "course": "..."}}. Shared by get_open_courses (which further filters to
    ones still open) and get_course_type_map (which needs the type label for
    courses regardless of open/closed status, e.g. already-enrolled ones).
    """
    params = {"semester_id": semester_id} if semester_id else None
    async with await session.get(STUDENTMODULE_URL, params=params) as r:
        html = await r.text()
        if r.status != 200:
            raise EnrollError(f"Could not load your modules (HTTP {r.status}).")
    if "loginform" in html.lower() or "login notwendig" in html.lower():
        raise EnrollError("Stud.IP session expired while fetching your modules")

    module_links = _parse_module_links(html)

    # Dedupe by sem_id while collecting: the same course can be a valid elective
    # for more than one module and would otherwise show up once per module it
    # satisfies. Keep every module it's listed under (comma-joined) instead of
    # only the first, so nothing is silently dropped.
    candidates: dict[str, dict] = {}  # sem_id -> {"modules": [...], "type": ..., "course": ..., "has_open_access": ...}
    semaphore = asyncio.Semaphore(3)
    failed_modules = []

    async def fetch_one_module(module_label, verzeichnis_url):
        async with semaphore:
            try:
                courses = await _fetch_module_courses(session, verzeichnis_url)
            except Exception as e:
                failed_modules.append(module_label)
                logging.warning("fast_enroll: failed to fetch courses for %s: %s", module_label, e)
                return
        for component_type, course_title, sem_id, has_open_access in courses:
            entry = candidates.setdefault(sem_id, {
                "modules": [], "type": component_type, "course": course_title, "has_open_access": has_open_access,
            })
            if module_label not in entry["modules"]:
                entry["modules"].append(module_label)

    await asyncio.gather(*(fetch_one_module(label, url) for label, url in module_links))
    if failed_modules:
        raise EnrollError(f"Could not read {len(failed_modules)} of your modules; the course list may be incomplete.")
    return candidates


async def get_course_type_map(session, semester_id: Optional[str] = None) -> dict:
    """Return {sem_id: "Lecture"/"Exercise"/"Seminar"/...} for every course
    component found under the student's own degree-programme modules for the
    given semester, regardless of whether it's still open for enrolment -
    used to label already-enrolled courses (e.g. in the sign-out list) by
    which component they are.
    """
    candidates = await _get_module_course_candidates(session, semester_id)
    return {sem_id: entry["type"] for sem_id, entry in candidates.items()}


async def get_open_courses(session, semester_id: Optional[str] = None) -> list:
    """Return every course, within the student's own degree-programme modules,
    that offers self-service enrolment for the given semester (or the page's
    default/current semester if omitted) - determined purely by that
    "Uneingeschränkter Zugang" (unrestricted access) badge the module listing
    page already shows per component (see _fetch_module_courses), never by
    probing each course's own Apply dialog.

    Each item: {"module": "wir823 - International Finance...", "type": "Lecture",
    "course": "...", "sem_id": "..."}. A module that requires both a Lecture and
    a separate Seminar/Exercise yields one item per component - each is its own
    independent enrolment.

    This does NOT exclude courses the student is already enrolled in - that
    needs the student's own course list, which lives outside this module
    (studip_bot.py's list_courses()); callers should cross-filter the result
    against it themselves. Enrolling in an already-enrolled course via
    enroll_now() still fails safely on its own (its Apply-dialog check
    detects "already enrolled" and refuses to submit), so an unfiltered
    result here is a display nuisance at worst, never a real double-enrol.
    """
    candidates = await _get_module_course_candidates(session, semester_id)

    return [
        {
            "module": " / ".join(entry["modules"]),
            "type": entry["type"],
            "course": entry["course"],
            "sem_id": sem_id,
        }
        for sem_id, entry in candidates.items()
        if entry["has_open_access"]
    ]


async def enroll_now(session, sem_id: str, user_id: Optional[int] = None) -> tuple:
    """Enroll immediately (no scheduling) by reusing the same Apply-dialog flow
    run_fast_enroll() uses. Returns (success: bool, message: str).

    Success is confirmed by re-checking the Apply dialog afterwards: Stud.IP
    only keeps offering a fresh security_token while enrolment is still
    possible, so its absence now (when it was present a moment ago) means the
    submission actually went through. The response page's own <title> isn't
    used for this - it isn't guaranteed to reflect the outcome (e.g. it can
    pick up an unrelated icon's accessibility label instead of the real page
    title), which reads as confusing/wrong even on a successful enrolment.
    """
    if not _user_action_is_current(user_id):
        return False, "Enrollment cancelled because your account is no longer active."
    set_request_priority(UNSPACED)
    try:
        security_token = await _fetch_security_token(session, sem_id)
    except EnrollError as e:
        return False, str(e)

    if not _user_action_is_current(user_id):
        return False, "Enrollment cancelled because your account is no longer active."

    status, _html = await _submit_enrollment(session, sem_id, security_token)
    if not _user_action_is_current(user_id):
        return False, "Account access changed during submission; check your course list manually."
    if status != 200:
        return False, f"Unexpected HTTP status {status} when submitting enrollment."

    still_open = await _is_enrollment_open(session, sem_id)
    if still_open:
        return False, "The site did not confirm the enrolment - please check your course list manually."
    if still_open is None:
        return False, "The enrollment request was submitted, but its result could not be verified. Please check your course list manually."
    return True, "Enrolled successfully."


async def decline_course(session, cid: str, user_id: Optional[int] = None) -> tuple:
    """Sign out of (withdraw from) a course the student is currently enrolled in.

    Unlike enrolment, this is a two-step server-rendered confirmation flow (not
    a modal dialog fetched separately): GET my_courses/decline/<cid>?cid=<cid>
    &cmd=suppose_to_kill returns a page (redirected to my_courses/index, but
    with a "Please confirm action" form embedded in the body) containing a
    <form action=".../decline/<cid>?cmd=kill&studipticket=<ticket>"> with
    hidden security_token/cmd/studipticket fields and a submit button
    name="yes". A plain GET to the *first* URl alone never withdraws anything
    - that only requests the confirmation; the actual withdrawal happens on
    the POST to the second (cmd=kill) URL with those fields plus yes="".
    Confirmed live: the studipticket is single-use/short-lived per GET, so it
    must be freshly fetched right before the POST, not cached.

    Returns (success: bool, message: str).
    """
    if not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{32}", cid):
        return False, "Invalid course id; reopen the enrolled course list and try again."
    if not _user_action_is_current(user_id):
        return False, "Sign-out cancelled because your account is no longer active."
    ask_url = f"{BASE_URL}/dispatch.php/my_courses/decline/{cid}?cid={cid}&cmd=suppose_to_kill"
    referer = f"{BASE_URL}/dispatch.php/course/overview?cid={cid}"
    async with await session.get(ask_url, allow_redirects=True, headers={"Referer": referer}) as resp:
        html = await resp.text()
        if resp.status != 200:
            return False, f"Could not load the sign-out confirmation (HTTP {resp.status})."
    if not _user_action_is_current(user_id):
        return False, "Sign-out cancelled because your account is no longer active."
    if "loginform" in html.lower() or "login notwendig" in html.lower():
        return False, "Stud.IP session expired before signing out. Please sign in again."

    soup = BeautifulSoup(html, "html.parser")
    form = next((f for f in soup.find_all("form") if "cmd=kill" in (f.get("action") or "")), None)
    if form is None:
        return False, "Could not find the sign-out confirmation form (you may already be signed out, or the course doesn't allow self sign-out)."

    raw_action = tag_attr(form, "action")
    action = urljoin(f"{BASE_URL}/", raw_action)
    action_parts = urlsplit(action)
    try:
        action_port = action_parts.port
    except ValueError:
        action_port = -1
    action_query = parse_qs(action_parts.query, keep_blank_values=True)
    if (
        action_parts.scheme.lower() != "https"
        or (action_parts.hostname or "").lower() != "elearning.uni-oldenburg.de"
        or action_port not in (None, 443)
        or action_parts.username is not None
        or action_parts.password is not None
        or action_parts.path.rstrip("/") != f"/dispatch.php/my_courses/decline/{cid}"
        or action_query.get("cmd") != ["kill"]
    ):
        return False, "The sign-out confirmation form had an unexpected destination; no request was sent."
    fields = {inp["name"]: inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
    fields["yes"] = ""

    if not _user_action_is_current(user_id):
        return False, "Sign-out cancelled because your account is no longer active."

    async with await session.post(action, data=fields, allow_redirects=True, headers={"Referer": ask_url}) as resp2:
        status = resp2.status
        html2 = await resp2.text()

    if not _user_action_is_current(user_id):
        return False, "Account access changed during sign-out; check your course list manually."

    if status != 200:
        return False, f"Unexpected HTTP status {status} when confirming sign-out."
    if "loginform" in html2.lower() or "login notwendig" in html2.lower():
        return False, "Stud.IP session expired before confirming sign-out. Please sign in again."

    async with await session.get(f"{BASE_URL}/dispatch.php/my_courses") as r3:
        html3 = await r3.text()
        if r3.status != 200:
            return False, f"Could not verify the sign-out (HTTP {r3.status}); please check your course list manually."
    if "loginform" in html3.lower() or "login notwendig" in html3.lower():
        return False, "Could not verify the sign-out because the Stud.IP session expired. Please check your course list manually."
    soup3 = BeautifulSoup(html3, "html.parser")
    still_enrolled = any(
        tag_attr(row, "data-course-id") == cid
        for row in soup3.find_all(["tr", "div", "li"], attrs={"data-course-id": True})
    )
    if not still_enrolled:
        course_href = re.compile(
            rf"(?:course_id=|to=|details\.php\?id=){re.escape(cid)}(?:[&#\"']|$)"
        )
        still_enrolled = any(
            course_href.search(tag_attr(link, "href"))
            for link in soup3.find_all("a", href=True)
        )
    if still_enrolled:
        return False, "The site did not confirm the sign-out - please check your course list manually."
    return True, "Signed out of the course successfully."
