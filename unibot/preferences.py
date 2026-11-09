"""Student profile and preferences engine.

One JSON document per student (the "preferences" private record in their
db.UserScope), validated against PREFERENCE_SCHEMA and merged over the
defaults, so a missing key always has a sane value. Everything that used to
be one global setting (previous semester, mensa food filter, morning time)
is per student here.
"""

import logging
import re
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any, Optional

from studip_utils import TZ_BERLIN
from unibot import db
from unibot.config import ALLOWED_USER_IDS
from unibot.storage import get_watch_previous_semester_enabled

PREFS_KIND = "preferences"

EXAM_THRESHOLD_PRESETS = {
    # days before the exam, and the same-day heads-up hours before start (None: off)
    "standard": ([14, 7, 3, 1], 3),
    "relaxed": ([7, 3, 1], 3),
    "critical": ([3], None),
    "off": ([], None),
}
EXAM_THRESHOLD_LABELS = {
    "standard": "14, 7, 3, 1 days and 3 h before",
    "relaxed": "7, 3, 1 days and 3 h before",
    "critical": "only 3 days before",
    "off": "off",
}
TUITION_MODES = {
    "smart": "every 3 days, daily in the last 14 days",
    "weekly": "once a week",
    "daily_last7": "weekly, daily in the last 7 days",
    "off": "muted",
}
CAMPUS_ANCHORS = {"both": "Both (follow my classes)", "haarentor": "Haarentor", "wechloy": "Wechloy"}
DIETS = {
    "all": "Everything",
    "vegetarian": "Vegetarian",
    "vegan": "Vegan",
    "halal": "Halal / no pork",
    "fish": "Fish, no meat",
    "budget": "Budget (max 3.50 EUR)",
}
MORNING_SECTIONS = {
    "weather": "🌤️ Weather",
    "classes": "📅 Today's classes",
    "study": "📚 Study windows",
    "mensa": "🍽️ Mensa",
    "exams": "⏳ Exam countdown",
}
DELIVERY_MODES = {"instant": "Instant", "digest": "Evening digest at 18:00"}
FILE_DELIVERY = {
    "card": "Card with a download button",
    "upload": "Upload the file itself",
    "link": "Name and Stud.IP link only (saves mobile data)",
}
MORNING_EARLIEST = dtime(7, 0)
MORNING_LATEST = dtime(9, 30)
MORNING_STEP_MINUTES = 15
DIGEST_HOUR = 18
BUDGET_MAX_PRICE = 3.50

