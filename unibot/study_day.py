"""A student's day around the study windows: which campus a room is on, the
travel time between campuses, where the lunch break goes and what kind of
work fits a window of a given length.

Pure functions on the window dicts that schedule.find_study_windows_for_day
and study_plan.find_free_study_windows build; no I/O here.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional

from studip_utils import TZ_BERLIN

# ── campuses ──────────────────────────────────────────────────────────────────
# Room names start with the building: A01-A15 and V01-V05 are on Campus
# Haarentor (Uhlhornsweg / Ammerländer Heerstraße), W01-W16 on Campus Wechloy.
HAARENTOR, WECHLOY = "haarentor", "wechloy"
_BUILDING_RE = re.compile(r"^\s*([AVW])\s?(\d{1,2})\b", re.IGNORECASE)

CAMPUS_INFO: dict[str, dict[str, Any]] = {
    HAARENTOR: {
        "name": "Campus Haarentor",
        "study_spot": "BIS library, 2nd floor quiet room",
        "mensa": "Mensa Uhlhornsweg",
        # the Stud.IP mensa widget's sub path for this mensa's menu
        "menu_path": "2/",
    },
    WECHLOY: {
        "name": "Campus Wechloy",
        "study_spot": "BIS Wechloy branch library or the W3 tables",
        "mensa": "Mensa Wechloy",
        "menu_path": None,  # not offered by the Stud.IP widget the bot reads
    },
}

# Haarentor <-> Wechloy by bus or bike, door to door.
CAMPUS_TRAVEL_MINUTES = 20

# ── lunch ─────────────────────────────────────────────────────────────────────
LUNCH_EARLIEST = dtime(11, 30)
LUNCH_LATEST_END = dtime(14, 0)
LUNCH_PREFERRED_START = dtime(12, 0)
LUNCH_MINUTES = 45
# A study piece left over after carving out lunch or travel is dropped below this.
MIN_STUDY_PIECE_MINUTES = 30

# ── kind of work by window length ─────────────────────────────────────────────
DEEP_WORK_MINUTES = 90
FOCUS_MINUTES = 60
STUDY_TYPES = {
    "micro": ("⚡", "Short block",
              "Ideal for flashcards, reading this week's announcements or skimming slides."),
    "focus": ("🎯", "Focused review",
              "Good for working through one lecture or a part of a problem set."),
    "deep": ("🧠", "Deep work",
             "Reserved for problem sets, coding and deep focus."),
}


def campus_of(location: Optional[str]) -> Optional[str]:
    """HAARENTOR, WECHLOY or None (online, unknown room, no room)."""
    m = _BUILDING_RE.match(location or "")
    if not m:
        return None
    return WECHLOY if m.group(1).upper() == "W" else HAARENTOR


def study_type(minutes: int) -> dict:
    """{"key", "emoji", "label", "hint"} for a window of this many minutes."""
    key = "deep" if minutes >= DEEP_WORK_MINUTES else "focus" if minutes >= FOCUS_MINUTES else "micro"
    emoji, label, hint = STUDY_TYPES[key]
    return {"key": key, "emoji": emoji, "label": label, "hint": hint}


def _minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _at(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t, tzinfo=TZ_BERLIN)


def _apply_travel(window: dict) -> Optional[dict]:
    """Shift a window between classes on different campuses by the travel
    time; None when too little is left to study."""
    origin, destination = campus_of(window.get("after_location")), campus_of(window.get("before_location"))
    if not origin or not destination or origin == destination:
        return window
    start = window["start_dt"] + timedelta(minutes=CAMPUS_TRAVEL_MINUTES)
    if _minutes(start, window["end_dt"]) < MIN_STUDY_PIECE_MINUTES:
        return None
    return {**window, "start_dt": start, "duration_minutes": _minutes(start, window["end_dt"]),
            "travel_minutes": CAMPUS_TRAVEL_MINUTES, "travel_from": origin, "travel_to": destination}


def _window_campus(window: dict) -> Optional[str]:
    """Where the student is during the window: the next class's campus
    (after any travel), else the previous class's."""
    return campus_of(window.get("before_location")) or campus_of(window.get("after_location"))


def _free_intervals(day_events: list, day: date, day_start: dtime, day_end: dtime) -> list[tuple[datetime, datetime]]:
    start, end = _at(day, day_start), _at(day, day_end)
    cursor, free = start, []
    for ev in sorted(day_events, key=lambda e: e["start_dt"]):
        if ev["start_dt"] > cursor:
            free.append((cursor, min(ev["start_dt"], end)))
        cursor = max(cursor, ev["end_dt"])
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(a, b) for a, b in free if b > a]


def _lunch_slot(a: datetime, b: datetime, day: date, carve: bool = False) -> Optional[tuple[datetime, datetime]]:
    """The lunch break inside the free span [a, b], or None if 45 minutes of
    it don't fit into the lunch hours. With `carve` (cutting it out of a study
    window) it avoids leaving a study piece too short to use."""
    lo, hi = max(a, _at(day, LUNCH_EARLIEST)), min(b, _at(day, LUNCH_LATEST_END))
    if _minutes(lo, hi) < LUNCH_MINUTES:
        return None
    start = min(max(_at(day, LUNCH_PREFERRED_START), lo), hi - timedelta(minutes=LUNCH_MINUTES))
    if carve and 0 < _minutes(a, start) < MIN_STUDY_PIECE_MINUTES:
        start = lo  # no study crumb before the meal
    # always exactly LUNCH_MINUTES; a too short rest after it is simply not a study window
    return start, start + timedelta(minutes=LUNCH_MINUTES)


