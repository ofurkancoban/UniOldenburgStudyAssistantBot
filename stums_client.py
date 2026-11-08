"""StuMS (HISinOne) client layer: the shared SSO session, the request flow
and the page fetchers (study planner, exam windows, registered exams, grades,
tuition, documents) plus the register/deregister action.

Nothing here decides what is new or sends a message; that is the reminder
layer in exam_reminder.py, which re-exports these names for older callers.
Parsing of the fetched pages lives in stums_parsers.py.
"""

import asyncio
import contextlib
import contextvars
import logging
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from studip_utils import tag_attr
from stums_parsers import (
    _extract_action_params,
    _parse_candidate_units,
    _parse_exam_schedule_line,
    _parse_examination_periods,
    _parse_invoice_table_rows,
    _parse_registration_status,
)

_TZ_BERLIN = ZoneInfo("Europe/Berlin")


def berlin_now() -> datetime:
    """Berlin wall-clock time, naive (StuMS dates are naive Berlin-local),
    independent of the server's timezone."""
    return datetime.now(_TZ_BERLIN).replace(tzinfo=None)


STUMS_BASE = "https://stums.uni-oldenburg.de"


SSO_ENTRY_URL = f"{STUMS_BASE}/qisserver/api/identityprovider/redirect/2/login"


STUMS_HOST = "stums.uni-oldenburg.de"


STUMS_START_URL = f"{STUMS_BASE}/qisserver/pages/cs/sys/portal/hisinoneStartPage.faces"


STUMS_VERIFIED_SECONDS = 5 * 60  # how long a verified StuMS login is trusted without re-checking


STUMS_RETRY_SECONDS = 30 * 60  # pause after StuMS refused the login (a new /login retries at once)


class StumsLoginUnavailable(RuntimeError):
    """StuMS did not accept the login. `quiet` is set while the retry pause
    is running, so callers log the failure once instead of every cycle."""

    def __init__(self, message: str, quiet: bool = False):
        super().__init__(message)
        self.quiet = quiet


# Background checks (watcher) respect the retry pause; anything the user
# triggers (a button, a command) always tries StuMS right away.
_stums_background = contextvars.ContextVar("stums_background", default=False)


@contextlib.contextmanager
def background_stums():
    """Mark StuMS calls inside this block as background work."""
    token = _stums_background.set(True)
    try:
        yield
    finally:
        _stums_background.reset(token)


STUDY_PLANNER_URL = (
    f"{STUMS_BASE}/qisserver/pages/startFlow.xhtml?_flowId=studyPlanner-flow"
    "&navigationPosition=hisinoneMeinStudium%2ChisinoneStudyPlanner&recordRequest=true"
)


ENROLLMENT_INFO_URL = (
    f"{STUMS_BASE}/qisserver/pages/cm/exa/enrollment/info/start.xhtml"
    "?_flowId=searchOwnEnrollmentInfo-flow&navigationPosition=hisinoneMeinStudium%2ChisinoneOwnEnrollmentList&recordRequest=true"
)


GRADES_URL = (
    f"{STUMS_BASE}/qisserver/pages/sul/examAssessment/personExamsReadonly.xhtml"
    "?_flowId=examsOverviewForPerson-flow&navigationPosition=hisinoneMeinStudium%2CexamAssessmentForStudent&recordRequest=true"
)


STUDY_SERVICE_URL = (
    f"{STUMS_BASE}/qisserver/pages/cm/stu/studyService/start.xhtml"
    "?_flowId=studyservice-flow&navigationPosition=hisinoneMeinStudium%2ChisinoneStudyservice&recordRequest=true"
)


def _stums_lock(session) -> asyncio.Lock:
    """One StuMS sign-in at a time per user session (the watcher, a button
    press and parallel detail fetches would otherwise each start their own
    SSO handoff and invalidate each other's StuMS JSESSIONID)."""
    return session.stums.lock()


def _stums_verified(session) -> bool:
    verified_at = session.stums.verified_at
    return (not session.explicitly_logged_out
            and time.time() - verified_at < STUMS_VERIFIED_SECONDS
            and bool(session.stums.referer))


