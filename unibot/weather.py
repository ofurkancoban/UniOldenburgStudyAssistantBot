"""Open-Meteo weather snapshots and the user's weather location.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import logging
from datetime import datetime
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN

from unibot import db
from unibot.telegram_ui import user_data
from unibot.config import (
    WEATHER_LATITUDE,
    WEATHER_LONGITUDE,
)
from unibot.storage import shared_http
from unibot.auth import is_user_allowed


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


WEATHER_LOCATION_CACHE_PATH = "weather_location.json"
WEATHER_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


def load_weather_location() -> Optional[dict]:
    """Return {"name", "country", "latitude", "longitude"} for the location
    set via /status's "📍 Set Location", or None if unset — in which case
    get_weather_snapshot falls back to WEATHER_LATITUDE/WEATHER_LONGITUDE
    (Uni Oldenburg's coordinates by default)."""
    if db.exists(WEATHER_LOCATION_CACHE_PATH):
        try:
            return db.load(WEATHER_LOCATION_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load weather location: %s", e)
    return None


def save_weather_location(name: str, country: str, latitude: float, longitude: float) -> None:
    try:
        db.save(WEATHER_LOCATION_CACHE_PATH, {"name": name, "country": country, "latitude": latitude, "longitude": longitude})
    except Exception as e:
        logging.error("Could not save weather location: %s", e)


async def geocode_location(query: str) -> Optional[dict]:
    """Resolve a free-text place name to {"name", "country", "latitude",
    "longitude"} via Open-Meteo's free, keyless geocoding API. Returns the
    single best match, or None if nothing matched or the request failed —
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
    """Handle set_weather_location callback (from /status) — ask for a
    place name, geocoded via Open-Meteo (see geocode_location) and stored
    for get_weather_snapshot to use instead of the WEATHER_LATITUDE/
    WEATHER_LONGITUDE default."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    user_data(context)["pending_step"] = "weather_location"
    current = load_weather_location()
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
# rain, snow, showers, thunderstorm) — below that is dry (clear/cloudy/fog).
WEATHER_WET_CODE_THRESHOLD = 51


async def get_weather_snapshot(target_date=None) -> Optional[dict]:
    """Fetch a concise weather snapshot for the location set via /status's
    "📍 Set Location" (see load_weather_location), falling back to
    WEATHER_LATITUDE/WEATHER_LONGITUDE (Uni Oldenburg's coordinates by
    default) if none is set, via Open-Meteo (free, no API key needed).

    Deliberately focused on the 8–11 AM "leaving home" window rather than a
    whole-day aggregate — a stormy afternoon shouldn't make an otherwise
    clear morning commute look bad, and vice versa. Picks the worst
    (highest) weather code in that window, so a "will I get wet leaving
    the house" read is conservative rather than averaged-away.

    Returns {"description", "emoji", "is_wet", "precip_chance", "temp_min",
    "temp_max"}, or None on any failure — callers should just omit the
    weather section rather than block the rest of the message on it.
    `target_date` defaults to today; only today/tomorrow are meaningfully
    forecastable, so this isn't meant for the weekly plan.
    """
    target_date = target_date or datetime.now(TZ_BERLIN).date()
    days_ahead = (target_date - datetime.now(TZ_BERLIN).date()).days
    if not (0 <= days_ahead <= 1):
        return None

    location = load_weather_location()
    latitude = location["latitude"] if location else WEATHER_LATITUDE
    longitude = location["longitude"] if location else WEATHER_LONGITUDE

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
                morning_codes.append(code)
                morning_precip.append(precip)
        if not morning_codes:
            return None

        code = max(morning_codes)  # worst-case in the window
        desc, emoji = WEATHER_CODE_DESCRIPTIONS.get(code, ("unknown conditions", "🌡️"))
        day_index = data["daily"]["time"].index(target_iso)
        return {
            "description": desc,
            "emoji": emoji,
            "is_wet": code >= WEATHER_WET_CODE_THRESHOLD,
            "precip_chance": max(morning_precip),
            "temp_min": data["daily"]["temperature_2m_min"][day_index],
            "temp_max": data["daily"]["temperature_2m_max"][day_index],
            "location_name": location["name"] if location else "Oldenburg",
        }
    except (KeyError, IndexError, ValueError) as e:
        logging.warning("get_weather_snapshot: parse failed: %s", e)
        return None


def build_weather_text_line(weather: dict) -> str:
    """Short, deterministic weather + clothing line for the text message —
    LLM-independent, so it's always available even when OpenRouter isn't.
    The voice narration gets the raw snapshot instead and phrases its own
    advice (see build_ai_daily_plan_narration)."""
    tips = []
    if weather["is_wet"]:
        tips.append("bring an umbrella/waterproof jacket")
    if weather["temp_min"] <= 5:
        tips.append("dress warmly")
    elif weather["temp_max"] >= 24:
        tips.append("light clothing should do")
    tip_text = ", ".join(tips) if tips else "no special prep needed"
    return (
        f"{weather['emoji']} Weather in {weather['location_name']}: {weather['description']}, {weather['temp_min']:.0f}–{weather['temp_max']:.0f}°C, "
        f"{weather['precip_chance']}% chance of rain — {tip_text}"
    )


async def check_weather_health() -> tuple[bool, str]:
    """(ok, detail) — free/keyless, so just confirms Open-Meteo itself is
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
