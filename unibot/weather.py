"""Open-Meteo weather snapshots and the user's weather location.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import logging
import time
from datetime import datetime
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN, html_escape

from unibot import db
from unibot.telegram_ui import user_data
from unibot.config import (
    PRIMARY_ADMIN_ID,
    WEATHER_LATITUDE,
    WEATHER_LONGITUDE,
)
from unibot.storage import personal_record, save_personal_record, shared_http
from unibot.auth import current_user_id, is_user_allowed


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


WEATHER_LOCATION_CACHE_PATH = "weather_location.json"
WEATHER_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


WEATHER_LOCATION_RECORD = "weather_location"  # each student's own place, encrypted


def _weather_owner(user_id=None) -> Optional[int]:
    return user_id if user_id is not None else current_user_id()


def load_weather_location(user_id=None) -> Optional[dict]:
    """Return {"name", "country", "latitude", "longitude"} for the place the
    student (default: the user the update acts for) set via /status's
    "📍 Set Location", or None if unset - in which case get_weather_snapshot
    falls back to WEATHER_LATITUDE/WEATHER_LONGITUDE (Uni Oldenburg's
    coordinates by default). A student's place is never shown to or used
    for anyone else."""
    uid = _weather_owner(user_id)
    if uid is None:
        return None
    try:
        own = personal_record(uid, WEATHER_LOCATION_RECORD)
        if own:
            return own
        if uid == PRIMARY_ADMIN_ID and db.exists(WEATHER_LOCATION_CACHE_PATH):
            # the one shared place from before belonged to the bot's owner
            legacy = db.load(WEATHER_LOCATION_CACHE_PATH)
            if legacy:
                save_personal_record(uid, WEATHER_LOCATION_RECORD, legacy)
            db.delete(WEATHER_LOCATION_CACHE_PATH)
            return legacy or None
    except Exception as e:
        logging.warning("Could not load weather location: %s", e)
    return None


def save_weather_location(name: str, country: str, latitude: float, longitude: float,
                          user_id=None) -> None:
    uid = _weather_owner(user_id)
    if uid is None:
        logging.error("Weather location not saved: no student")
        return
    try:
        save_personal_record(uid, WEATHER_LOCATION_RECORD,
                             {"name": name, "country": country, "latitude": latitude, "longitude": longitude})
    except Exception as e:
        logging.error("Could not save weather location: %s", e)


async def geocode_location(query: str) -> Optional[dict]:
    """Resolve a free-text place name to {"name", "country", "latitude",
    "longitude"} via Open-Meteo's free, keyless geocoding API. Returns the
    single best match, or None if nothing matched or the request failed -
    callers should ask the user to try a different/more specific name
    rather than silently keeping the old location."""
    try:
        async with shared_http() as session:
            async with session.get(
                WEATHER_GEOCODING_URL,
                params={"name": query, "count": 1, "language": "en", "format": "json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        logging.warning("geocode_location: fetch failed: %s", e)
        return None

    results = data.get("results") or []
    if not results:
        return None
    best = results[0]
    return {
        "name": best.get("name", query),
        "country": best.get("country", ""),
        "latitude": best["latitude"],
        "longitude": best["longitude"],
    }


async def handle_set_weather_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle set_weather_location callback (from /status) - ask for a
    place name, geocoded via Open-Meteo (see geocode_location) and stored
    for get_weather_snapshot to use instead of the WEATHER_LATITUDE/
    WEATHER_LONGITUDE default."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    user_data(context)["pending_step"] = "weather_location"
    current = load_weather_location(user_id)
    current_line = f"\nCurrently: {current['name']}, {current['country']}" if current else ""
    await query.message.reply_text(f"📍 Where should I check the weather for? (city or place name){current_line}")


WEATHER_CODE_DESCRIPTIONS = {
    0: ("clear sky", "☀️"), 1: ("mostly clear", "🌤️"), 2: ("partly cloudy", "⛅"), 3: ("overcast", "☁️"),
    45: ("foggy", "🌫️"), 48: ("foggy with frost", "🌫️"),
    51: ("light drizzle", "🌦️"), 53: ("drizzle", "🌦️"), 55: ("heavy drizzle", "🌧️"),
    56: ("freezing drizzle", "🌧️"), 57: ("freezing drizzle", "🌧️"),
    61: ("light rain", "🌦️"), 63: ("rain", "🌧️"), 65: ("heavy rain", "🌧️"),
    66: ("freezing rain", "🌨️"), 67: ("freezing rain", "🌨️"),
    71: ("light snow", "🌨️"), 73: ("snow", "❄️"), 75: ("heavy snow", "❄️"), 77: ("snow grains", "❄️"),
    80: ("rain showers", "🌦️"), 81: ("rain showers", "🌧️"), 82: ("heavy rain showers", "⛈️"),
    85: ("snow showers", "🌨️"), 86: ("heavy snow showers", "❄️"),
    95: ("thunderstorm", "⛈️"), 96: ("thunderstorm with hail", "⛈️"), 99: ("severe thunderstorm", "⛈️"),
}
# WMO weather codes 51 and up are all some form of precipitation (drizzle,
# rain, snow, showers, thunderstorm) - below that is dry (clear/cloudy/fog).
WEATHER_WET_CODE_THRESHOLD = 51


# (date, latitude, longitude) -> the last snapshot fetched for that place
_SNAPSHOTS: dict = {}
_FETCHED_AT: dict = {}
SNAPSHOT_REUSE_SECONDS = 30 * 60  # students at the same place share a fetch


def _coords(location: Optional[dict]) -> tuple[float, float]:
    if location:
        return float(location["latitude"]), float(location["longitude"])
    return float(WEATHER_LATITUDE), float(WEATHER_LONGITUDE)


def snapshot_key(day, user_id=None) -> tuple:
    """Cache key of `day`'s snapshot at the student's place."""
    lat, lon = _coords(load_weather_location(user_id))
    return (day, round(lat, 3), round(lon, 3))


def forget_weather_place(user_id, place: Optional[dict]) -> None:
    """Drop the in-memory snapshots of a deleted student's own place (only a
    cache: anyone else at that place simply fetches it again)."""
    if not place:
        return
    lat, lon = (round(c, 3) for c in _coords(place))
    for key in [k for k in _SNAPSHOTS if k[1:] == (lat, lon)]:
        _SNAPSHOTS.pop(key, None)
        _FETCHED_AT.pop(key, None)


def cached_weather_snapshot(day, user_id=None) -> Optional[dict]:
    """The last get_weather_snapshot result for `day` at the student's place,
    without a request (None when it wasn't fetched in this process)."""
    return _SNAPSHOTS.get(snapshot_key(day, user_id))


async def get_weather_snapshot(target_date=None, user_id=None) -> Optional[dict]:
    """Fetch a concise weather snapshot for the location set via /status's
    "📍 Set Location" (see load_weather_location), falling back to
    WEATHER_LATITUDE/WEATHER_LONGITUDE (Uni Oldenburg's coordinates by
    default) if none is set, via Open-Meteo (free, no API key needed).

    Deliberately focused on the 8–11 AM "leaving home" window rather than a
    whole-day aggregate - a stormy afternoon shouldn't make an otherwise
    clear morning commute look bad, and vice versa. Picks the worst
    (highest) weather code in that window, so a "will I get wet leaving
    the house" read is conservative rather than averaged-away.

    Returns {"description", "emoji", "is_wet", "precip_chance", "temp_min",
    "temp_max"}, or None on any failure - callers should just omit the
    weather section rather than block the rest of the message on it.
    `target_date` defaults to today; only today/tomorrow are meaningfully
    forecastable, so this isn't meant for the weekly plan.
    """
    target_date = target_date or datetime.now(TZ_BERLIN).date()
    days_ahead = (target_date - datetime.now(TZ_BERLIN).date()).days
    if not (0 <= days_ahead <= 1):
        return None

    location = load_weather_location(user_id)
    latitude, longitude = _coords(location)
    key = (target_date, round(latitude, 3), round(longitude, 3))
    if key in _SNAPSHOTS and time.time() - _FETCHED_AT.get(key, 0.0) < SNAPSHOT_REUSE_SECONDS:
        return _SNAPSHOTS[key]

    try:
        async with shared_http() as session:
            async with session.get(
                WEATHER_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "hourly": "weathercode,precipitation_probability",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "Europe/Berlin",
                    "forecast_days": 2,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logging.warning("get_weather_snapshot: HTTP %s", resp.status)
                    return None
                data = await resp.json()
    except Exception as e:
        logging.warning("get_weather_snapshot: fetch failed: %s", e)
        return None

    try:
        target_iso = target_date.isoformat()
        morning_codes = []
        morning_precip = []
        for t, code, precip in zip(data["hourly"]["time"], data["hourly"]["weathercode"], data["hourly"]["precipitation_probability"]):
            if t.startswith(target_iso) and 8 <= int(t[11:13]) <= 11:
                # Open-Meteo sends null for hours a station has no value for
                if isinstance(code, (int, float)):
                    morning_codes.append(int(code))
                if isinstance(precip, (int, float)):
                    morning_precip.append(precip)
        if not morning_codes:
            return None

        code = max(morning_codes)  # worst-case in the window
        desc, emoji = WEATHER_CODE_DESCRIPTIONS.get(code, ("unknown conditions", "🌡️"))
        day_index = data["daily"]["time"].index(target_iso)
        temp_min = data["daily"]["temperature_2m_min"][day_index]
        temp_max = data["daily"]["temperature_2m_max"][day_index]
        if not all(isinstance(t, (int, float)) for t in (temp_min, temp_max)):
            # a null temperature would break every line that prints it
            # (the morning summary of everyone at this place)
            logging.warning("get_weather_snapshot: no temperature for %s", target_iso)
            return None
        snapshot = {
            "description": desc,
            "emoji": emoji,
            "is_wet": code >= WEATHER_WET_CODE_THRESHOLD,
            "precip_chance": max(morning_precip) if morning_precip else 0,
            "temp_min": temp_min,
            "temp_max": temp_max,
            "location_name": location["name"] if location else "Oldenburg",
        }
        _SNAPSHOTS[key] = snapshot
        _FETCHED_AT[key] = time.time()
        return snapshot
    except (KeyError, IndexError, ValueError, TypeError) as e:
        logging.warning("get_weather_snapshot: parse failed: %s", e)
        return None


def build_weather_text_line(weather: dict, escape=None) -> str:
    """Short, deterministic weather + clothing line for the text message -
    LLM-independent, so it's always available even when OpenRouter isn't.
    The voice narration gets the raw snapshot instead and phrases its own
    advice (see build_ai_daily_plan_narration).

    The place and description are free text (the student typed the place):
    `escape` makes them safe for the markup of the message the line goes into
    (HTML by default, studip_utils.md_escape for the Markdown schedule)."""
    escape = escape or html_escape
    tips = []
    if weather["is_wet"]:
        tips.append("bring an umbrella/waterproof jacket")
    if weather["temp_min"] <= 5:
        tips.append("dress warmly")
    elif weather["temp_max"] >= 24:
        tips.append("light clothing should do")
    tip_text = ", ".join(tips) if tips else "no special prep needed"
    return (
        f"{weather['emoji']} Weather in {escape(weather['location_name'])}: {escape(weather['description'])}, {weather['temp_min']:.0f}–{weather['temp_max']:.0f}°C, "
        f"{weather['precip_chance']}% chance of rain - {tip_text}"
    )


async def check_weather_health() -> tuple[bool, str]:
    """(ok, detail) - free/keyless, so just confirms Open-Meteo itself is
    reachable right now, same endpoint get_weather_snapshot calls."""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LATITUDE}&longitude={WEATHER_LONGITUDE}&current_weather=true"
        async with shared_http() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    return True, "OK"
                return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)[:150]