def _campus_near(day_events: list, moment: datetime) -> Optional[str]:
    """Campus of the class right before `moment`, else of the next one."""
    before = [ev for ev in day_events if ev["end_dt"] <= moment and campus_of(ev.get("location"))]
    if before:
        return campus_of(max(before, key=lambda e: e["end_dt"]).get("location"))
    after = [ev for ev in day_events if ev["start_dt"] >= moment and campus_of(ev.get("location"))]
    if after:
        return campus_of(min(after, key=lambda e: e["start_dt"]).get("location"))
    return None


def plan_day(day_events: list, windows: list[dict], day: date,
             day_start: dtime = dtime(9, 0), day_end: dtime = dtime(19, 0)) -> dict[str, Any]:
    """Refine one day's study windows: travel time between campuses comes off
    the start, a 45-minute lunch break is placed between 11:30 and 14:00 (in
    free time outside the windows when there is any, else cut out of a
    window), and each window gets its campus, a study spot and a work type.

    `day_events` are the day's classes (not cancelled, with start_dt/end_dt).
    Returns {"windows": [...], "lunch": {...} or None}; windows stay sorted."""
    refined = []
    for w in windows:
        moved = _apply_travel(w)
        if moved is not None:
            refined.append(moved)

    # Lunch: prefer free time no window uses (a short gap, a free day's break).
    lunch = None
    if day.weekday() < 5:
        free = _free_intervals(day_events, day, day_start, day_end)
        spans_outside = []
        for a, b in free:
            cursor = a
            for w in sorted(refined, key=lambda x: x["start_dt"]):
                if w["end_dt"] <= cursor or w["start_dt"] >= b:
                    continue
                if w["start_dt"] > cursor:
                    spans_outside.append((cursor, w["start_dt"]))
                cursor = max(cursor, w["end_dt"])
            if cursor < b:
                spans_outside.append((cursor, b))
        # travel time before a window is not free for lunch either
        spans_outside = [(a, b) for a, b in spans_outside
                         if not any(w.get("travel_minutes") and a >= w["start_dt"] - timedelta(minutes=w["travel_minutes"])
                                    and b <= w["start_dt"] for w in refined)]
        slot = None
        for a, b in spans_outside:
            slot = _lunch_slot(a, b, day)
            if slot:
                break
        target = None
        if slot is None:
            candidates = [(w, _lunch_slot(w["start_dt"], w["end_dt"], day, carve=True)) for w in refined]
            candidates = [(w, s) for w, s in candidates if s]
            if candidates:
                target, slot = min(candidates, key=lambda c: abs(_minutes(_at(day, LUNCH_PREFERRED_START), c[1][0])))
        if slot:
            start, end = slot
            campus = (target and _window_campus(target)) or _campus_near(day_events, start)
            info = CAMPUS_INFO[campus or HAARENTOR]
            lunch = {"kind": "lunch", "date_key": day, "start_dt": start, "end_dt": end,
                     "duration_minutes": _minutes(start, end), "campus": campus,
                     "mensa": info["mensa"], "menu_path": info["menu_path"]}
            if target is not None:
                pieces = []
                if _minutes(target["start_dt"], start) >= MIN_STUDY_PIECE_MINUTES:
                    pieces.append({**target, "end_dt": start, "duration_minutes": _minutes(target["start_dt"], start),
                                   "before": "lunch", "description": f"{target.get('description') or 'study window'}, before lunch"})
                if _minutes(end, target["end_dt"]) >= MIN_STUDY_PIECE_MINUTES:
                    # the travel note stays with the first study piece of the window
                    travel_keys = ("travel_minutes", "travel_from", "travel_to") if pieces else ()
                    after = {k: v for k, v in target.items() if k not in travel_keys}
                    pieces.append({**after, "start_dt": end, "duration_minutes": _minutes(end, target["end_dt"]),
                                   "after": "lunch", "description": f"{target.get('description') or 'study window'}, after lunch"})
                index = refined.index(target)
                refined[index:index + 1] = pieces

    for w in refined:
        campus = _window_campus(w)
        w["campus"] = campus
        w["study_spot"] = CAMPUS_INFO[campus]["study_spot"] if campus else None
        w["study_type"] = study_type(w["duration_minutes"])
    refined.sort(key=lambda w: w["start_dt"])
    return {"windows": refined, "lunch": lunch}


def menu_highlight(items: list, limit: int = 2) -> Optional[str]:
    """A short "Falafel Bowl & dessert" line from today's mensa items
    (mensa.fetch_todays_menu_items), or None when there is nothing to show."""
    mains, has_dessert = [], False
    for item in items:
        category = (item.get("category") or "").lower()
        # the mensa page marks dishes in short supply with a "Limited - " prefix
        name = re.sub(r"^\s*limited\s*-\s*", "", item.get("name") or "", flags=re.IGNORECASE).strip()
        if not name:
            continue
        if "dessert" in category or "nachtisch" in category or "nachspeise" in category:
            has_dessert = True
        elif not any(word in category for word in ("beilage", "side", "salat", "suppe", "soup")):
            if name not in mains:
                mains.append(name)
    if not mains:
        return None
    text = " / ".join(mains[:limit])
    return f"{text} & dessert" if has_dessert else text