def _mark_stums_verified(session, referer: str) -> str:
    session.stums.verified_at = time.time()
    session.stums.referer = referer
    session.stums.blocked_until = 0.0
    return referer


async def _establish_stums_session(session) -> str:
    """Make sure this user's session is signed in to StuMS and return a Referer
    for the following requests (the JSF app rejects deep links without one).

    1. A StuMS login verified in the last STUMS_VERIFIED_SECONDS is reused.
    2. Otherwise the existing StuMS cookie is tried first: the keep-alive loop
       keeps it warm, so most checks need a single request and no SSO round
       trip at all (fewer logins means less risk of the account being flagged).
    3. Only when StuMS shows "Login notwendig" does it run the SSO handoff
       (`sso_handoff`), which reuses the warm NetIQ SSO cookies. If that still
       lands logged out, the stale StuMS cookies are dropped and the handoff
       runs once more.

    After a refusal, background checks pause for STUMS_RETRY_SECONDS; anything
    the user triggers tries again right away. Without a stored password the
    handoff cannot answer a NetIQ password/2FA prompt; the user then has to
    sign in once through the /login Mini App (StuMS button).
    """
    if _stums_verified(session):
        return session.stums.referer
    async with _stums_lock(session):
        if _stums_verified(session):  # another task signed in while we waited
            return session.stums.referer
        blocked_until = session.stums.blocked_until
        if _stums_background.get() and time.time() < blocked_until:
            raise StumsLoginUnavailable(
                f"StuMS refused the login earlier; background checks retry at "
                f"{time.strftime('%H:%M', time.localtime(blocked_until))}", quiet=True,
            )

        known_referer = session.stums.referer or STUMS_START_URL
        if _has_stums_cookie(session):
            try:
                if await _stums_logged_in(session, known_referer):
                    return _mark_stums_verified(session, known_referer)
            except Exception as exc:  # a network blip here just means: do the handoff
                logging.debug("exam_reminder: StuMS pre-check failed: %s", exc)

        for attempt in (1, 2):
            try:
                referer = await session.sso_handoff(SSO_ENTRY_URL, STUMS_HOST)
            except RuntimeError as e:
                if "Interactive login required" in str(e):
                    session.stums.blocked_until = time.time() + STUMS_RETRY_SECONDS
                    raise StumsLoginUnavailable(
                        "StuMS needs a fresh university sign-in (the SSO session expired). "
                        "Sign in again through /login (one sign-in connects Stud.IP and StuMS); background checks "
                        f"retry in {STUMS_RETRY_SECONDS // 60} min."
                    ) from e
                if "Interactive login or logout active" in str(e):
                    raise RuntimeError(f"StuMS SSO handoff failed: {e}") from e
                # Pause background retries after any failed handoff: the same
                # SSO flow would fail again and every attempt is a login try.
                session.stums.blocked_until = time.time() + STUMS_RETRY_SECONDS
                if "stuck at https://" in str(e) and any(
                        h in str(e) for h in ("login.uni-oldenburg.de", "auth.uni-oldenburg.de", "idp.uni-oldenburg.de")):
                    raise StumsLoginUnavailable(
                        "StuMS sign-in stopped at the university login (the SSO session has probably expired). "
                        "Sign in again through /login (one sign-in connects Stud.IP and StuMS); background checks retry in "
                        f"{STUMS_RETRY_SECONDS // 60} min."
                    ) from e
                raise RuntimeError(f"StuMS SSO handoff failed: {e}") from e
            if await _stums_logged_in(session, referer):
                return _mark_stums_verified(session, referer)
            if attempt == 1:
                logging.info("exam_reminder: StuMS session expired; dropping StuMS cookies and signing in again")
                session.session.cookie_jar.clear_domain(STUMS_HOST)
        session.stums.blocked_until = time.time() + STUMS_RETRY_SECONDS
        raise StumsLoginUnavailable(
            "StuMS did not sign this account in: the university login succeeded, but the "
            "StuMS portal still shows you as logged out. Background checks retry in "
            f"{STUMS_RETRY_SECONDS // 60} min."
        )


def _has_stums_cookie(session) -> bool:
    try:
        from yarl import URL
        return bool(session.session.cookie_jar.filter_cookies(URL(STUDY_PLANNER_URL)))
    except Exception:
        return True


