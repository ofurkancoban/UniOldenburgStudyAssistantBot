"""Exam registration watcher for the StuMS/HISinOne portal (stums.uni-oldenburg.de).

Reuses the existing Stud.IP session (SSO cookies are shared between
elearning.uni-oldenburg.de and stums.uni-oldenburg.de) to read the
"Prüfungsanmeldung" study planner tree, and reports which exams currently
have an open registration window.

Key detail learned while reverse-engineering the page: the study planner
tree shows an "Apply" (anmelden) button for every exam the student is
allowed to register for, even after the registration window has closed.
The actual open/closed state has to be read from the exam's detail page,
which uses a tense difference in its wording:
  - "Enrollment feasible from X until Y"      -> currently open
  - "Enrollment was feasible from X until Y"  -> already closed
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

WA_FORWARD_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])

STUMS_BASE = "https://stums.uni-oldenburg.de"
SSO_ENTRY_URL = f"{STUMS_BASE}/qisserver/api/identityprovider/redirect/2/login"
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

EXAM_CACHE_PATH = "exam_reminder_cache.json"
DEADLINE_THRESHOLDS_DAYS = [7, 3, 1]
EXAM_DATE_THRESHOLDS_DAYS = [3, 1, 0]
EXAM_HOURS_BEFORE_THRESHOLD = 3

ENROLLMENT_LINE_RE = re.compile(r"from\s+([\d/]+),\s*([\d:]+\s*[AP]M).*?until\s+([\d/]+),\s*([\d:]+\s*[AP]M)")
PERIOD_HEADER_RE = re.compile(r"^Examination period:\s*\d+$")
EXAM_DATE_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")
EXAM_TIME_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M$")
WEEKDAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$")
GROUP_LABEL_RE = re.compile(r"\(([^)]*(?:group|Gruppe)[^)]*)\)", re.IGNORECASE)
EXAM_SCHEDULE_LINE_RE = re.compile(r"^(?P<weekday>\w+)\s+(?P<date>\d{1,2}/\d{1,2}/\d{2})\s+(?P<rest>.+)$")
EXAM_SCHEDULE_TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*([AP]M)")

# Page-chrome text that can end up where a room would be when a sitting has no
# room assigned and the block happens to be the last one on the page.
FOOTER_NOISE = {
    "Default language", "Deutsch", "English", "Imprint", "Privacy",
    "Accessibility Statement", "Plain Language", "Sign language", "Sitemap",
    "Logout from this portal", "Close [ESC]",
}


def load_exam_cache() -> dict:
    if os.path.exists(EXAM_CACHE_PATH):
        try:
            with open(EXAM_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load exam reminder cache: {e}")
    return {
        "notified_open": {}, "notified_deadline": {}, "notified_exam_date": {},
        "notified_exam_hours": {}, "known_grades": {},
    }


def save_exam_cache(data: dict) -> None:
    try:
        tmp = EXAM_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, EXAM_CACHE_PATH)
    except Exception as e:
        logging.error(f"Could not save exam reminder cache: {e}")


async def _establish_stums_session(session) -> str:
    """Follow the SSO handoff so the shared Stud.IP cookies are valid on stums.uni-oldenburg.de too.

    Returns the resulting portal URL, used as a Referer for subsequent requests
    (the JSF app rejects deep links without one).

    The cross-domain NetIQ SSO trust (login.uni-oldenburg.de) can expire on its own
    schedule, independently of the Stud.IP elearning session cookie, so a still-valid
    elearning login does not guarantee a plain redirect reaches stums directly. This
    reuses the session's shared NetIQ state machine (`sso_handoff`), which
    transparently falls through to the same credential-entry steps `login()` uses
    (Ecom fields, TOTP/contract page) whenever that happens.
    """
    try:
        return await session.sso_handoff(SSO_ENTRY_URL, "stums.uni-oldenburg.de")
    except RuntimeError as e:
        raise RuntimeError(f"StuMS SSO handoff failed: {e}")


def _parse_candidate_units(html_text: str) -> list[dict]:
    """Return the deduplicated exam units that show an 'Apply' (anmelden) button."""
    soup = BeautifulSoup(html_text, "html.parser")
    tree = soup.find(id=lambda x: x and x.endswith("studyPlannerTree"))
    if not tree:
        return []
    table = tree.find("table")
    if not table:
        return []

    seen: dict[str, dict] = {}
    for row in table.find_all("tr"):
        node = row.find("div", class_="StudyPlannerPruefungNodeData")
        if not node:
            continue

        info_div = node.find("div", class_="unit-information")
        title_div = node.find("div", class_="unit-title-container")
        info_text = info_div.get_text(" ", strip=True) if info_div else ""
        title_text = title_div.get_text(" ", strip=True) if title_div else ""
        code = info_text.split(" ")[0] if info_text else title_text
        if not code or code in seen:
            continue

        anmeld_btn = node.find("button", id=lambda x: x and x.endswith(":anmelden"))
        if not anmeld_btn:
            continue

        details_link = node.find("a", href=lambda x: x and "detailView-flow" in x)
        if not details_link:
            continue

        m_unit = re.search(r"unitId=(\d+)", details_link["href"])
        m_period = re.search(r"periodId=(\d+)", details_link["href"])

        seen[code] = {
            "code": code,
            "title": title_text,
            "info": info_text,
            "unit_id": m_unit.group(1) if m_unit else None,
            "period_id": m_period.group(1) if m_period else None,
        }

    return list(seen.values())


ACTION_ONCLICK_RE = re.compile(
    r"submitForm\('([^']*)',\s*'([^']*)',\s*[^,]*,\s*(\[.*?\])\)", re.DOTALL
)
ACTION_PARAM_PAIR_RE = re.compile(r"\['([^']*)',\s*'([^']*)'\]")


def _extract_action_params(onclick: str) -> Optional[dict]:
    """Parse a myfaces.oam.submitForm(...) onclick handler into its submission parts.

    HISinOne performs the Apply/Sign-off-Cancel actions entirely client-side via this
    helper: it adds one hidden field named after the clicked element (so the server
    knows which command fired) plus a handful of extra hidden fields, then submits
    the underlying <form> normally. To perform the same action over plain HTTP we
    need those exact name/value pairs.
    """
    m = ACTION_ONCLICK_RE.search(onclick)
    if not m:
        return None
    form_name, source_id, params_js = m.groups()
    pairs = dict(ACTION_PARAM_PAIR_RE.findall(params_js))
    return {"form_name": form_name, "source_id": source_id, "params": pairs}


def _parse_registration_status(html_text: str) -> list[dict]:
    """Return every exam unit in the study planner tree with its current registration
    action (register/deregister/none) and status label, deduplicated by module code.

    Unlike `_parse_candidate_units`, this also picks up units that show a
    "Sign off/Cancel" (abmelden) button, i.e. exams the student is already
    registered for.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tree = soup.find(id=lambda x: x and x.endswith("studyPlannerTree"))
    if not tree:
        return []
    table = tree.find("table")
    if not table:
        return []

    seen: dict[str, dict] = {}
    for row in table.find_all("tr"):
        node = row.find("div", class_="StudyPlannerPruefungNodeData")
        if not node:
            continue

        info_div = node.find("div", class_="unit-information")
        title_div = node.find("div", class_="unit-title-container")
        info_text = info_div.get_text(" ", strip=True) if info_div else ""
        title_text = title_div.get_text(" ", strip=True) if title_div else ""
        code = info_text.split(" ")[0] if info_text else title_text
        if not code or code in seen:
            continue

        details_link = node.find("a", href=lambda x: x and "detailView-flow" in x)
        if not details_link:
            continue
        m_unit = re.search(r"unitId=(\d+)", details_link["href"])
        m_period = re.search(r"periodId=(\d+)", details_link["href"])
        if not m_unit:
            continue

        anmeld_btn = node.find("button", id=lambda x: x and x.endswith(":anmelden"))
        abmeld_btn = node.find("button", id=lambda x: x and x.endswith(":abmelden"))
        action_btn = anmeld_btn or abmeld_btn
        action_type = "register" if anmeld_btn else ("deregister" if abmeld_btn else None)
        action = _extract_action_params(action_btn.get("onclick", "")) if action_btn else None

        status_btn = node.find("button", attrs={"data-popupbutton": "true"})
        status_text = status_btn.get_text(strip=True) if status_btn else ""

        seen[code] = {
            "code": code,
            "title": title_text,
            "info": info_text,
            "unit_id": m_unit.group(1),
            "period_id": m_period.group(1) if m_period else None,
            "action_type": action_type,
            "action": action,
            "status": status_text,
        }

    return list(seen.values())


