"""Precise-time course enrollment for Stud.IP.

Reproduces the confirm-dialog flow (GET the apply dialog for its CSRF
security_token, then POST apply=1&yes=&security_token=... at the exact
target time) without a browser, reusing an authenticated StudIPSession.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def load_pending() -> dict:
    """Load pending fast-enroll jobs keyed by str(chat_id) -> {sem_id -> {sem_id, target_time_str, target_date_str}}."""
    if not os.path.exists(PENDING_FILE):
        return {}
    try:
        with open(PENDING_FILE, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logging.warning("fast_enroll: could not read %s, starting empty", PENDING_FILE, exc_info=True)
        return {}

    # Migrate legacy single-job-per-chat format ({chat_id: {sem_id, ...}}) to
    # the multi-job format ({chat_id: {sem_id: {sem_id, ...}}}).
    migrated = {}
    for chat_id_str, value in data.items():
        if "sem_id" in value and isinstance(value.get("sem_id"), str) and all(
            not isinstance(v, dict) for v in value.values()
        ):
            migrated[chat_id_str] = {value["sem_id"]: value}
        else:
            migrated[chat_id_str] = value
    return migrated


def _save_pending(pending: dict):
    tmp_path = PENDING_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(pending, f)
    os.replace(tmp_path, PENDING_FILE)


def list_pending_jobs(chat_id) -> dict:
    """Return {sem_id: {sem_id, target_time_str, target_date_str}} for one chat."""
    return load_pending().get(str(chat_id), {})


def save_pending_job(chat_id, sem_id: str, target_time_str: str, target_date_str: str):
    pending = load_pending()
    pending.setdefault(str(chat_id), {})[sem_id] = {
        "sem_id": sem_id,
        "target_time_str": target_time_str,
        "target_date_str": target_date_str,
    }
    _save_pending(pending)


def clear_pending_job(chat_id, sem_id: str = None):
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
    downloading the full ~240KB page — this keeps the round-trip time (and thus
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
    apply_url = f"{BASE_URL}/dispatch.php/course/enrolment/apply/{sem_id}"
    async with await session.get(apply_url) as resp:
        html = await resp.text()
    match = TOKEN_RE.search(html)
    if not match:
        raise EnrollError("Could not find security_token in enrolment dialog (course may already be full, closed, or already enrolled).")
    return match.group(1)


async def _submit_enrollment(session, sem_id: str, security_token: str):
    apply_url = f"{BASE_URL}/dispatch.php/course/enrolment/apply/{sem_id}?apply=1"
    payload = {"security_token": security_token, "apply": "1", "yes": ""}
    async with await session.post(apply_url, data=payload) as resp:
        html = await resp.text()
        status = resp.status
    return status, html


def resolve_target(target_time_str: str, target_date_str: str, now: datetime) -> datetime:
    """Parse 'HH:MM:SS' and an optional 'DD.MM.YYYY' into a Berlin-time datetime.

    If target_date_str is given, that exact date+time is used (may be in the past,
    caller should validate). If omitted, returns the next occurrence of that time
    of day (today if still upcoming, otherwise tomorrow)."""
    hh, mm, ss = (int(p) for p in target_time_str.split(":"))
    if target_date_str:
        day, month, year = (int(p) for p in target_date_str.split("."))
        return datetime(year, month, day, hh, mm, ss, tzinfo=TZ_BERLIN)
    candidate = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def run_fast_enroll(session, sem_id: str, target_time_str: str, target_date_str: str = None, on_progress=None):
    """Wait until the target Europe/Berlin date+time then submit the enrollment.

    Returns (success: bool, message: str).
    """
    async def notify(msg: str):
        logging.info("[fast_enroll] %s", msg)
        if on_progress:
            await on_progress(msg)

    now = datetime.now(TZ_BERLIN)
    target = resolve_target(target_time_str, target_date_str, now)
    if target <= now:
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
        remaining = target_ts - local_now_as_site()
        if remaining <= 60:
            break
        await asyncio.sleep(min(remaining - 60, 120))
        offset = await _site_clock_offset(session)

    # Re-sync right before the critical window, then fetch a fresh token.
    while True:
        remaining = target_ts - local_now_as_site()
        if remaining <= TOKEN_REFRESH_LEAD:
            break
        await asyncio.sleep(remaining - TOKEN_REFRESH_LEAD)
    offset = await _site_clock_offset(session)
    await notify(f"Re-synced site clock offset: {offset:+.3f}s")

    try:
        security_token = await _fetch_security_token(session, sem_id)
    except EnrollError as e:
        return False, str(e)
    await notify("Fetched fresh security_token, waiting for exact moment...")

    # Coarse sleep, then spin for the final stretch to minimize drift.
    while True:
        remaining = target_ts - local_now_as_site()
        if remaining <= SPIN_WINDOW:
            break
        await asyncio.sleep(remaining - SPIN_WINDOW)

    while local_now_as_site() < target_ts:
        pass

    status, html = await _submit_enrollment(session, sem_id, security_token)

    title_match = TITLE_RE.search(html)
    page_title = title_match.group(1).strip() if title_match else ""

    if status == 200:
        await notify(f"Submitted (HTTP {status}). Page title: {page_title!r}")
        return True, f"Enrollment request submitted at {target.strftime('%H:%M:%S')}. Response title: {page_title}"
    return False, f"Unexpected HTTP status {status} when submitting enrollment."