async def _stums_logged_in(session, referer: str) -> bool:
    """Whether StuMS treats this session as signed in (checked on the study planner)."""
    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
        final_url = str(getattr(r, "url", "") or "")
    if final_url and STUMS_HOST not in final_url:
        return False  # bounced to the identity provider
    return "Login notwendig" not in html_text


def forget_stums_login(session) -> None:
    """Drop the remembered StuMS verification, e.g. after a page reported "Login notwendig"."""
    session.stums.verified_at = 0.0


async def _fetch_examination_periods(session, referer: str, unit_id: str, period_id: str) -> list[dict]:
    """Fetch one exam unit's detail page and return all of its offered sittings."""
    url = (
        f"{STUMS_BASE}/qisserver/pages/startFlow.xhtml?_flowId=detailView-flow"
        f"&unitId={unit_id}&periodId={period_id}&navigationPosition=hisinoneStudyPlanner"
    )
    async with await session.get(url, allow_redirects=True, headers={"Referer": referer}) as r:
        text = await r.text()

    soup = BeautifulSoup(text, "html.parser")
    body_text = soup.get_text("\n", strip=True)
    return _parse_examination_periods(body_text, now=berlin_now())


async def get_open_exam_registrations(session, failed_codes: Optional[set] = None) -> list[dict]:
    """Return one entry per currently-open exam sitting (a unit may yield more than one,
    e.g. two parallel groups each with their own open registration window).

    Each unit's detail page is fetched independently, so one unit's fetch can
    fail (network blip, StuMS hiccup) without the others; that unit is just
    silently skipped from the result rather than raising. If `failed_codes`
    is given (a set), such units' codes are added to it, so a caller that
    treats "missing from this result" as "no longer open" (e.g.
    check_exam_reminders' cache cleanup) can tell that apart from a real
    closure and avoid re-notifying about it as if it had just reopened.
    """
    referer = await _establish_stums_session(session)

    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while fetching study planner")

    candidates = _parse_candidate_units(html_text)

    results: list[dict] = []
    semaphore = asyncio.Semaphore(3)

    async def process(unit: dict):
        if not unit["unit_id"] or not unit["period_id"]:
            return
        async with semaphore:
            try:
                periods = await _fetch_examination_periods(session, referer, unit["unit_id"], unit["period_id"])
            except Exception as e:
                logging.warning("exam_reminder: failed to fetch detail for %s: %s", unit['code'], e)
                if failed_codes is not None:
                    failed_codes.add(unit["code"])
                return
        for period in periods:
            if period["is_open"]:
                results.append({**unit, **period})

    await asyncio.gather(*(process(u) for u in candidates))
    return results


async def get_registered_exams(session) -> list[dict]:
    """Return the exam units the student is currently registered for (Sign off/Cancel
    button present), i.e. candidates for deregistration."""
    referer = await _establish_stums_session(session)

    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while fetching study planner")

    units = _parse_registration_status(html_text)
    return [u for u in units if u["action_type"] == "deregister"]