def _parse_examination_periods(body_text: str) -> list[dict]:
    """Parse the detail page's flattened text into one entry per offered exam sitting.

    A single exam unit can be offered as several parallel groups (different weekday,
    time, examiner or resit), each with its own independent enrollment window. The
    page lists them one after another separated by "Examination period: N" markers,
    so we split on that and parse each block separately instead of taking the first
    "Enrollment ..." line found anywhere on the page.
    """
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if PERIOD_HEADER_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    periods = []
    for block in blocks:
        enroll_idx = None
        exam_date_text = None
        exam_weekday = None
        exam_time_range = None
        group_label = None
        for i, line in enumerate(block):
            if enroll_idx is None and "Enrollment" in line and ("feasible" in line or "possible" in line):
                enroll_idx = i
            if exam_date_text is None and EXAM_DATE_RE.match(line):
                exam_date_text = line
            if exam_weekday is None and WEEKDAY_RE.match(line):
                exam_weekday = line
            if exam_time_range is None and EXAM_TIME_RANGE_RE.match(line):
                exam_time_range = line
            if group_label is None:
                gm = GROUP_LABEL_RE.search(line)
                if gm:
                    group_label = gm.group(1).strip()
        if enroll_idx is None:
            continue
        enroll_line = block[enroll_idx]

        exam_date_display = None
        exam_date = None
        if exam_date_text:
            parts = [p for p in (exam_weekday, exam_date_text) if p]
            exam_date_display = ", ".join(parts)
            if exam_time_range:
                exam_date_display += f", {exam_time_range}"
            try:
                exam_date = datetime.strptime(exam_date_text, "%b %d, %Y").date().isoformat()
            except ValueError:
                pass

        # Positionally, "Examiner" and "Room" follow right after the
        # Enrollment/Disenrollment lines in the page's table layout.
        room = None
        for offset in (2, 3):
            idx = enroll_idx + offset
            if idx >= len(block):
                break
            candidate = block[idx]
            if PERIOD_HEADER_RE.match(candidate) or "Enrollment" in candidate or "Disenrollment" in candidate:
                break
            if "Prüfer" in candidate:
                continue
            if candidate in FOOTER_NOISE:
                break
            room = candidate
            break

        is_open = "was feasible" not in enroll_line and "was possible" not in enroll_line
        start_dt = end_dt = None
        m = ENROLLMENT_LINE_RE.search(enroll_line)
        if m:
            try:
                start_dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%m/%d/%y %I:%M %p")
            except ValueError:
                pass
            try:
                end_dt = datetime.strptime(f"{m.group(3)} {m.group(4)}", "%m/%d/%y %I:%M %p")
            except ValueError:
                pass

        periods.append({
            "is_open": is_open,
            "exam_date_text": exam_date_text,
            "exam_date_display": exam_date_display,
            "exam_date": exam_date,
            "group_label": group_label,
            "room": room,
            "start_date": start_dt.isoformat() if start_dt else None,
            "end_date": end_dt.isoformat() if end_dt else None,
            "raw": enroll_line,
        })

    return periods


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
    return _parse_examination_periods(body_text)


