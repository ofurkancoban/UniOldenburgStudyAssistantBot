"""Nightly canary: finds out within a day when a university page changed under the bot.

Two kinds of checks, once a night (after 04:00, when no student is waiting for anything):

* public pages, no account: the StuMS start page, StuMS's sign-in entry, the NetIQ login page, the Stud.IP
  login page and JSON API, and the Mensa menu widget. Each must still look like the page the bot's code was
  written for (status code and a few markers). A page that changed is announced as "the parser may break".
* an administrator's own account, read-only: StuMS sign-in, study planner tree, results page, enrolments page
  and the study service page. These are the pages the exam, grade and fee alerts read, so a change there
  (like the hidden session-timeout form that once made every sign-in look like a failure) shows up here the
  same night instead of when a student misses an alert.

The result is a document the health check reads; the administrators are told through the usual alerts.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp

from studip_http import make_uni_connector
from studip_utils import TZ_BERLIN
from unibot import db

CANARY_STATUS_PATH = "canary_status.json"
CANARY_HOUR = 4
CANARY_STALE_HOURS = 36

UNI_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"}

STUMS_BASE = "https://stums.uni-oldenburg.de"
SPEISEPLAN_URL = "https://sw-oldenburg-spl56.maxmanager.xyz/inc/ajax-php_konnektor.inc.php"

# name -> what the page must still show; "markers" are text the bot's code relies on
PUBLIC_PAGE_CHECKS: dict[str, dict[str, Any]] = {
    "StuMS start page": {"url": f"{STUMS_BASE}/qisserver/pages/cs/sys/portal/hisinoneStartPage.faces",
                         "status": (200,), "markers": ('id="startPage"', "javax.faces.ViewState")},
    "StuMS sign-in entry": {"url": f"{STUMS_BASE}/qisserver/api/identityprovider/redirect/2/login",
                            "status": (302,), "redirect_host": "login.uni-oldenburg.de",
                            "redirect_path": "/nidp/oauth/nam/authz"},
    "NetIQ login page": {"url": "https://login.uni-oldenburg.de/nidp/app/login",
                         "status": (200,), "markers": ("nidp", "getToContent")},
    "Stud.IP login page": {"url": "https://elearning.uni-oldenburg.de/dispatch.php/login",
                           "status": (200,), "markers": ("sso=oidc", 'name="login_ticket"')},
    "Stud.IP JSON API": {"url": "https://elearning.uni-oldenburg.de/jsonapi.php/v1/users/me",
                         "status": (401, 403)},  # without a login it must answer "not allowed", not 404 or 500
}


def _short(text: object, limit: int = 90) -> str:
    return " ".join(str(text).split())[:limit]


def evaluate_public_page(spec: dict, status: int, location: str, body: str) -> tuple[bool, str]:
    """(ok, detail) for one public page, from what it answered."""
    if status not in spec["status"]:
        return False, f"answered HTTP {status}, expected {'/'.join(map(str, spec['status']))}"
    if spec.get("redirect_host"):
        parts = urlsplit(location or "")
        if (parts.hostname or "") != spec["redirect_host"] or spec["redirect_path"] not in parts.path:
            return False, f"redirects to {parts.hostname}{parts.path} instead of {spec['redirect_host']}{spec['redirect_path']}"
    missing = [m for m in spec.get("markers", ()) if m not in body]
    if missing:
        return False, "the page changed, no longer contains: " + ", ".join(missing)
    return True, "OK"


async def check_public_pages(http: aiohttp.ClientSession) -> dict:
    results: dict = {}
    for name, spec in PUBLIC_PAGE_CHECKS.items():
        try:
            async with http.get(spec["url"], headers=UA, allow_redirects="redirect_host" not in spec) as resp:
                body = (await resp.text(errors="replace"))[:600_000]
                results[name] = evaluate_public_page(spec, resp.status, resp.headers.get("Location", ""), body)
        except Exception as e:
            results[name] = (False, f"unreachable ({type(e).__name__})")
    results["Mensa menu widget"] = await _check_speiseplan(http)
    return results


async def _check_speiseplan(http: aiohttp.ClientSession) -> tuple[bool, str]:
    day = datetime.now(TZ_BERLIN).strftime("%Y-%m-%d")
    try:
        async with http.post(SPEISEPLAN_URL, headers=UA, data={
                "func": "make_spl", "locId": "2", "date": day, "lang": "en",
                "startThisWeek": day, "startNextWeek": day}) as resp:
            body = (await resp.text(errors="replace"))[:600_000]
            if resp.status != 200:
                return False, f"answered HTTP {resp.status}"
            # the header rows are there even on a day with no meals (weekend, holiday)
            missing = [m for m in ("gruppenkopf", "preise-head") if m not in body]
            return (False, "the page changed, no longer contains: " + ", ".join(missing)) if missing else (True, "OK")
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"


def _admin_with_login() -> Optional[int]:
    """An active administrator who is signed in: the canary reads StuMS with their account."""
    from unibot.auth import get_user_session, has_stored_login
    for user in db.get_all_users():
        if user.get("role") == "admin" and user.get("is_active", True):
            try:
                if has_stored_login(get_user_session(user["telegram_id"])):
                    return int(user["telegram_id"])
            except Exception:
                logging.debug("Could not look at an administrator's session", exc_info=True)
    return None


async def _stums_check(session, referer: str, url: str, verify) -> tuple[bool, str]:
    from stums_client import _read_stums_page
    try:
        html_text, _final = await _read_stums_page(session, url, referer)
        problem = verify(html_text)
        return (False, problem) if problem else (True, "OK")
    except Exception as e:
        return False, _short(f"{type(e).__name__}: {e}")


def _verify_planner(html_text: str) -> Optional[str]:
    from stums_client import _require_study_planner_tree
    from stums_parsers import _parse_registration_status
    try:
        _require_study_planner_tree(html_text)
        _parse_registration_status(html_text)
    except Exception as e:
        return _short(f"the study planner changed: {e}")
    return None


def _verify_grades(html_text: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    if not BeautifulSoup(html_text, "html.parser").find("form", id="examsReadonly"):
        return "the results page has no form examsReadonly any more"
    return None


def _verify_enrolments(html_text: str) -> Optional[str]:
    from bs4 import BeautifulSoup
    if BeautifulSoup(html_text, "html.parser").find("table", class_="belegungen"):
        return None
    # no table is how an account without registrations looks; the flow marker proves it is the right page
    return None if "searchOwnEnrollmentInfo-flow" in html_text else "the enrolments page changed"


def _verify_study_service(html_text: str) -> Optional[str]:
    return None if "studyservice-flow" in html_text else "the study service page changed"


async def check_account(uid: int) -> dict:
    """StuMS sign-in and the four pages the alerts read, with one administrator's account. Read-only."""
    from stums_client import (
        ENROLLMENT_INFO_URL, GRADES_URL, STUDY_PLANNER_URL, STUDY_SERVICE_URL,
        _establish_stums_session, background_stums,
    )
    from unibot.auth import acting_as, get_user_session
    session = get_user_session(uid)
    results: dict = {}
    with acting_as(uid), background_stums():
        try:
            referer = await _establish_stums_session(session)
        except Exception as e:
            return {"StuMS sign-in": (False, _short(f"{type(e).__name__}: {e}"))}
        results["StuMS sign-in"] = (True, "OK")
        results["Study planner"] = await _stums_check(session, referer, STUDY_PLANNER_URL, _verify_planner)
        results["Results page"] = await _stums_check(session, referer, GRADES_URL, _verify_grades)
        results["Enrolments page"] = await _stums_check(session, referer, ENROLLMENT_INFO_URL, _verify_enrolments)
        results["Study service page"] = await _stums_check(session, referer, STUDY_SERVICE_URL, _verify_study_service)
    return results