DEFAULTS: dict[str, Any] = {
    "watch_previous_semester": False,
    "exam_thresholds": "standard",
    "tuition_reminders": "smart",
    "campus_anchor": "both",
    "diet": "all",
    "food_avoid_codes": [],
    "food_avoid_keywords": [],
    "morning_time": "07:00",
    "morning_sections": {key: True for key in MORNING_SECTIONS},
    "outfit": True,
    "quiet_hours": None,
    "delivery_mode": "instant",
    "file_delivery": "card",
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def morning_time_options() -> list[str]:
    options, at = [], datetime.combine(datetime(2000, 1, 1), MORNING_EARLIEST)
    while at.time() <= MORNING_LATEST:
        options.append(at.strftime("%H:%M"))
        at += timedelta(minutes=MORNING_STEP_MINUTES)
    return options


def _valid_hhmm(value) -> bool:
    return isinstance(value, str) and bool(_HHMM.match(value))


def validate(key: str, value) -> Any:
    """The value to store for `key`, or ValueError when it is not allowed."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown preference {key!r}")
    choices: dict[str, set] = {
        "exam_thresholds": set(EXAM_THRESHOLD_PRESETS), "tuition_reminders": set(TUITION_MODES),
        "campus_anchor": set(CAMPUS_ANCHORS), "diet": set(DIETS), "delivery_mode": set(DELIVERY_MODES),
        "file_delivery": set(FILE_DELIVERY),
    }
    if key in choices:
        if value not in choices[key]:
            raise ValueError(f"{key} must be one of {sorted(choices[key])}")
        return value
    if key in ("watch_previous_semester", "outfit"):
        return bool(value)
    if key == "morning_time":
        if value != "off" and value not in morning_time_options():
            raise ValueError("morning_time is 'off' or 07:00 to 09:30 in 15 minute steps")
        return value
    if key == "morning_sections":
        if not isinstance(value, dict) or set(value) - set(MORNING_SECTIONS):
            raise ValueError("unknown morning section")
        return {k: bool(value.get(k, True)) for k in MORNING_SECTIONS}
    if key == "quiet_hours":
        if value is None:
            return None
        if not (isinstance(value, dict) and _valid_hhmm(value.get("start")) and _valid_hhmm(value.get("end"))
                and value["start"] != value["end"]):
            raise ValueError("quiet_hours is None or {'start': 'HH:MM', 'end': 'HH:MM'}")
        return {"start": value["start"], "end": value["end"]}
    if key in ("food_avoid_codes", "food_avoid_keywords"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} is a list of strings")
        return [v.strip() for v in value if v.strip()]
    raise ValueError(f"no validator for {key!r}")


def _initial(user_id: int) -> dict:
    """First preferences of a student: the old global settings carry over to
    the account they were made for (the primary admin), so nothing changes
    for them; everyone else starts from the defaults."""
    prefs: dict = {}
    if user_id == next(iter(ALLOWED_USER_IDS), None):
        try:
            prefs["watch_previous_semester"] = bool(get_watch_previous_semester_enabled())
        except Exception:
            logging.debug("No previous-semester setting to carry over", exc_info=True)
        try:
            if db.exists("food_preferences.json"):
                food = db.load("food_preferences.json") or {}
                prefs["food_avoid_codes"] = list(food.get("avoid_codes") or [])
                prefs["food_avoid_keywords"] = list(food.get("avoid_keywords") or [])
        except Exception:
            logging.debug("No food preferences to carry over", exc_info=True)
        # the recommendation always left out pork, alcohol and gelatine for this account
        prefs["diet"] = "halal"
    return prefs


def get_prefs(user_id: int) -> dict:
    """The student's preferences merged over DEFAULTS (always complete)."""
    scope = db.get_user_scope(user_id)
    stored = scope.private(PREFS_KIND)
    if stored is None:
        stored = _initial(user_id)
        scope.save_private(PREFS_KIND, stored)
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    merged["morning_sections"] = {**DEFAULTS["morning_sections"], **(stored.get("morning_sections") or {})}
    return merged


def get_pref(user_id: Optional[int], key: str) -> Any:
    """One preference; the default when there is no student."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown preference {key!r}")
    if user_id is None:
        return DEFAULTS[key]
    try:
        return get_prefs(user_id)[key]
    except ValueError:
        raise
    except Exception:
        logging.warning("Preferences unavailable for a student; using the default for %s", key)
        return DEFAULTS[key]


def set_pref(user_id: int, key: str, value) -> Any:
    """Validate and store one preference; returns the stored value."""
    value = validate(key, value)
    scope = db.get_user_scope(user_id)
    stored = dict(scope.private(PREFS_KIND) or _initial(user_id))
    stored[key] = value
    scope.save_private(PREFS_KIND, stored)
    return value


def cycle_choice(user_id: int, key: str, choices) -> Any:
    """Move a choice preference to the next option."""
    order = list(choices)
    current = get_pref(user_id, key)
    nxt = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
    return set_pref(user_id, key, nxt)


# ── derived answers ───────────────────────────────────────────────────────────

def exam_thresholds(user_id: Optional[int]) -> tuple[list[int], Optional[int]]:
    """(days before the exam, hours-before heads-up or None) for the student."""
    days, hours = EXAM_THRESHOLD_PRESETS[get_pref(user_id, "exam_thresholds")]
    return list(days), hours


def tuition_should_remind(mode: str, days_left: Optional[int], days_since_last: Optional[int],
                          first_time: bool) -> bool:
    """Whether to send an unpaid-fee reminder today (one a day at most)."""
    if mode == "off":
        return False
    if first_time:
        return True
    if days_since_last is not None and days_since_last < 1:
        return False
    if mode == "weekly":
        return days_since_last is None or days_since_last >= 7
    if mode == "daily_last7":
        if days_left is not None and days_left <= 7:
            return True
        return days_since_last is None or days_since_last >= 7
    # smart: daily in the last 14 days, else every 3 days
    if days_left is None or days_left <= 14:
        return True
    return days_since_last is None or days_since_last >= 3


def morning_send_time(user_id: int) -> Optional[dtime]:
    value = get_pref(user_id, "morning_time")
    if value == "off":
        return None
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def _t(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def in_quiet_hours(user_id: Optional[int], now: datetime) -> bool:
    quiet = get_pref(user_id, "quiet_hours")
    if not quiet:
        return False
    start, end, at = _t(quiet["start"]), _t(quiet["end"]), now.astimezone(TZ_BERLIN).time()
    return start <= at < end if start < end else (at >= start or at < end)


def campus_anchor(user_id: Optional[int]) -> Optional[str]:
    """The campus to assume when the classes don't say (None: follow them)."""
    anchor = get_pref(user_id, "campus_anchor")
    return None if anchor == "both" else anchor


# Mensa codes (see mensa.ALLERGEN_CODE_NAMES)
PORK_CODES = {"S", "Sch", "Su"}
MEAT_CODES_NO_FISH = PORK_CODES | {"G", "R", "L", "W"}
FISH_CODES = {"F", "Fi"}
ANIMAL_BY_PRODUCT_CODES = {"RG", "SG", "KL"}  # gelatine and calf rennet
VEGAN_EXTRA_CODES = {"Ei", "Mi"}


def diet_filter(diet: str) -> dict:
    """{"avoid_codes", "avoid_keywords", "max_price"} for a diet."""
    if diet == "vegetarian":
        codes = MEAT_CODES_NO_FISH | FISH_CODES | ANIMAL_BY_PRODUCT_CODES
        return {"avoid_codes": codes, "avoid_keywords": set(), "max_price": None}
    if diet == "vegan":
        codes = MEAT_CODES_NO_FISH | FISH_CODES | ANIMAL_BY_PRODUCT_CODES | VEGAN_EXTRA_CODES
        return {"avoid_codes": codes, "avoid_keywords": {"honig", "honey"}, "max_price": None}
    if diet == "halal":
        return {"avoid_codes": PORK_CODES | {"A", "RG", "SG"}, "avoid_keywords": {"wine", "wein"}, "max_price": None}
    if diet == "fish":
        return {"avoid_codes": MEAT_CODES_NO_FISH | ANIMAL_BY_PRODUCT_CODES, "avoid_keywords": set(), "max_price": None}
    if diet == "budget":
        return {"avoid_codes": set(), "avoid_keywords": set(), "max_price": BUDGET_MAX_PRICE}
    return {"avoid_codes": set(), "avoid_keywords": set(), "max_price": None}


def food_filter(user_id: Optional[int]) -> dict:
    """The student's diet plus their own avoided codes and keywords."""
    prefs = get_prefs(user_id) if user_id is not None else dict(DEFAULTS)
    base = diet_filter(prefs["diet"])
    return {
        "diet": prefs["diet"],
        "avoid_codes": set(base["avoid_codes"]) | set(prefs["food_avoid_codes"]),
        "avoid_keywords": {k.lower() for k in base["avoid_keywords"]} | {k.lower() for k in prefs["food_avoid_keywords"]},
        "max_price": base["max_price"],
    }