async def get_open_exam_registrations(session) -> list[dict]:
    """Return one entry per currently-open exam sitting (a unit may yield more than one,
    e.g. two parallel groups each with their own open registration window)."""
    referer = await _establish_stums_session(session)

    async with await session.get(STUDY_PLANNER_URL, allow_redirects=True, headers={"Referer": referer}) as r:
        html_text = await r.text()
    if "Login notwendig" in html_text:
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
                logging.warning(f"exam_reminder: failed to fetch detail for {unit['code']}: {e}")
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
        raise RuntimeError("StuMS session expired while fetching study planner")

    units = _parse_registration_status(html_text)
    return [u for u in units if u["action_type"] == "deregister"]


def _parse_exam_schedule_line(line: str) -> dict:
    """Parse a schedule line like 'Monday 8/31/26  No time defined' or
    'Tuesday 9/29/26  2:15 PM - 3:45 PM' into date/time info."""
    m = EXAM_SCHEDULE_LINE_RE.match(line.strip())
    if not m:
        return {"date": None, "time_text": None, "start_datetime": None}

    date_str = m.group("date")
    rest = m.group("rest").strip()
    try:
        date_obj = datetime.strptime(date_str, "%m/%d/%y")
    except ValueError:
        date_obj = None

    start_dt = None
    if date_obj and "no time defined" not in rest.lower():
        tm = EXAM_SCHEDULE_TIME_RE.search(rest)
        if tm:
            try:
                start_dt = datetime.strptime(f"{date_str} {tm.group(1)} {tm.group(2)}", "%m/%d/%y %I:%M %p")
            except ValueError:
                pass

    return {
        "date": date_obj.isoformat() if date_obj else None,
        "time_text": rest,
        "start_datetime": start_dt.isoformat() if start_dt else None,
    }


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
                m = re.search(r"unitId=(\d+)", details_link["href"])
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