def canary_status() -> Optional[dict]:
    try:
        return db.load(CANARY_STATUS_PATH) if db.exists(CANARY_STATUS_PATH) else None
    except Exception:
        logging.warning("Could not read the canary status", exc_info=True)
        return None


async def run_nightly_canary(now: Optional[datetime] = None) -> dict:
    """Run every check, store the result and log what failed. Returns the stored status."""
    now = now or datetime.now(TZ_BERLIN)
    results: dict = {}
    async with aiohttp.ClientSession(connector=make_uni_connector(limit=4, limit_per_host=2),
                                     timeout=UNI_REQUEST_TIMEOUT) as http:
        results.update(await check_public_pages(http))
    uid = _admin_with_login()
    account = "checked"
    if uid is None:
        account = "skipped: no administrator is signed in"
    else:
        results.update(await check_account(uid))
    failed = {name: detail for name, (ok, detail) in results.items() if not ok}
    status = {"at": time.time(), "day": now.date().isoformat(), "ok": not failed, "account": account,
              "results": {name: [ok, detail] for name, (ok, detail) in results.items()}}
    db.save(CANARY_STATUS_PATH, status)
    for name, detail in failed.items():
        logging.warning("Canary: %s: %s", name, detail)
    return status


async def run_canary_if_due(now: datetime) -> Optional[dict]:
    """Once a day, after CANARY_HOUR; a start after that hour with no run today runs it at once."""
    if now.hour < CANARY_HOUR:
        return None
    status = canary_status()
    if status and status.get("day") == now.date().isoformat():
        return None
    return await run_nightly_canary(now)


def canary_health(now_ts: Optional[float] = None) -> Optional[tuple[bool, str]]:
    """(ok, detail) for the health check, None while the canary never ran."""
    status = canary_status()
    if status is None:
        return None
    age_hours = ((time.time() if now_ts is None else now_ts) - float(status.get("at") or 0)) / 3600
    if age_hours > CANARY_STALE_HOURS:
        return False, f"the canary has not run for {age_hours:.0f} hours"
    failed = [f"{name} ({detail})" for name, (ok, detail) in (status.get("results") or {}).items() if not ok]
    if failed:
        return False, "; ".join(failed)[:600]
    note = "" if status.get("account") == "checked" else f" ({status.get('account')})"
    return True, f"{len(status.get('results') or {})} checks passed{note}"
