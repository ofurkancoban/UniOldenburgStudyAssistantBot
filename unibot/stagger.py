"""Staggered personal check times (pure).

Personal StuMS data (grades, exam registrations, fees) is checked with each
student's own login, so checking every student in the same minute looks
like a burst of logins from one IP. Instead every student gets their own
check times per day: one per window, at a minute derived from a hash of
their id and the date (so it differs between students and between days,
like people opening the portal on their phones).

More windows in grade season (exam and result months, or while a
registration is open), fewer in the middle of the semester, none at night.
"""

import hashlib
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Optional

from studip_utils import TZ_BERLIN

NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 7
# exams and results: WiSe exams Feb/Mar with results into April, SoSe exams Jul to Sep
GRADE_SEASON_MONTHS = frozenset({2, 3, 4, 7, 8, 9})
SEASON_WINDOWS = (((8, 0), (10, 30)), ((11, 0), (13, 30)), ((14, 0), (16, 30)), ((17, 0), (19, 30)))
QUIET_WINDOWS = (((9, 0), (12, 0)), ((15, 0), (18, 30)))


def is_grade_season(day: date, registration_open: bool = False) -> bool:
    return registration_open or day.month in GRADE_SEASON_MONTHS


def is_night(now: datetime) -> bool:
    return NIGHT_START_HOUR <= now.astimezone(TZ_BERLIN).hour < NIGHT_END_HOUR


def windows_for(season: bool) -> tuple:
    return SEASON_WINDOWS if season else QUIET_WINDOWS


def _offset_seconds(user_id: int, day: date, index: int, span_seconds: int) -> int:
    digest = hashlib.sha256(f"stagger:{int(user_id)}:{day.isoformat()}:{index}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % max(1, span_seconds)


def personal_slots(user_id: int, day: date, season: bool) -> list[datetime]:
    """The student's check times on `day` (Berlin time, one per window)."""
    slots = []
    for index, ((sh, sm), (eh, em)) in enumerate(windows_for(season)):
        start = datetime.combine(day, clock_time(sh, sm), tzinfo=TZ_BERLIN)
        end = datetime.combine(day, clock_time(eh, em), tzinfo=TZ_BERLIN)
        span = int((end - start).total_seconds())
        slots.append(start + timedelta(seconds=_offset_seconds(user_id, day, index, span)))
    return slots


def slot_id(day: date, index: int) -> str:
    return f"{day.isoformat()}#{index}"


def due_slot(user_id: int, now: datetime, done: Optional[str], season: bool) -> Optional[str]:
    """The id of the latest of today's slots that has passed and was not run
    yet, or None (night, nothing passed yet, or already done). Slots missed
    while the bot was down collapse into one run."""
    if is_night(now):
        return None
    local = now.astimezone(TZ_BERLIN)
    passed = [i for i, at in enumerate(personal_slots(user_id, local.date(), season)) if at <= local]
    if not passed:
        return None
    latest = slot_id(local.date(), passed[-1])
    if done is not None and done >= latest:
        return None
    return latest


def describe_slots(user_id: int, day: date, season: bool) -> str:
    """"09:12, 15:18" style list, for the status screen and tests."""
    return ", ".join(at.strftime("%H:%M") for at in personal_slots(user_id, day, season))