async def get_registered_exam_schedule(session) -> list[dict]:
    """Return the student's registered exams with their scheduled date/time, parsed
    from the "Angemeldete Prüfungen" (registered exams) overview page. Unlike
    `get_registered_exams` (from the study planner tree), this page shows the exact
    sitting the student is enrolled in, including its date and, if set, time.
    """
    referer = await _establish_stums_session(session)
    async with await session.get(ENROLLMENT_INFO_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while fetching registered exam schedule")

    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    for table in soup.find_all("table", class_="belegungen"):
        for row in table.find_all("tr"):
            cell0 = row.find("td", class_="column0")
            if not cell0:
                continue
            container = cell0.find("div", class_="whiteSpaceNormal")
            if not container:
                continue

            title_node = container.find(string=True, recursive=False)
            title = title_node.strip() if title_node else ""

            li = container.find("li")
            date_line_node = li.find(string=True, recursive=False) if li else None
            date_line = date_line_node.strip() if date_line_node else ""
            schedule = _parse_exam_schedule_line(date_line)

            exam_form = None
            form_div = li.find("div") if li else None
            if form_div:
                exam_form = form_div.get_text(strip=True).replace("Examinationform:", "").strip()

            examiner = None
            instructor_span = li.find("span") if li else None
            if instructor_span:
                examiner = instructor_span.get_text(strip=True)

            actions_cell = row.find("td", class_="column2")
            details_link = actions_cell.find("a", href=lambda x: x and "detailView-flow" in x) if actions_cell else None
            unit_id = None
            if details_link:
                m = re.search(r"unitId=(\d+)", tag_attr(details_link, "href"))
                unit_id = m.group(1) if m else None

            if not unit_id or not schedule["date"]:
                continue

            results.append({
                "title": title,
                "unit_id": unit_id,
                "exam_form": exam_form,
                "examiner": examiner,
                "date_line": date_line,
                **schedule,
            })

    return results


async def get_all_exam_dates(session) -> list[dict]:
    """Return every exam sitting across the whole curriculum that has a resolvable
    date, regardless of registration status (open, closed, registered, or none of
    those). Unlike `get_registered_exam_schedule`, this is not limited to exams the
    student has actually registered for.
    """
    referer = await _establish_stums_session(session)

    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while fetching study planner")

    units = _parse_registration_status(html_text)

    results: list[dict] = []
    semaphore = asyncio.Semaphore(3)

    async def process(unit: dict):
        if not unit["unit_id"] or not unit["period_id"]:
            return
        async with semaphore:
            try:
                periods = await _fetch_examination_periods(session, referer, unit["unit_id"], unit["period_id"])
            except Exception as e:
                logging.warning("exam_reminder: failed to fetch detail for %s: %s", unit['code'], e)
                return
        for period in periods:
            if period.get("exam_date"):
                results.append({**unit, **period})

    await asyncio.gather(*(process(u) for u in units))
    return results


async def _submit_action(session, soup, page_url: str, action: dict) -> tuple[str, str]:
    """Build and POST the hidden-field payload for one myfaces.oam.submitForm(...)
    action, mirroring what the site's own JS does for a real click. Returns the
    response body and its resulting URL (used as Referer for a following step)."""
    form = soup.find("form", id=action["form_name"])
    if not form:
        raise RuntimeError(f"Could not locate form '{action['form_name']}' on the page.")

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value") or ""
    payload[action["source_id"]] = action["source_id"]
    payload.update(action["params"])

    form_action_url = urljoin(page_url, tag_attr(form, "action"))
    async with await session.post(form_action_url, data=payload, headers={"Referer": page_url}) as r:
        result_text = await r.text()
        result_url = str(r.url)
    return result_text, result_url


def _find_confirm_action(html_text: str, expected_aktion: str) -> Optional[dict]:
    """Registering/deregistering from the study planner tree is a two-step flow:
    clicking Apply/Sign-off-Cancel there navigates to a per-exam confirmation page
    (form id="enrollForm") with its own Apply button that actually finalizes the
    change. Find that real confirm button here, skipping the "Cancel enrollment"
    abort button which carries no belegungsAktion of its own.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form", id="enrollForm")
    if not form:
        return None
    for btn in form.find_all("button"):
        action = _extract_action_params(tag_attr(btn, "onclick"))
        if action and action["params"].get("belegungsAktion") == expected_aktion:
            return action
    return None


async def submit_exam_action(session, unit_id: str, action_type: str) -> dict:
    """Register (anmelden) or deregister (abmelden) for a specific exam unit.

    The study planner's registration state lives in a live Spring WebFlow execution
    tied to the current HTTP session, so this fetches the tree fresh right before
    submitting (rather than reusing anything cached), locates the matching action
    button for `unit_id`, and POSTs the same hidden-field payload the site's own
    client-side JS (myfaces.oam.submitForm) would have sent for a real click.

    This is a two-step flow on the site itself: the tree's Apply/Sign-off-Cancel
    button only navigates to a confirmation page; a second Apply click there is
    what actually finalizes the change, so both steps are performed here.
    """
    if action_type not in ("anmelden", "abmelden"):
        raise ValueError(f"Invalid action_type: {action_type}")
    expected_aktion = "ANMELDUNG" if action_type == "anmelden" else "ABMELDUNG"

    referer = await _establish_stums_session(session)
    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
        page_url = str(r.url)

    soup = BeautifulSoup(html_text, "html.parser")
    tree = soup.find(id=lambda x: x and x.endswith("studyPlannerTree"))
    action = None
    title = None
    if tree:
        table = tree.find("table")
        for row in table.find_all("tr") if table else []:
            node = row.find("div", class_="StudyPlannerPruefungNodeData")
            if not node:
                continue
            details_link = node.find("a", href=lambda x: x and "detailView-flow" in x)
            if not details_link or f"unitId={unit_id}&" not in details_link["href"]:
                continue
            btn = node.find("button", id=lambda x: x and x.endswith(f":{action_type}"))
            if not btn:
                continue
            action = _extract_action_params(tag_attr(btn, "onclick"))
            title_div = node.find("div", class_="unit-title-container")
            title = title_div.get_text(" ", strip=True) if title_div else None
            break

    if not action:
        return {
            "success": False,
            "title": title,
            "message": "Could not find that action on the current page anymore (the registration window may have just changed).",
        }

    try:
        step1_text, step1_url = await _submit_action(session, soup, page_url, action)
    except RuntimeError as e:
        return {"success": False, "title": title, "message": str(e)}

    confirm_action = _find_confirm_action(step1_text, expected_aktion)
    if not confirm_action:
        return {
            "success": False,
            "title": title,
            "message": "Reached the confirmation page but could not find its Apply button; no change was made.",
        }

    confirm_soup = BeautifulSoup(step1_text, "html.parser")
    try:
        result_text, _ = await _submit_action(session, confirm_soup, step1_url, confirm_action)
    except RuntimeError as e:
        return {"success": False, "title": title, "message": str(e)}

    # Re-fetch the tree fresh to verify the change actually took effect, rather
    # than trusting the confirmation page's own response.
    referer2 = await _establish_stums_session(session)
    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer2}) as r:
        verify_text = await r.text()

    result_units = _parse_registration_status(verify_text)
    result_unit = next((u for u in result_units if u["unit_id"] == str(unit_id)), None)

    expected_action = "deregister" if action_type == "anmelden" else "register"
    success = result_unit is not None and result_unit["action_type"] == expected_action

    return {
        "success": success,
        "title": title,
        "status": result_unit["status"] if result_unit else None,
        "message": "Done." if success else "The site did not confirm the change; please check the status manually.",
    }


# The onclick handler is itself embedded in a single-quoted JS string, so its own
# quotes come through backslash-escaped (\') rather than plain.
JSF_AJAX_CALL_RE = re.compile(r"execute\s*:\s*\\?'([^']*)\\?'\s*,\s*render\s*:\s*\\?'([^']*)\\?'")


def _find_span(row, id_suffix: str):
    return row.find("span", id=lambda x: x and x.endswith(id_suffix))


def _span_text(row, id_suffix: str) -> Optional[str]:
    span = _find_span(row, id_suffix)
    if not span:
        return None
    text = span.get_text(strip=True)
    return text or None


async def _expand_grades_tree(session, page_url: str, soup: BeautifulSoup) -> Optional[str]:
    """The grades tree only ships its top few levels in the initial HTML; the rest
    (individual modules, one level below each subject group) is lazy-loaded via a
    JSF ajax partial-request the same way the page's own "Expand all" button
    triggers it. Returns the expanded tree's HTML fragment, or None if the
    "Expand all" control isn't present (nothing to expand).
    """
    form = soup.find("form", id="examsReadonly")
    expand_btn = form.find("button", attrs={"value": "Expand all"}) if form else None
    if not form or not expand_btn:
        return None

    onclick = tag_attr(expand_btn, "onclick")
    m = JSF_AJAX_CALL_RE.search(onclick)
    execute = m.group(1).strip() if m else tag_attr(expand_btn, "id")
    render = m.group(2).strip() if m else ""
    # "@this" is a client-side jsf.ajax.js keyword resolved to the triggering
    # component before the real request is sent; resolve it ourselves too.
    execute = execute.replace("@this", tag_attr(expand_btn, "id"))
    render = render.replace("@this", tag_attr(expand_btn, "id"))
    tree_render_id = next(
        (rid for rid in render.split() if rid.endswith(":ExamOverviewForPersonTreeReadonly")),
        None,
    )
    if not tree_render_id:
        return None

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value") or ""
    payload.update({
        "javax.faces.partial.ajax": "true",
        "javax.faces.source": expand_btn["id"],
        "javax.faces.partial.execute": execute,
        "javax.faces.partial.render": render,
        expand_btn["id"]: expand_btn["id"],
    })

    form_action_url = urljoin(page_url, tag_attr(form, "action"))
    headers = {"Referer": page_url, "Faces-Request": "partial/ajax"}
    async with await session.post(form_action_url, data=payload, headers=headers) as r:
        xml_text = await r.text()

    m = re.search(
        rf'<update id="{re.escape(tree_render_id)}"><!\[CDATA\[(.*?)\]\]></update>',
        xml_text, re.DOTALL,
    )
    return m.group(1) if m else None


async def get_grades(session) -> list[dict]:
    """Return every graded/in-progress module from the "My achievements"
    (Notenspiegel) page: number, title, attempt, grade, credit points, status,
    and grade release date, where available.

    The tree distinguishes "module" rows (a subject's current, latest-attempt
    result) from "examination" rows (one per individual sitting, so a resat
    module has several); only the former are kept, to avoid double-counting a
    module once per attempt. A module with no separate summary row (e.g. one
    outside the main curriculum tree) is kept via its own "examination" row.
    """
    referer = await _establish_stums_session(session)
    async with await session.get(GRADES_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
        page_url = str(r.url)
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while fetching grades")

    soup = BeautifulSoup(html_text, "html.parser")

    fragment = await _expand_grades_tree(session, page_url, soup)
    tree_soup = BeautifulSoup(fragment, "html.parser") if fragment else soup

    results = []
    seen_numbers = set()
    for row in tree_soup.find_all("tr"):
        number = _span_text(row, ":elementnr")
        if not number or not number.isdigit() or number in seen_numbers:
            continue

        icon = row.find("img")
        level = tag_attr(icon, "alt").strip().lower()
        if level not in ("module", "examination"):
            continue  # programme/module-group rows carry no individual result

        seen_numbers.add(number)

        status_span = _find_span(row, ":workstatus")
        status_title = (status_span.get("title") if status_span else None) or ""
        status_code = status_span.get_text(strip=True) if status_span else None

        results.append({
            "number": number,
            "title": _span_text(row, ":unDeftxt"),
            "attempt": _span_text(row, ":attempt"),
            "grade": _span_text(row, ":grade"),
            "credit_points": _span_text(row, ":bonus"),
            "status_code": status_code,
            "status_title": status_title,
            "release_date_text": _span_text(row, ":geplantesFreigabedatum"),
        })

    return results


STUMS_DOCUMENTS = {
    "imma": {
        "label": "📄 Immatrikulationsbescheinigung (+ BAföG)",
        "filename": "Immatrikulationsbescheinigung.pdf",
        "source": "studyservice",
        "button_id": "studyserviceForm:report:reports:reportButtons:jobConfigurationButtons:0:jobConfigurationButtons:0:job2",
        "label_match": "immatrikulationsbescheinigung",
    },
    "verlauf": {
        "label": "📈 Studienverlaufsbescheinigung",
        "filename": "Studienverlaufsbescheinigung.pdf",
        "source": "studyservice",
        "button_id": "studyserviceForm:report:reports:reportButtons:jobConfigurationButtons:0:jobConfigurationButtons:1:job2",
        "label_match": "studienverlaufsbescheinigung",
    },
    "transcript_en": {
        "label": "🇬🇧 Transcript of Records (English)",
        "filename": "Transcript_of_Records_EN.pdf",
        "source": "grades",
        "button_id": "examsReadonly:exaReports:fieldset_exaReports:printReport_2",
        "label_match": "leistungsübersicht englisch",
    },
    "transcript_de": {
        "label": "🇩🇪 Leistungsübersicht (Deutsch)",
        "filename": "Leistungsuebersicht_DE.pdf",
        "source": "grades",
        "button_id": "examsReadonly:exaReports:fieldset_exaReports:printReport_1",
        "label_match": "leistungsübersicht deutsch",
    },
    "grades_en": {
        "label": "🇬🇧 Grade Certificate / Notenbescheinigung (EN)",
        "filename": "Grade_Certificate_EN.pdf",
        "source": "grades",
        "button_id": "examsReadonly:exaReports:fieldset_exaReports:printReport_4",
        "label_match": "notenbescheinigung englisch",
    },
    "grades_de": {
        "label": "🇩🇪 Notenbescheinigung (DE)",
        "filename": "Notenbescheinigung_DE.pdf",
        "source": "grades",
        "button_id": "examsReadonly:exaReports:fieldset_exaReports:printReport_3",
        "label_match": "notenbescheinigung deutsch",
    },
    "reg_exams": {
        "label": "📝 Registered Exams / Angemeldete Prüfungen",
        "filename": "Angemeldete_Pruefungen.pdf",
        "source": "grades",
        "button_id": "examsReadonly:exaReports:fieldset_exaReports:printReport_0",
        "label_match": "angemeldete prüfungen",
    },
}


def _build_jsf_form_payload(form, clicked_id: str, label_match: Optional[str] = None) -> dict:
    """Build a standard JSF form submission payload including the clicked button's
    `name` attribute (which in HISinOne's `studyserviceForm` differs from its `id`)."""
    payload: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("submit", "button", "image", "reset"):
            continue
        if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        payload[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        payload[name] = opt.get("value", "") if opt else ""

    btn = form.find(id=clicked_id)
    if not btn and label_match:
        needle = label_match.lower()
        for candidate in form.find_all(["button", "input"]):
            txt = (candidate.get_text(" ", strip=True) or candidate.get("value") or candidate.get("title") or "").lower()
            if needle in txt:
                btn = candidate
                break

    if btn:
        btn_name = btn.get("name") or btn.get("id") or clicked_id
        payload[btn_name] = btn.get("value", "")
    else:
        payload[clicked_id] = ""
    return payload


async def download_stums_document(session, doc_key: str) -> tuple[bytes, str]:
    """Download an official PDF certificate from StuMS (`STUMS_DOCUMENTS[doc_key]`).

    Returns `(pdf_bytes, filename)` or raises `RuntimeError` on failure.
    """
    spec = STUMS_DOCUMENTS.get(doc_key)
    if not spec:
        raise RuntimeError(f"Unknown document key: {doc_key}")

    referer = await _establish_stums_session(session)

    if spec["source"] == "grades":
        async with await session.get(GRADES_URL, allow_redirects=True, headers={"Referer": referer}) as r:
            html_text = await r.text()
            page_url = str(r.url)
        if "Login notwendig" in html_text:
            forget_stums_login(session)
            raise RuntimeError("StuMS session expired while opening My Achievements.")

        soup = BeautifulSoup(html_text, "html.parser")
        form = soup.find("form", id="examsReadonly")
        if not form:
            raise RuntimeError("Could not find the reports form on StuMS.")

        payload = _build_jsf_form_payload(form, spec["button_id"], spec.get("label_match"))
        action_url = urljoin(page_url, tag_attr(form, "action"))
        async with await session.post(action_url, data=payload, allow_redirects=True, headers={"Referer": page_url}) as r2:
            raw = await r2.read()
            ctype = (r2.headers.get("Content-Type") or "").lower()
            resp_url = str(r2.url)
    else:
        async with await session.get(STUDY_SERVICE_URL, allow_redirects=True, headers={"Referer": referer}) as r:
            html_text = await r.text()
            page_url = str(r.url)
        if "Login notwendig" in html_text:
            forget_stums_login(session)
            raise RuntimeError("StuMS session expired while opening Student Service.")

        soup = BeautifulSoup(html_text, "html.parser")
        form = soup.find("form", id="studyserviceForm")
        if not form:
            raise RuntimeError("Could not find studyserviceForm on StuMS.")

        # Step 1: Switch to the Reports tab if the report button isn't already rendered
        if not form.find(id=spec["button_id"]):
            tab_payload = _build_jsf_form_payload(form, "studyserviceForm:report_TabBtn", "reports")
            tab_action_url = urljoin(page_url, tag_attr(form, "action"))
            async with await session.post(tab_action_url, data=tab_payload, allow_redirects=True, headers={"Referer": page_url}) as r_tab:
                html_text = await r_tab.text()
                page_url = str(r_tab.url)
            soup = BeautifulSoup(html_text, "html.parser")
            form = soup.find("form", id="studyserviceForm")
            if not form:
                raise RuntimeError("Could not switch to the Reports tab on StuMS.")

        payload = _build_jsf_form_payload(form, spec["button_id"], spec.get("label_match"))
        action_url = urljoin(page_url, tag_attr(form, "action"))
        async with await session.post(action_url, data=payload, allow_redirects=True, headers={"Referer": page_url}) as r2:
            raw = await r2.read()
            ctype = (r2.headers.get("Content-Type") or "").lower()
            resp_url = str(r2.url)

    if raw.startswith(b"%PDF") or "application/pdf" in ctype:
        return raw, spec["filename"]

    # HISinOne Student Service often returns an HTML page with a `rds?state=docdownload&docId=...` redirect/link
    html_after = raw.decode("utf-8", errors="ignore")
    m = re.search(r"(/qisserver/rds\?state=docdownload[^\"'\s<>]+)", html_after)
    if m:
        dl_path = html_after[m.start(1):m.end(1)].replace("&amp;", "&")
        dl_url = urljoin(STUMS_BASE, dl_path)
        async with await session.get(dl_url, allow_redirects=True, headers={"Referer": resp_url}) as r_dl:
            pdf_bytes = await r_dl.read()
        if pdf_bytes.startswith(b"%PDF"):
            return pdf_bytes, spec["filename"]

    raise RuntimeError("StuMS did not return a valid PDF document.")


async def get_tuition_status(session) -> dict:
    """Fetch open and paid semester fee invoices (*Semesterbeitrag* / *Rückmeldung*)
    from StuMS Student Service (`Payments` tab).
    """
    referer = await _establish_stums_session(session)
    async with await session.get(STUDY_SERVICE_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
        page_url = str(r.url)
    if "Login notwendig" in html_text:
        forget_stums_login(session)
        raise RuntimeError("StuMS session expired while opening Student Service.")

    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.find("form", id="studyserviceForm")
    if not form:
        raise RuntimeError("Could not find studyserviceForm on StuMS.")

    # Switch to the Payments tab (`studyserviceForm:billsAndPayment_TabBtn`)
    tab_payload = _build_jsf_form_payload(form, "studyserviceForm:billsAndPayment_TabBtn", "payments")
    action_url = urljoin(page_url, tag_attr(form, "action"))
    async with await session.post(action_url, data=tab_payload, allow_redirects=True, headers={"Referer": page_url}) as r2:
        pay_html = await r2.text()

    pay_soup = BeautifulSoup(pay_html, "html.parser")

    # 1. Open / unpaid invoices
    open_fs = pay_soup.find(id=lambda x: x and x.endswith("fieldSetInvoiceOffen"))
    open_text = open_fs.get_text(" ", strip=True) if open_fs else ""
    open_invoices = _parse_invoice_table_rows(open_fs)
    no_open_flag = (
        "no open payments" in open_text.lower()
        or "keine offenen" in open_text.lower()
    )
    has_open = bool(open_invoices) and not no_open_flag

    # 2. Paid / all invoices
    paid_table = (
        pay_soup.find(id=lambda x: x and x.endswith("dataTableSalesInvoicesOffenTable"))
        or pay_soup.find(id=lambda x: x and x.endswith("fieldSetInvoicePaid"))
    )
    paid_invoices = _parse_invoice_table_rows(paid_table)

    # 3. Current enrollment / reregistration status message if shown on page
    status_banner = ""
    for msg_div in pay_soup.select(".message_info, .box_info, .status_info"):
        t = msg_div.get_text(" ", strip=True)
        if t and len(t) < 240:
            status_banner = t
            break

    return {
        "has_open_payments": has_open,
        "open_invoices": open_invoices,
        "paid_invoices": paid_invoices,
        "status_banner": status_banner,
    }