MODULE_CODE_SUFFIX_RE = re.compile(r"_p\d*$", re.IGNORECASE)


def base_module_code(code: str) -> str:
    """Strip the study planner's component suffix (_P, _P1, _P2, ...) from an exam
    unit code, e.g. 'wir893_P' -> 'wir893', 'inf962_P2' -> 'inf962'. This is the
    form Stud.IP course pages reference the same module by.
    """
    return MODULE_CODE_SUFFIX_RE.sub("", (code or "").lower())


def filter_exams_by_module_codes(exams: list[dict], module_codes: set[str]) -> list[dict]:
    """Keep only exam entries whose base module code matches one of the given
    Stud.IP module codes. This is far more reliable than matching by title: Stud.IP
    course names and StuMS exam titles often differ completely (e.g. the Stud.IP
    course "Machine Learning with Scikit-Learn" is actually module 'inf536',
    listed in StuMS as "Computational Intelligence") while the module code itself
    is shared between both systems.
    """
    normalized = {c.lower() for c in module_codes}
    return [e for e in exams if base_module_code(e.get("code", "")) in normalized]


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
                logging.warning(f"exam_reminder: failed to fetch detail for {unit['code']}: {e}")
                return
        for period in periods:
            if period.get("exam_date"):
                results.append({**unit, **period})

    await asyncio.gather(*(process(u) for u in units))
    return results


