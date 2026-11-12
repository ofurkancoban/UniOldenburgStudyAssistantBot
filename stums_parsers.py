"""Pure parsers for StuMS (HISinOne) pages: study planner, exam unit detail
pages, exam schedules, transcripts and invoices.

No network, no state and no clock of its own (callers pass `now`), so every
function here is tested against the HTML fixtures in tests/fixtures/.
exam_reminder fetches the pages and acts on what these return.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# StuMS shows Berlin wall-clock times without a zone; compare naive to naive
_TZ_BERLIN = ZoneInfo("Europe/Berlin")


ENROLLMENT_LINE_RE = re.compile(r"from\s+([\d/]+),\s*([\d:]+\s*[AP]M).*?until\s+([\d/]+),\s*([\d:]+\s*[AP]M)")


# One date + time of an enrollment window, in the formats StuMS uses for the
# English ("10/19/26, 12:00 AM") and German ("19.10.2026, 00:00") interface.
WINDOW_DATETIME_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\d{1,2}:\d{2}\s*[AaPp][Mm])"
    r"|(\d{1,2}\.\d{1,2}\.\d{2,4}),?\s*(\d{1,2}:\d{2})"
)


def _parse_window_datetimes(text: str) -> list[datetime]:
    """Every enrollment-window timestamp in `text`, in page order."""
    found = []
    for m in WINDOW_DATETIME_RE.finditer(text):
        if m.group(1):
            date_s, time_s = m.group(1), re.sub(r"\s+", " ", m.group(2).upper())
            if " " not in time_s:
                time_s = time_s[:-2] + " " + time_s[-2:]
            fmts = ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p")
        else:
            date_s, time_s = m.group(3), m.group(4)
            fmts = ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M")
        for fmt in fmts:
            try:
                found.append(datetime.strptime(f"{date_s} {time_s}", fmt))
                break
            except ValueError:
                continue
    return found


PERIOD_HEADER_RE = re.compile(r"^Examination period:\s*\d+$")


EXAM_DATE_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")


EXAM_TIME_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M$")


WEEKDAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$")


def _query_param(url: str, name: str) -> Optional[str]:
    """Read one query parameter independent of its position or encoding."""
    try:
        values = parse_qs(urlsplit(url).query).get(name)
    except ValueError:
        return None
    return values[0] if values else None


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

        href = str(details_link["href"])
        unit_id = _query_param(href, "unitId")
        period_id = _query_param(href, "periodId")

        seen[code] = {
            "code": code,
            "title": title_text,
            "info": info_text,
            "unit_id": unit_id,
            "period_id": period_id,
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
        href = str(details_link["href"])
        unit_id = _query_param(href, "unitId")
        period_id = _query_param(href, "periodId")
        if not unit_id:
            continue

        anmeld_btn = node.find("button", id=lambda x: x and x.endswith(":anmelden"))
        abmeld_btn = node.find("button", id=lambda x: x and x.endswith(":abmelden"))
        action_btn = anmeld_btn or abmeld_btn
        action_type = "register" if anmeld_btn else ("deregister" if abmeld_btn else None)
        action = _extract_action_params(str(action_btn.get("onclick", ""))) if action_btn else None

        status_btn = node.find("button", attrs={"data-popupbutton": "true"})
        status_text = status_btn.get_text(strip=True) if status_btn else ""

        seen[code] = {
            "code": code,
            "title": title_text,
            "info": info_text,
            "unit_id": unit_id,
            "period_id": period_id,
            "action_type": action_type,
            "action": action,
            "status": status_text,
        }

    return list(seen.values())


def _format_time_range_24h(time_range_text: str) -> Optional[str]:
    """Convert a scraped "4:00 PM - 6:00 PM" time range into 24h "16:00 -
    18:00". Returns None if it doesn't parse (caller falls back to the
    original text), so a format the page unexpectedly changes to never
    breaks the notification, just loses the 24h conversion for that one."""
    m = re.match(r"^(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)$", time_range_text.strip())
    if not m:
        return None
    try:
        start = datetime.strptime(m.group(1), "%I:%M %p").strftime("%H:%M")
        end = datetime.strptime(m.group(2), "%I:%M %p").strftime("%H:%M")
        return f"{start} - {end}"
    except ValueError:
        return None


def _parse_examination_periods(body_text: str, now: Optional[datetime] = None) -> list[dict]:
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
            try:
                exam_date_obj = datetime.strptime(exam_date_text, "%b %d, %Y").date()
            except ValueError:
                exam_date_obj = None

            if exam_date_obj:
                # Day/month/year + 24h, not the page's scraped "Nov 27,
                # 2026" / "4:00 PM" - matches the rest of the bot's date
                # formatting conventions.
                exam_date = exam_date_obj.isoformat()
                exam_date_display = exam_date_obj.strftime("%a, %d.%m.%Y")
                time_range_24h = _format_time_range_24h(exam_time_range) if exam_time_range else None
                if time_range_24h:
                    exam_date_display += f", {time_range_24h}"
                elif exam_time_range:
                    exam_date_display += f", {exam_time_range}"
            else:
                # Couldn't parse the date at all - fall back to the raw
                # scraped text rather than showing nothing.
                parts = [p for p in (exam_weekday, exam_date_text) if p]
                exam_date_display = ", ".join(parts)
                if exam_time_range:
                    exam_date_display += f", {exam_time_range}"

        # The page renders the window's dates in their own elements, so the
        # flattened text can spread "from ... until ..." over several lines:
        # read the enrollment line together with the lines that follow it.
        window_lines = [enroll_line]
        for follow in block[enroll_idx + 1: enroll_idx + 6]:
            if PERIOD_HEADER_RE.match(follow) or "Disenrollment" in follow or "Prüfer" in follow:
                break
            window_lines.append(follow)
        window_end = enroll_idx + len(window_lines) - 1

        # Positionally, "Examiner" and "Room" follow right after the
        # Enrollment/Disenrollment lines in the page's table layout (counted
        # from the window's last line, which may not be the Enrollment line).
        room = None
        for offset in (2, 3):
            idx = window_end + offset
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
        window_text = " ".join(window_lines)
        stamps = _parse_window_datetimes(window_text)
        start_dt = stamps[0] if stamps else None
        end_dt = stamps[1] if len(stamps) > 1 else None
        if end_dt is None:
            logging.info("exam_reminder: no enrollment window dates in %r", window_text[:200])
        # "Enrollment feasible from ..." is also shown for a window that only
        # opens later (e.g. the resit), so the dates decide whether it is open now.
        at = now or datetime.now(_TZ_BERLIN).replace(tzinfo=None)
        if is_open and start_dt and at < start_dt:
            is_open = False
        if is_open and end_dt and at > end_dt:
            is_open = False
        if len(window_lines) > 1 and start_dt:
            enroll_line = window_text

        if start_dt and end_dt:
            # Day/month/year + 24h, built from the already-parsed datetimes
            # rather than re-displaying the page's raw "10/19/26 12:00 AM
            # until 11/20/26 11:59 PM" sentence.
            verb = "feasible" if is_open else "was feasible"
            window_display = f"Enrollment {verb} from {start_dt.strftime('%d.%m.%Y %H:%M')} until {end_dt.strftime('%d.%m.%Y %H:%M')}"
        else:
            window_display = enroll_line

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
            "window_display": window_display,
        })

    return periods


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


MODULE_CODE_SUFFIX_RE = re.compile(r"_p\d*$", re.IGNORECASE)


def base_module_code(code: str) -> str:
    """Strip the study planner's component suffix (_P, _P1, _P2, ...) from an exam
    unit code, e.g. 'wir893_P' -> 'wir893', 'inf962_P2' -> 'inf962'. This is the
    form Stud.IP course pages reference the same module by.
    """
    return MODULE_CODE_SUFFIX_RE.sub("", (code or "").lower())


def is_passed_exam(exam: dict) -> bool:
    """The study planner marks a module the student already completed as
    "Passed"/"Bestanden". Its exam unit still carries a date (e.g. the
    module's generic portfolio date), which isn't an exam for any course
    the student takes now under another module."""
    return (exam.get("status") or "").strip().lower() in ("passed", "bestanden")


def normalize_grade_status(value) -> str:
    """Canonical English form for final result labels in English or German."""
    status = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    return {
        "bestanden": "passed",
        "nicht bestanden": "not passed",
    }.get(status, status)


def _parse_localized_number(value) -> Optional[float]:
    """Parse a decimal written with either German or English separators."""
    if value is None:
        return None
    text = re.sub(r"[\s\u00a0\u202f]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def filter_exams_by_module_codes(exams: list[dict], module_codes: set[str]) -> list[dict]:
    """Keep only exam entries whose base module code matches one of the given
    Stud.IP module codes. This is far more reliable than matching by title: Stud.IP
    course names and StuMS exam titles often differ completely (e.g. the Stud.IP
    course "Machine Learning with Scikit-Learn" is actually module 'inf536',
    listed in StuMS as "Computational Intelligence") while the module code itself
    is shared between both systems. Modules already passed are left out
    (see is_passed_exam).
    """
    normalized = {c.lower() for c in module_codes}
    return [e for e in exams if base_module_code(e.get("code", "")) in normalized and not is_passed_exam(e)]


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

        status_title = normalize_grade_status(entry.get("status_title"))
        grade_str = entry.get("grade")
        credit_str = entry.get("credit_points")

        parsed_credits = _parse_localized_number(credit_str)
        credits = max(0.0, parsed_credits or 0.0)

        if status_title == "passed":
            item = {"title": title, "grade": grade_str, "credits": credits}
            passed.append(item)
            try:
                grade_value = _parse_localized_number(grade_str)
                if grade_value is not None:
                    weighted_sum += grade_value * credits
                    total_credits += credits
            except (TypeError, ValueError):
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


def _parse_invoice_table_rows(container) -> list[dict]:
    """Extract invoice rows `[Period, Due date, Purpose, To Pay, Credit]` from a StuMS invoice table."""
    if not container:
        return []
    rows = []
    tbody = container.find("tbody") or container
    for tr in tbody.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 5:
            continue
        # Skip empty placeholder rows
        if not any(cells[:5]):
            continue
        rows.append({
            "period": cells[0],
            "due_date": cells[1],
            "purpose": cells[2],
            "to_pay": cells[3],
            "credit": cells[4],
        })
    return rows