async def check_exam_date_reminders(session, bot, broadcast_fn) -> None:
    """Remind about upcoming exam dates the student is registered for: at 3/1/0 days
    before, plus a same-day heads-up a few hours before start when a start time is
    known. Past exam dates are skipped (a registration can stay listed for a while
    after the exam itself has happened).
    """
    try:
        exams = await get_registered_exam_schedule(session)
    except Exception as e:
        logging.error(f"exam_reminder: fetch registered exam schedule failed: {e}")
        return

    cache = load_exam_cache()
    notified_days = cache.get("notified_exam_date", {})
    notified_hours = cache.get("notified_exam_hours", {})

    now = datetime.now()
    today = now.date()
    still_upcoming_keys = set()

    for exam in exams:
        exam_date = datetime.fromisoformat(exam["date"]).date()
        days_left = (exam_date - today).days
        if days_left < 0:
            continue  # exam date already passed

        key = f"{exam['unit_id']}:{exam['date']}"
        still_upcoming_keys.add(key)

        sent_days = set(notified_days.get(key, []))
        for threshold in EXAM_DATE_THRESHOLDS_DAYS:
            if days_left == threshold and threshold not in sent_days:
                when_line = "🔴 <b>Exam is TODAY!</b>" if threshold == 0 else f"⏳ <b>{threshold} day(s) left</b>"
                form_line = f"🎯 <b>Form:</b> {exam['exam_form']}\n" if exam.get("exam_form") else ""
                examiner_line = f"👤 <b>Examiner:</b> {exam['examiner']}\n" if exam.get("examiner") else ""
                text = (
                    "📚 <b>UPCOMING EXAM</b>\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>Course:</b> {exam['title']}\n"
                    f"📅 <b>Date:</b> {exam['date_line']}\n"
                    f"{form_line}"
                    f"{examiner_line}"
                    f"{when_line}\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                try:
                    await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                    logging.info(f"🎓 Notified: exam date reminder ({threshold}d) for {key}")
                except Exception as e:
                    logging.error(f"exam_reminder: failed to send exam-date reminder for {key}: {e}")
                sent_days.add(threshold)
        notified_days[key] = sorted(sent_days)

        if exam.get("start_datetime") and not notified_hours.get(key):
            start_dt = datetime.fromisoformat(exam["start_datetime"])
            hours_left = (start_dt - now).total_seconds() / 3600
            if 0 <= hours_left <= EXAM_HOURS_BEFORE_THRESHOLD:
                form_line = f"🎯 <b>Form:</b> {exam['exam_form']}\n" if exam.get("exam_form") else ""
                text = (
                    "⏰ <b>EXAM STARTING SOON</b>\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>Course:</b> {exam['title']}\n"
                    f"📅 <b>Date:</b> {exam['date_line']}\n"
                    f"{form_line}"
                    f"⌛ <b>Starts in ~{int(hours_left)} hour(s)</b>\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                try:
                    await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                    logging.info(f"🎓 Notified: exam starting soon for {key}")
                except Exception as e:
                    logging.error(f"exam_reminder: failed to send starting-soon reminder for {key}: {e}")
                notified_hours[key] = True

    # Drop bookkeeping for exams that dropped off the upcoming list (already
    # happened, or deregistered), so a future re-registration is treated as new.
    for key in list(notified_days.keys()):
        if key not in still_upcoming_keys:
            notified_days.pop(key, None)
            notified_hours.pop(key, None)

    cache["notified_exam_date"] = notified_days
    cache["notified_exam_hours"] = notified_hours
    save_exam_cache(cache)


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

    form_action_url = urljoin(page_url, form.get("action") or "")
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
        action = _extract_action_params(btn.get("onclick", ""))
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
            action = _extract_action_params(btn.get("onclick", ""))
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
    success = bool(result_unit) and result_unit["action_type"] == expected_action

    return {
        "success": success,
        "title": title,
        "status": result_unit["status"] if result_unit else None,
        "message": "Done." if success else "The site did not confirm the change; please check the status manually.",
    }


async def check_exam_reminders(session, bot, broadcast_fn) -> None:
    """Check for newly-opened exam registrations and closing-soon deadlines.

    `broadcast_fn` must be an async callable with signature (bot, text, parse_mode="HTML"),
    matching studip_bot.broadcast.
    """
    try:
        open_exams = await get_open_exam_registrations(session)
    except Exception as e:
        logging.error(f"exam_reminder: fetch failed: {e}")
        return

    cache = load_exam_cache()
    notified_open = cache.get("notified_open", {})
    notified_deadline = cache.get("notified_deadline", {})

    now = datetime.now()
    still_open_keys = set()

    for exam in open_exams:
        code = exam["code"]
        # A unit can have several parallel groups open at once (each its own sitting),
        # so the notification/dedup key has to include the sitting, not just the code.
        occurrence_key = f"{code}:{exam['start_date']}:{exam['end_date']}"
        still_open_keys.add(occurrence_key)
        end_date = datetime.fromisoformat(exam["end_date"]) if exam["end_date"] else None
        class_line = f"👥 <b>Class:</b> {exam['group_label']}\n" if exam.get("group_label") else ""
        room_line = f"📍 <b>Room:</b> {exam['room']}\n" if exam.get("room") else ""
        exam_date_line = f"📅 <b>Exam Date:</b> {exam['exam_date_display']}\n" if exam.get("exam_date_display") else ""

        if occurrence_key not in notified_open:
            text = (
                "🆕 <b>EXAM REGISTRATION OPEN</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"📘 <b>Course:</b> {exam['title']}\n"
                f"{exam_date_line}"
                f"{class_line}"
                f"{room_line}"
                f"🗓 <b>Window:</b> {exam['raw']}\n"
                "━━━━━━━━━━━━━━━━━"
            )
            try:
                await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                logging.info(f"🎓 Notified: exam registration opened for {occurrence_key}")
            except Exception as e:
                logging.error(f"exam_reminder: failed to send open-notification for {occurrence_key}: {e}")
            notified_open[occurrence_key] = now.isoformat()

        if end_date:
            days_left = (end_date.date() - now.date()).days
            sent_thresholds = set(notified_deadline.get(occurrence_key, []))
            for threshold in DEADLINE_THRESHOLDS_DAYS:
                if days_left == threshold and threshold not in sent_thresholds:
                    text = (
                        "⏰ <b>EXAM REGISTRATION CLOSING SOON</b>\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"📘 <b>Course:</b> {exam['title']}\n"
                        f"{exam_date_line}"
                        f"{class_line}"
                        f"{room_line}"
                        f"⌛ <b>{days_left} day(s) left</b> (closes {end_date.strftime('%d.%m.%Y %H:%M')})\n"
                        "━━━━━━━━━━━━━━━━━"
                    )
                    try:
                        await broadcast_fn(bot, text, reply_markup=WA_FORWARD_MARKUP)
                        logging.info(f"🎓 Notified: {days_left} day(s) left for {occurrence_key}")
                    except Exception as e:
                        logging.error(f"exam_reminder: failed to send deadline-notification for {occurrence_key}: {e}")
                    sent_thresholds.add(threshold)
            notified_deadline[occurrence_key] = sorted(sent_thresholds)

    # A sitting missing from still_open_keys has either closed or its registration
    # window ended; clear its notification state so a future re-opening (e.g. a
    # resit period) is treated as new again.
    for key in list(notified_open.keys()):
        if key not in still_open_keys:
            notified_open.pop(key, None)
            notified_deadline.pop(key, None)

    cache["notified_open"] = notified_open
    cache["notified_deadline"] = notified_deadline
    save_exam_cache(cache)


# Terminal grade statuses worth notifying about (as opposed to interim states
# like "Coursework submitted/registered" or "admission"). The grades page spells
# the failing one "not passed", not "failed" — matched exactly since "passed" is
# a substring of "not passed".
FINAL_GRADE_STATUSES = {"passed", "not passed"}

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

    onclick = expand_btn.get("onclick", "")
    m = JSF_AJAX_CALL_RE.search(onclick)
    execute = m.group(1).strip() if m else expand_btn["id"]
    render = m.group(2).strip() if m else ""
    # "@this" is a client-side jsf.ajax.js keyword resolved to the triggering
    # component before the real request is sent — resolve it ourselves too.
    execute = execute.replace("@this", expand_btn["id"])
    render = render.replace("@this", expand_btn["id"])
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

    form_action_url = urljoin(page_url, form.get("action") or "")
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
        level = (icon.get("alt") or "").strip().lower() if icon else ""
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


# Aggregate/summary rows (e.g. "Gesamtpunkte AEDS", "Overall Grade") sit in the
# same tree alongside real modules and also carry a numeric id, so they can't be
# filtered out the way the non-numeric admin rows (gÜK) are. They're identified
# by title instead.
SUMMARY_ROW_TITLE_RE = re.compile(r"gesamtpunkte|overall", re.IGNORECASE)


def compute_transcript_summary(grades: list[dict]) -> dict:
    """Compute a weighted-average grade and credit total from `get_grades()`
    output. This is an approximation of the university's own calculation (based
    on the same passed-module grade/credit data shown in "My achievements"), not
    an official transcript.
    """
    passed = []
    failed = []
    weighted_sum = 0.0
    total_credits = 0.0

    for entry in grades:
        title = entry.get("title") or ""
        if SUMMARY_ROW_TITLE_RE.search(title):
            continue

        status_title = (entry.get("status_title") or "").lower()
        grade_str = entry.get("grade")
        credit_str = entry.get("credit_points")

        try:
            credits = float(credit_str) if credit_str else 0.0
        except ValueError:
            credits = 0.0

        if status_title == "passed":
            item = {"title": title, "grade": grade_str, "credits": credits}
            passed.append(item)
            try:
                if grade_str:
                    weighted_sum += float(grade_str) * credits
                    total_credits += credits
            except ValueError:
                pass
        elif status_title == "not passed":
            failed.append({"title": title, "grade": grade_str, "credits": credits})

    gpa = round(weighted_sum / total_credits, 2) if total_credits else None
    return {
        "gpa": gpa,
        "total_credits": total_credits,
        "passed": passed,
        "failed": failed,
    }


async def check_grade_reminders(session, bot, broadcast_fn) -> None:
    """Notify when a module reaches a final grade (passed/failed) for the first
    time, or when that result changes (e.g. a correction)."""
    try:
        grades = await get_grades(session)
    except Exception as e:
        logging.error(f"exam_reminder: fetch grades failed: {e}")
        return

    cache = load_exam_cache()
    known_grades = cache.get("known_grades", {})

    for entry in grades:
        status_title = (entry.get("status_title") or "").lower()
        if status_title not in FINAL_GRADE_STATUSES:
            continue

        number = entry["number"]
        fingerprint = {"status_title": status_title, "grade": entry.get("grade")}
        if known_grades.get(number) == fingerprint:
            continue  # already notified about this exact result

        icon = "✅" if status_title == "passed" else "❌"
        grade_line = f"🔢 <b>Grade:</b> {entry['grade']}\n" if entry.get("grade") else ""
        credit_line = f"🎓 <b>Credits:</b> {entry['credit_points']}\n" if entry.get("credit_points") else ""

        # Personal message with the actual grade — no WA-forward button, since
        # this can carry personal grade info that shouldn't end up in a shared
        # (e.g. department) WhatsApp group by an accidental tap.
        personal_text = (
            f"{icon} <b>EXAM RESULT AVAILABLE</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"📘 <b>Course:</b> {entry.get('title')}\n"
            f"📊 <b>Status:</b> {status_title.capitalize()}\n"
            f"{grade_line}"
            f"{credit_line}"
            "━━━━━━━━━━━━━━━━━"
        )
        # Separate, grade-free announcement — safe to forward, so it carries the
        # WA button instead.
        announcement_text = (
            "📢 <b>NEW EXAM RESULT ANNOUNCED</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"📘 <b>Course:</b> {entry.get('title')}\n"
            "━━━━━━━━━━━━━━━━━"
        )
        try:
            await broadcast_fn(bot, personal_text)
            await broadcast_fn(bot, announcement_text, reply_markup=WA_FORWARD_MARKUP)
            logging.info(f"🎓 Notified: grade result for {entry.get('title')} ({number})")
        except Exception as e:
            logging.error(f"exam_reminder: failed to send grade notification for {number}: {e}")

        known_grades[number] = fingerprint

    cache["known_grades"] = known_grades
    save_exam_cache(cache)
