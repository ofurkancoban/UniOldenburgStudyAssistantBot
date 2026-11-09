"""Schedule from Stud.IP, rooms, study windows, calendar views and change alerts.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from exam_reminder import get_open_exam_registrations, get_registered_exam_schedule
from fast_enroll import load_pending, save_pending_job
from studip_utils import TZ_BERLIN, berlin_now, format_hours as _format_hours, md_escape as _md

from unibot import state
from unibot.storage import (
    get_travel_mode,
    record_study_block,
    load_general_cache,
    load_reminders_cache,
    load_tasks,
    save_reminders_cache,
    single_flight,
    update_general_cache,
)
from unibot.formatting import (
    _format_relative,
    _get_course_icon,
    _get_day_emoji,
    _get_location_emoji,
    _safe_loc,
    _status_emoji,
    clean_course_title,
)
from unibot.telegram_ui import _show_typing, _start_loading_animation, _stop_loading_animation, broadcast
from unibot.auth import current_user_id, get_user_session, is_user_allowed, login_studip
from unibot.studip_api import (
    BASE_URL,
    _api_berlin,
    _api_html_to_text,
    _api_rel_id,
    studip_api_get,
    studip_api_get_all,
    studip_api_user_id,
)
from unibot.weather import build_weather_text_line, cached_weather_snapshot, get_weather_snapshot
from unibot.mensa import fetch_menu_titles, fetch_todays_menu_items
from unibot.study_day import menu_highlight, plan_day, switch_warning, tight_campus_switches, travel_for, travel_note


fastenroll_tasks: dict[int, dict[str, asyncio.Task]] = {}  # chat_id -> {sem_id -> asyncio.Task}


MORNING_SUMMARY_HOUR = 7
MORNING_PREPARE_LEAD_MINUTES = 30  # AI parts (lunch pick, outfit image, voice) are generated this long before delivery


async def run_morning_summary_schedule(bot, user_ids, now: datetime) -> None:
    """Called every watcher cycle. From MORNING_PREPARE_LEAD_MINUTES before
    MORNING_SUMMARY_HOUR, prepares today's summary once, then hands it to a
    task that sends it exactly at MORNING_SUMMARY_HOUR. If that time has
    already passed (bot started late), prepares and sends right away."""
    from unibot.morning import _deliver_morning_summary_at, _mark_morning_summary_sent, prepare_morning_summary
    today_str = now.strftime("%Y-%m-%d")
    send_at = now.replace(hour=MORNING_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
    if now < send_at - timedelta(minutes=MORNING_PREPARE_LEAD_MINUTES):
        return
    if load_general_cache().get("last_morning_summary") == today_str:
        return
    if state._morning_delivery_task and not state._morning_delivery_task.done():
        return  # prepared and waiting for send_at

    if state._prepared_morning.get("date") != today_str:
        logging.info("🌞 Preparing morning summary...")
        try:
            data = await prepare_morning_summary(bot, user_ids)
        except Exception as e:
            logging.error("❌ Morning summary preparation failed: %s", e)
            data = None
        if not data:
            if datetime.now(TZ_BERLIN) >= send_at:
                _mark_morning_summary_sent(today_str)  # same no-retry-all-day rule as before
            return
        state._prepared_morning = {"date": today_str, "data": data}

    state._morning_delivery_task = asyncio.create_task(
        _deliver_morning_summary_at(bot, user_ids, state._prepared_morning["data"], send_at, today_str)
    )
    state._prepared_morning = {}


@single_flight
async def check_calendar_reminders(bot, chat_id, silent: bool = False):
    """Check for upcoming classes and send reminders 30 minutes before."""
    user_session = get_user_session(chat_id)
    session = user_session or state.global_session
    try:
        if not silent:
            logging.info("⏰ Checking calendar reminders for %s...", chat_id)

        reminders = load_reminders_cache(user_id=chat_id)
        updated = False

        today = datetime.now(TZ_BERLIN).date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=session, week_start=week_start, user_id=chat_id)
        if not events:
            return

        now = datetime.now(TZ_BERLIN)

        # Only the day's *first* class gets the weather-based earlier
        # heads-up -- that's the actual leaving-home commute; later classes
        # are typically just a walk across campus, where weather matters
        # far less than a wet trip from home.
        today_starts = [e["start_dt"] for e in events if e.get("start_dt") and e["start_dt"].date() == now.date()]
        first_start_today = min(today_starts) if today_starts else None
        first_class_weather = None
        if first_start_today:
            try:
                first_class_weather = await get_weather_snapshot(first_start_today.date())
            except Exception as e:
                logging.warning("check_calendar_reminders: weather fetch failed: %s", e)

        # classes reached from the other campus with too little time to travel
        tight_arrivals = {id(s["to_event"]): s for s in tight_campus_switches(
            [e for e in events if e.get("start_dt") and e["start_dt"].date() == now.date()],
            user_travel(now.date(), chat_id))}

        for event in events:
            if not event.get("start_dt") or not event.get("title") or event["title"] == "Untitled":
                continue

            title = event["title"]
            start_dt = event["start_dt"]
            location = event.get("location", "Unknown location")
            # Create a unique key for this reminder (Title + Timestamp)
            event_key = f"{title}|{int(start_dt.timestamp())}"

            if event_key in reminders:
                continue

            diff = start_dt - now
            minutes_left = diff.total_seconds() / 60

            is_first_of_day = first_start_today is not None and start_dt == first_start_today
            # Rainy/snowy first-class-of-the-day: widen the window so the
            # heads-up lands ~15 minutes earlier, giving time to grab an
            # umbrella/jacket and allow for a slower, wetter commute.
            extra_lead = 15 if (is_first_of_day and first_class_weather and first_class_weather["is_wet"]) else 0
            window_low, window_high = 25 + extra_lead, 35 + extra_lead

            # Reminder window: 25-35 minutes before the lesson starts (wider,
            # earlier, if today's first class starts in wet weather)
            if window_low < minutes_left <= window_high:
                weather_line = ""
                if is_first_of_day and first_class_weather:
                    if first_class_weather["is_wet"]:
                        weather_line = f"\n{first_class_weather['emoji']} {first_class_weather['description'].capitalize()} expected – grab an umbrella and leave a bit early!"
                    else:
                        weather_line = f"\n{first_class_weather['emoji']} {first_class_weather['description'].capitalize()} out there, should be a fine walk over."
                switch = tight_arrivals.get(id(event))
                switch_line = f"\n{html.escape(switch_warning(switch))}" if switch else ""
                markup = None
                if switch:
                    # arriving late: the newest recording and slides to catch up on the start
                    from unibot.study_plan import build_catch_up_buttons
                    try:
                        markup = await build_catch_up_buttons(event, chat_id)
                    except Exception as e:
                        logging.warning("Catch-up files unavailable for %s: %s", title, e)
                    if markup:
                        switch_line += "\n📎 Catch up on what you miss:"
                text = (
                    "🔔 <b>LECTURE REMINDER</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>{title}</b>\n"
                    f"🕒 Starting in <b>{int(minutes_left)} minutes</b>\n"
                    f"📍 {location}"
                    f"{weather_line}"
                    f"{switch_line}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
                    logging.info("🔔 Sent reminder for %s to %s", title, chat_id)
                    reminders[event_key] = int(now.timestamp())
                    updated = True
                except Exception as e:
                    logging.error("Failed to send reminder for %s: %s", title, e)

        # Study window reminders (30 minutes before, window: 25-35 minutes)
        today_windows = find_study_windows_for_day(events, now.date(), user_id=chat_id)
        for w in today_windows:
            start_dt = w.get("start_dt")
            if not start_dt:
                continue

            diff = start_dt - now
            minutes_left = diff.total_seconds() / 60
            if 25 < minutes_left <= 35:
                study_key = f"study_remind|{w['date_key']}|{int(start_dt.timestamp())}"
                if study_key in reminders:
                    continue

                duration_str = _format_hours(w["duration_minutes"])
                desc = w.get("description", "focused study window")
                desc_formatted = html.escape(desc[0].upper() + desc[1:] if desc else "Focused study window")
                kind = w.get("study_type") or {}
                extra = ""
                if kind:
                    extra += f"{kind['emoji']} <b>{kind['label']}</b>: {html.escape(kind['hint'])}\n"
                if travel_note(w):
                    extra += f"{html.escape(travel_note(w) or '')}\n"
                if w.get("study_spot"):
                    extra += f"📍 {html.escape(w['study_spot'])}\n"
                text = (
                    "🔔 <b>STUDY TIME REMINDER</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "📚 <b>Good Time to Study</b>\n"
                    f"🕒 Starting in <b>{int(minutes_left)} minutes</b> ({start_dt:%H:%M} – {w['end_dt']:%H:%M})\n"
                    f"⏳ Duration: <b>{duration_str}</b>\n"
                    f"💡 <i>{desc_formatted}</i>\n"
                    f"{extra}"
                    "━━━━━━━━━━━━━━━━━━"
                )
                keyboard = [
                    [
                        InlineKeyboardButton("📅 Today's Plan", callback_data="calendar_today"),
                        InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
                    logging.info("🔔 Sent study session reminder for %s to %s", desc, chat_id)
                    reminders[study_key] = int(now.timestamp())
                    updated = True
                except Exception as e:
                    logging.error("Failed to send study session reminder: %s", e)

            # The block has just started: send the study pack (course, work
            # type, deadlines and the files to open right away).
            if -STUDY_PACK_LATE_MINUTES < minutes_left <= 0:
                start_key = f"study_start|{w['date_key']}|{int(start_dt.timestamp())}"
                if start_key in reminders:
                    continue
                from unibot.study_plan import build_study_pack, study_pack_course
                try:
                    pack_text, pack_markup = await build_study_pack(w, chat_id, now)
                    await bot.send_message(chat_id=chat_id, text=pack_text, parse_mode="HTML", reply_markup=pack_markup)
                    logging.info("📚 Sent study pack for the %s block to %s", f"{start_dt:%H:%M}", chat_id)
                    reminders[start_key] = int(now.timestamp())
                    updated = True
                    course = study_pack_course(w, chat_id, now)
                    record_study_block(chat_id, int(start_dt.timestamp()), "sent", course[1] if course else None)
                except Exception as e:
                    logging.error("Failed to send study pack: %s", e)

        # Cleanup old reminders (older than 24 hours)
        cutoff = now.timestamp() - 86400
        new_cache = {k: v for k, v in reminders.items() if v > cutoff}
        if len(new_cache) != len(reminders):
            updated = True
        if updated:
            save_reminders_cache(new_cache, user_id=chat_id)
    except Exception as e:
        logging.error("❌ Error in check_calendar_reminders: %s", e)


# ── Calendar, Schedule & Activity Stream helpers ──────────────────────────────

async def fetch_user_schedule_api(timestamp: Optional[int] = None, user_id: Optional[int] = None, session: Optional[object] = None) -> list[dict]:
    """Fetch the user's weekly semester timetable (`/users/{id}/schedule`) via JSON:API.
    Note: `/schedule` does not accept `page[limit]`, so we call `studip_api_get` directly.
    """
    uid = await studip_api_user_id(session=session, user_id=user_id)
    params = {"filter[timestamp]": int(timestamp)} if timestamp else None
    payload = await studip_api_get(f"/users/{uid}/schedule", params=params, session=session, user_id=user_id)
    slots = []
    for item in payload.get("data", []):
        attr = item.get("attributes", {})
        rec = attr.get("recurrence") or {}
        owner_id = _api_rel_id(item, "owner")
        locs = attr.get("locations") or []
        loc_str = ", ".join(str(x).strip() for x in locs if str(x).strip())
        slots.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "title": (attr.get("title") or "Unknown Course").strip(),
            "description": (attr.get("description") or "").strip(),
            "start": attr.get("start") or "00:00",
            "end": attr.get("end") or "00:00",
            "weekday": int(attr.get("weekday") or 1),
            "location": loc_str,
            "recurrence": rec,
            "course_id": owner_id,
        })
    slots.sort(key=lambda s: (s["weekday"], s["start"], s["title"]))
    return slots


async def _get_calendar_events_api(week_start, days: int = 7, user_id: Optional[int] = None, session: Optional[object] = None) -> list[dict]:
    """Fetch and merge calendar events for `[week_start, week_start + days)` from:
      1. `/users/{id}/schedule` (recurring `seminar-cycle-dates` + schedule entries)
      2. `/courses/{cid}/events` (concrete `course-events` with `is-cancelled`, `location`, `topics`)
      3. `/users/{id}/events` (personal user calendar events)
    """
    uid = await studip_api_user_id(session=session, user_id=user_id)
    week_end = week_start + timedelta(days=days)
    mid_dt = datetime.combine(week_start + timedelta(days=max(0, days // 2)), datetime.min.time(), tzinfo=TZ_BERLIN)
    mid_ts = int(mid_dt.timestamp())
    now = datetime.now(TZ_BERLIN)

    schedule_slots = await fetch_user_schedule_api(timestamp=mid_ts, user_id=user_id, session=session)

    # Map (course_id, weekday, start_hm) -> cycle location & recurrence info
    cycle_locs: dict[tuple, str] = {}
    course_ids: set[str] = set()
    for slot in schedule_slots:
        cid = slot.get("course_id")
        if cid:
            course_ids.add(cid)
            if slot.get("location"):
                cycle_locs[(cid, slot["weekday"], slot["start"][:5])] = slot["location"]

    events: list[dict] = []
    seen_keys: set[tuple] = set()

    def _status_for(start_dt: datetime, end_dt: datetime, is_cancelled: bool) -> str:
        if is_cancelled:
            return "cancelled"
        if end_dt < now:
            return "past"
        if start_dt <= now <= end_dt:
            return "ongoing"
        return "upcoming"

    # 1. Fetch concrete course-events for all schedule courses in parallel
    async def _fetch_course_evs(cid: str) -> tuple[str, list]:
        try:
            data, _ = await studip_api_get_all(f"/courses/{cid}/events", session=session, user_id=user_id)
            return cid, data
        except Exception as exc:
            logging.debug("_get_calendar_events_api: /courses/%s/events failed: %s", cid, exc)
            return cid, []

    course_results = await asyncio.gather(*(_fetch_course_evs(cid) for cid in sorted(course_ids)))
    courses_with_concrete_events: set[str] = set()

    for cid, ev_list in course_results:
        if ev_list:
            courses_with_concrete_events.add(cid)
        for ev in ev_list:
            attr = ev.get("attributes", {})
            start_dt = _api_berlin(attr.get("start"))
            end_dt = _api_berlin(attr.get("end"))
            if not start_dt or not end_dt:
                continue
            if not (week_start <= start_dt.date() < week_end):
                continue
            title = (attr.get("title") or "Unknown Event").strip()
            start_hm = start_dt.strftime("%H:%M")
            loc = (attr.get("location") or "").strip() or cycle_locs.get((cid, start_dt.isoweekday(), start_hm), "")
            is_cancelled = bool(attr.get("is-cancelled", False))
            meta = ev.get("meta") or {}
            raw_topics = meta.get("topics") or []
            titles = ((t.get("title") if isinstance(t, dict) else str(t)) for t in raw_topics)
            topics = [title.strip() for title in titles if title]
            desc = (attr.get("description") or "").strip()
            key = (start_dt.isoformat(), clean_course_title(title).lower())
            seen_keys.add(key)
            events.append({
                "id": ev.get("id"),
                "title": title,
                "day": start_dt.strftime("%A, %d.%m.%Y"),
                "time": f"{start_dt.strftime('%H:%M')} – {end_dt.strftime('%H:%M')}",
                "location": loc,
                "url": f"{BASE_URL}/dispatch.php/course/times?cid={cid}",
                "date_key": start_dt.date(),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "status": _status_for(start_dt, end_dt, is_cancelled),
                "is_cancelled": is_cancelled,
                "categories": (attr.get("categories") or "").strip(),
                "description": desc,
                "topics": topics,
                "course_id": cid,
            })

    # 2. Expand seminar-cycle-dates for any slot whose course did not return concrete events
    for slot in schedule_slots:
        cid = slot.get("course_id")
        if cid and cid in courses_with_concrete_events:
            continue
        rec = slot.get("recurrence") or {}
        dtstart = _api_berlin(rec.get("DTSTART"))
        until = _api_berlin(rec.get("UNTIL"))
        interval = int(rec.get("INTERVAL") or 1)
        exdates = {
            moment.date()
            for moment in (_api_berlin(x) for x in (rec.get("EXDATES") or []))
            if moment is not None
        }
        for offset in range(days):
            d = week_start + timedelta(days=offset)
            if d.isoweekday() != slot["weekday"]:
                continue
            if dtstart and d < dtstart.date():
                continue
            if until and d > until.date():
                continue
            if dtstart and interval > 1:
                weeks_diff = (d - dtstart.date()).days // 7
                if weeks_diff % interval != 0:
                    continue
            sh, sm = (int(x) for x in slot["start"][:5].split(":"))
            eh, em = (int(x) for x in slot["end"][:5].split(":"))
            start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=TZ_BERLIN)
            end_dt = datetime(d.year, d.month, d.day, eh, em, tzinfo=TZ_BERLIN)
            is_cancelled = d in exdates
            title = slot["title"]
            key = (start_dt.isoformat(), clean_course_title(title).lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            events.append({
                "id": f"sch_{slot.get('id')}_{d.isoformat()}",
                "title": title,
                "day": start_dt.strftime("%A, %d.%m.%Y"),
                "time": f"{start_dt.strftime('%H:%M')} – {end_dt.strftime('%H:%M')}",
                "location": slot.get("location") or "",
                "url": f"{BASE_URL}/dispatch.php/course/times?cid={cid}" if cid else "",
                "date_key": d,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "status": _status_for(start_dt, end_dt, is_cancelled),
                "is_cancelled": is_cancelled,
                "categories": "",
                "description": slot.get("description") or "",
                "topics": [],
                "course_id": cid,
            })

    # 3. Personal user events (`/users/{id}/events`)
    try:
        user_evs, _ = await studip_api_get_all(f"/users/{uid}/events", {"filter[timestamp]": mid_ts}, session=session, user_id=user_id)
        for uev in user_evs:
            attr = uev.get("attributes", {})
            start_dt = _api_berlin(attr.get("start"))
            end_dt = _api_berlin(attr.get("end"))
            if not start_dt or not end_dt:
                continue
            if not (week_start <= start_dt.date() < week_end):
                continue
            title = (attr.get("title") or "Personal Event").strip()
            key = (start_dt.isoformat(), clean_course_title(title).lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            is_cancelled = bool(attr.get("is-cancelled", False))
            events.append({
                "id": uev.get("id"),
                "title": title,
                "day": start_dt.strftime("%A, %d.%m.%Y"),
                "time": f"{start_dt.strftime('%H:%M')} – {end_dt.strftime('%H:%M')}",
                "location": (attr.get("location") or "").strip(),
                "url": "",
                "date_key": start_dt.date(),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "status": _status_for(start_dt, end_dt, is_cancelled),
                "is_cancelled": is_cancelled,
                "categories": (attr.get("categories") or "").strip(),
                "description": (attr.get("description") or "").strip(),
                "topics": [],
                "course_id": None,
            })
    except Exception as exc:
        logging.debug("_get_calendar_events_api: /users/%s/events skipped: %s", uid, exc)

    events.sort(key=lambda e: e["start_dt"])
    return events


_COURSE_ROOMS_CACHE: dict[str, tuple[float, dict]] = {}  # cid -> (expires_at, rooms_map)
_COURSE_ROOMS_INFLIGHT: dict[str, asyncio.Task] = {}
COURSE_ROOMS_CACHE_SECONDS = 86400
COURSE_ROOMS_FAILURE_CACHE_SECONDS = 1800  # don't refetch a failing page on every view

_WEEKDAY_FULL_NAMES = {
    "monday": 0, "montag": 0, "tuesday": 1, "dienstag": 1, "wednesday": 2, "mittwoch": 2,
    "thursday": 3, "donnerstag": 3, "friday": 4, "freitag": 4, "saturday": 5, "samstag": 5,
    "sunday": 6, "sonntag": 6,
}
# German short forms only count written the way Stud.IP prints them ("Do.",
# "Mo,"), so ordinary words like "do", "so" or "mi" in a note never match.
_WEEKDAY_SHORT_RE = re.compile(r"\b(mo|di|mi|do|fr|sa|so)[.,]", re.IGNORECASE)
_WEEKDAY_SHORT = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


def _parse_weekday(txt: str) -> Optional[int]:
    lowered = txt.lower()
    for name, wday in _WEEKDAY_FULL_NAMES.items():
        if re.search(r"\b" + name + r"\b", lowered):
            return wday
    m = _WEEKDAY_SHORT_RE.search(txt)
    return _WEEKDAY_SHORT[m.group(1).lower()] if m else None


def _parse_course_rooms_page(page_html: str) -> dict:
    """Room assignments from a course details page:
      ("date", datetime.date, "HH:MM") -> room_name
      ("recurring", weekday_int, "HH:MM") -> room_name
      ("course_default", None) -> room_name, only when the course uses a
        single room (with several, a fallback could confidently show the wrong one)
    """
    soup = BeautifulSoup(page_html, "html.parser")
    rooms_map: dict = {}
    all_rooms: set[str] = set()

    for art in soup.find_all("article"):
        h1 = art.find("h1")
        if not h1 or not any(k in h1.get_text().lower() for k in ["room", "raum", "termine", "times"]):
            continue

        for h4 in art.find_all("h4"):
            room_a = h4.find("a")
            room_name = (room_a.get_text(strip=True) if room_a else h4.get_text(strip=True)).strip()
            if not room_name or room_name.lower() in ("no room", "kein raum"):
                continue
            all_rooms.add(room_name)

            ul = h4.find_next_sibling("ul")
            if not ul:
                continue

            for li in ul.find_all("li"):
                txt = li.get_text(" ", strip=True)
                time_m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", txt)
                if not time_m:
                    continue
                start_hm = time_m.group(1).zfill(5)

                date_m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", txt)
                is_from = bool(re.search(r"(from|ab)\s+\d{1,2}\.\d{1,2}\.\d{2,4}", txt, re.IGNORECASE))

                if date_m and not is_from:
                    d, m, y = map(int, date_m.groups())
                    if y < 100:
                        y += 2000
                    try:
                        rooms_map[("date", date(y, m, d), start_hm)] = room_name
                    except ValueError:
                        pass
                else:
                    weekday = _parse_weekday(txt)
                    if weekday is not None:
                        rooms_map[("recurring", weekday, start_hm)] = room_name

    if len(all_rooms) == 1:
        rooms_map[("course_default", None)] = next(iter(all_rooms))
    return rooms_map


async def _download_course_rooms_map(session, cid: str) -> dict:
    s = session or state.global_session
    if not s:
        try:
            s = await login_studip()
        except Exception:
            logging.warning("Stud.IP login failed while loading course rooms", exc_info=True)
            return {}

    url = f"{BASE_URL}/dispatch.php/course/details?sem_id={cid}"
    rooms_map: dict = {}
    ttl = COURSE_ROOMS_CACHE_SECONDS
    try:
        async with await s.get(url) as r:
            if r.status != 200:
                logging.warning("Course rooms page for %s returned HTTP %s", cid, r.status)
                ttl = COURSE_ROOMS_FAILURE_CACHE_SECONDS
            else:
                rooms_map = _parse_course_rooms_page(await r.text())
    except Exception as exc:
        logging.warning("_fetch_course_rooms_map error for cid %s: %s", cid, exc)
        ttl = COURSE_ROOMS_FAILURE_CACHE_SECONDS
    _COURSE_ROOMS_CACHE[cid] = (time.time() + ttl, rooms_map)
    return rooms_map


async def _fetch_course_rooms_map(session, cid: str) -> dict:
    """Cached room assignments of a course (see _parse_course_rooms_page).
    Concurrent callers for the same course share a single download."""
    if not cid:
        return {}
    cached = _COURSE_ROOMS_CACHE.get(cid)
    if cached and time.time() < cached[0]:
        return cached[1]
    task = _COURSE_ROOMS_INFLIGHT.get(cid)
    if task is None:
        task = asyncio.create_task(_download_course_rooms_map(session, cid))
        _COURSE_ROOMS_INFLIGHT[cid] = task
        def _forget(_task: asyncio.Task, c: str = cid) -> None:
            _COURSE_ROOMS_INFLIGHT.pop(c, None)

        task.add_done_callback(_forget)
    return await asyncio.shield(task)


async def get_calendar_events(session, week_start, days: int = 7, user_id: Optional[int] = None) -> list:
    """Fetch calendar events for `days` days starting at `week_start` natively from Stud.IP.

    Retrieves events and cancellations via JSON:API, and enriches room allocations
    from course details. Fully eliminates dependency on external iCal URLs.
    """
    events = await _get_calendar_events_api(week_start, days=days, user_id=user_id, session=session)
    if not events:
        return []

    cids_to_fetch: set[str] = {
        ev["course_id"]
        for ev in events
        if ev.get("course_id") and not ev.get("location")
    }

    if cids_to_fetch:
        tasks = [_fetch_course_rooms_map(session, cid) for cid in cids_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cid_rooms = {
            cid: res for cid, res in zip(cids_to_fetch, results)
            if isinstance(res, dict)
        }

        for ev in events:
            if not ev.get("location") and ev.get("course_id"):
                cid = ev["course_id"]
                rooms = cid_rooms.get(cid, {})
                sdt = ev.get("start_dt")
                if rooms and sdt:
                    start_hm = sdt.strftime("%H:%M")
                    date_key = ("date", sdt.date(), start_hm)
                    rec_key = ("recurring", sdt.weekday(), start_hm)
                    loc = rooms.get(date_key) or rooms.get(rec_key) or rooms.get(("course_default", None))
                    if loc:
                        ev["location"] = loc

    return events


def _format_event_markdown_block(ev: dict) -> str:
    """Format a single calendar event block for daily/weekly Markdown views,
    including cancellation status, topics, and session notes from JSON:API."""
    original_title = ev.get("title", "")
    is_cancelled = bool(ev.get("is_cancelled", False))
    status = "🚫" if is_cancelled else _status_emoji(ev.get("status") or "")
    course_icon = _get_course_icon(original_title)
    display_title = clean_course_title(original_title)
    loc_icon = _get_location_emoji(ev.get("location", ""))
    duration = ev["time"]
    cancelled_tag = " *(CANCELLED)*" if is_cancelled else ""

    lines = [
        f"{status} {course_icon} *{_md(display_title)}*{cancelled_tag}",
        f"   🕒 `{duration}`",
        f"   {loc_icon} {_md(_safe_loc(ev.get('location', '')))}",
    ]
    topics = ev.get("topics") or []
    if topics:
        safe_topics = ", ".join(str(t).replace("*", "").replace("_", "").replace("`", "").replace("[", "(") for t in topics[:2])
        lines.append(f"   📌 _Topic: {safe_topics}_")
    desc = (ev.get("description") or "").strip()
    if desc:
        safe_desc = desc.replace("*", "").replace("_", "").replace("`", "").replace("[", "(").replace("\n", " ")[:120]
        lines.append(f"   📝 _{safe_desc}_")
    return "\n".join(lines)


_MENU_HIGHLIGHT_CACHE: dict[tuple, Optional[str]] = {}  # (date, menu_path) -> "Falafel Bowl & dessert"


async def _lunch_menu_highlight(lunch: Optional[dict], session) -> Optional[str]:
    """Today's main dishes at the lunch break's mensa: the Stud.IP widget
    (with categories) where it covers the mensa, else the Studierendenwerk's
    public Speiseplan (Wechloy). Only today's menu is shown."""
    if not lunch or lunch["date_key"] != berlin_now().date():
        return None
    key = (lunch["date_key"], lunch.get("menu_path") or lunch.get("menu_loc_id"))
    if key in _MENU_HIGHLIGHT_CACHE:
        return _MENU_HIGHLIGHT_CACHE[key]
    highlight = None
    if lunch.get("menu_path") and session is not None:
        try:
            highlight = menu_highlight(await fetch_todays_menu_items(session, lunch["menu_path"]))
        except Exception as e:
            logging.warning("Stud.IP mensa menu unavailable: %s", e)
    if not highlight and lunch.get("menu_loc_id"):
        titles = await fetch_menu_titles(lunch["menu_loc_id"], lunch["date_key"])
        highlight = menu_highlight([{"name": t} for t in titles])
    if highlight:  # failures are retried on the next view
        _MENU_HIGHLIGHT_CACHE[key] = highlight
    return highlight


def _plan_entry_lines(entry: dict, indent: str = "", menu: Optional[str] = None, remaining: bool = False) -> list[str]:
    """Markdown lines for one study window or the lunch break."""
    span = f"`{entry['start_dt']:%H:%M}–{entry['end_dt']:%H:%M}`"
    if entry.get("kind") == "lunch":
        dishes = f" ({_md(menu)})" if menu else ""
        return [f"{indent}🥗 {span} Lunch at {_md(entry['mensa'])}{dishes}"]
    if remaining:
        length = f" 🟢 _({_format_hours(entry['duration_minutes'])} left, available now)_"
    else:
        length = f" ({_format_hours(entry['duration_minutes'])})"
    desc = entry.get("description", "")
    desc_formatted = desc[0].upper() + desc[1:] if desc else "Study window"
    lines = [f"{indent}🕒 {span}{length} - {_md(desc_formatted)}"]
    kind = entry.get("study_type") or {}
    details = []
    if kind:
        details.append(f"{kind['emoji']} {kind['label']}")
    note = travel_note(entry)
    if note:
        details.append(note)
    if entry.get("study_spot"):
        details.append(f"📍 {entry['study_spot']}")
    if details:
        lines.append(f"{indent}   " + _md(" · ".join(details)))
    return lines


async def send_daily_calendar(sender, events: list, target_date, week_start):
    """Send a specific day's schedule with a 'Week Plan' button. Matches backup UI."""
    today_events = [ev for ev in events if ev["date_key"] == target_date]
    weather = await get_weather_snapshot(target_date)
    weather_block = f"\n\n{build_weather_text_line(weather)}" if weather else ""

    if not today_events:
        text = (
            "🎉 *No Events Today!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🗓️ *{target_date:%A, %d %B %Y}*"
            f"{weather_block}"
        )
    else:
        # Header
        header = (
            "✨ *Today's Schedule* ✨\n"
            f"📅 *{target_date:%A, %d %B %Y}*\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        
        lines = [header]
        course_blocks = [_format_event_markdown_block(ev) for ev in today_events]
        lines.append("\n\n".join(course_blocks))
        for switch in tight_campus_switches(today_events, user_travel(target_date)):
            lines.append("\n" + _md(switch_warning(switch)))
        lines.append("━━━━━━━━━━━━━━━━━━")
        text = "\n".join(lines) + weather_block

    now = datetime.now(TZ_BERLIN)
    plan = plan_study_day(events, target_date, reference_now=now if target_date == now.date() else None)
    study_windows, lunch = plan["windows"], plan["lunch"]
    if study_windows or lunch:
        menu = await _lunch_menu_highlight(lunch, get_user_session())
        gap_lines = ["\n📚 *Good Times to Study*", "━━━━━━━━━━━━━━━━━━"]
        for entry in sorted(study_windows + ([lunch] if lunch else []), key=lambda e: e["start_dt"]):
            gap_lines.extend(_plan_entry_lines(entry, menu=menu, remaining=bool(entry.get("is_remaining"))))
        text += "\n" + "\n".join(gap_lines)
    elif target_date == now.date() and target_date.weekday() < 5:
        all_today_windows = find_study_windows_for_day(events, target_date, reference_now=None)
        if all_today_windows:
            gap_lines = [
                "\n📚 *Good Times to Study*",
                "━━━━━━━━━━━━━━━━━━",
                "✅ _All scheduled study windows for today have ended._",
            ]
            text += "\n" + "\n".join(gap_lines)

    keyboard = [
        [
            InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly"),
            InlineKeyboardButton("🗓️ Timetable", callback_data="calendar_timetable"),
        ],
        [
            InlineKeyboardButton("⚡ Activity Stream", callback_data="activity_stream"),
            InlineKeyboardButton("🔔 Upcoming", callback_data="upcoming_dashboard"),
        ],
        [
            InlineKeyboardButton("📚 My Exam Dates", callback_data="exam_dates_list"),
            InlineKeyboardButton("🔊 Listen", callback_data="calendar_today_voice"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(sender, "edit_message_text"):
        try:
            await sender.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except TelegramError:
            pass
    await sender.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


def _week_tasks_for_range(week_start, week_end, user_id: Optional[int] = None) -> list:
    """The user's own tasks due within [week_start, week_end] (inclusive),
    sorted by due time. `user_id` defaults to the user the current update
    acts for; other students' tasks are never included."""
    user_id = user_id if user_id is not None else current_user_id()
    try:
        tasks = [
            t for t in load_tasks()
            if t.get("user_id") == user_id and t.get("due_time")
            and week_start <= datetime.fromisoformat(t["due_time"]).astimezone(TZ_BERLIN).date() <= week_end
        ]
    except Exception as e:
        logging.warning("_week_tasks_for_range: tasks fetch failed: %s", e)
        return []
    tasks.sort(key=lambda t: t["due_time"])
    return tasks
# "Good Times to Study" hints (daily view, weekly view, morning summary, narration)
STUDY_WINDOW_DAY_START = dtime(9, 0)
STUDY_WINDOW_DAY_END = dtime(19, 0)
STUDY_WINDOW_BETWEEN_MIN_MINUTES = 60  # a gap squeezed between two classes
STUDY_WINDOW_EDGE_MIN_MINUTES = 60  # before the first / after the last class
# The study pack goes out when a block starts; a watcher cycle this late still sends it.
STUDY_PACK_LATE_MINUTES = 10
STUDY_WINDOW_REMAINING_MIN_MINUTES = 30  # today: still worth showing a window already under way

FREE_DAY_SESSIONS = [
    (dtime(9, 30), dtime(12, 30), "morning focus session"),
    (dtime(13, 30), dtime(16, 30), "afternoon focus session"),
    (dtime(17, 0), dtime(19, 0), "evening review session"),
]


def find_study_windows_for_day(events: list, target_date: date, reference_now: Optional[datetime] = None,
                               user_id: Optional[int] = None) -> list[dict]:
    """The day's study windows (see plan_study_day, which also returns the lunch break)."""
    return plan_study_day(events, target_date, reference_now, user_id)["windows"]


def user_travel(day: date, user_id: Optional[int] = None) -> dict:
    """How long the student (default: the user the update acts for) needs
    between the campuses on `day`: their bike/bus setting, by bus when the
    bot has seen a wet forecast for that day."""
    uid = user_id if user_id is not None else current_user_id()
    weather = cached_weather_snapshot(day)
    return travel_for(get_travel_mode(uid) if uid is not None else "bus", bool(weather and weather.get("is_wet")))


def plan_study_day(events: list, target_date: date, reference_now: Optional[datetime] = None,
                   user_id: Optional[int] = None) -> dict:
    """Find good study windows for a single day:
      - Divided focus sessions (Mon-Fri) if no classes are scheduled.
      - Free time before the first class (09:00 to first class start, if >= 60 mins).
      - Free time between classes (gaps >= 60 mins).
      - Free time after the last class (last class end to 19:00, if >= 60 mins).
    Bounds are the STUDY_WINDOW_* constants. study_day.plan_day then takes
    the travel time between campuses off, places the lunch break and adds
    campus, study spot and work type to each window.
    Returns {"windows": [...], "lunch": {...} or None}.
    If `reference_now` is provided and target_date == reference_now.date(),
    dynamically excludes expired windows and updates ongoing windows to reflect
    remaining time today.
    """
    day_events = [
        ev for ev in events
        if ev.get("date_key") == target_date and not ev.get("is_cancelled")
        and ev.get("start_dt") and ev.get("end_dt")
    ]

    day_study_start = datetime.combine(target_date, STUDY_WINDOW_DAY_START, tzinfo=TZ_BERLIN)
    day_study_end = datetime.combine(target_date, STUDY_WINDOW_DAY_END, tzinfo=TZ_BERLIN)

    windows: list[dict[str, Any]] = []

    if not day_events:
        if target_date.weekday() < 5:  # Mon to Fri
            for start_t, end_t, desc in FREE_DAY_SESSIONS:
                s_dt = datetime.combine(target_date, start_t, tzinfo=TZ_BERLIN)
                e_dt = datetime.combine(target_date, end_t, tzinfo=TZ_BERLIN)
                windows.append({
                    "date_key": target_date,
                    "start_dt": s_dt,
                    "end_dt": e_dt,
                    "duration_minutes": int((e_dt - s_dt).total_seconds() / 60),
                    "description": desc,
                    "after": "morning" if start_t.hour < 12 else "lunch break",
                    "before": "evening" if end_t.hour >= 18 else "afternoon break",
                    "is_remaining": False,
                })
    else:
        busy_intervals: list[dict[str, Any]] = []
        for ev in sorted(day_events, key=lambda e: e["start_dt"]):
            s, e = ev["start_dt"], ev["end_dt"]
            title = clean_course_title(ev.get("title", "")) or "class"
            loc, cid = ev.get("location") or "", ev.get("course_id")
            if not busy_intervals:
                busy_intervals.append({"start": s, "end": e, "first_title": title, "last_title": title,
                                       "first_location": loc, "last_location": loc, "first_cid": cid, "last_cid": cid})
            else:
                last = busy_intervals[-1]
                if s <= last["end"]:
                    if e >= last["end"]:
                        last["last_title"], last["last_location"], last["last_cid"] = title, loc, cid
                    last["end"] = max(last["end"], e)
                else:
                    busy_intervals.append({"start": s, "end": e, "first_title": title, "last_title": title,
                                           "first_location": loc, "last_location": loc, "first_cid": cid, "last_cid": cid})

        # 1. Before first class
        first_busy = busy_intervals[0]
        if first_busy["start"] > day_study_start:
            mins = (min(first_busy["start"], day_study_end) - day_study_start).total_seconds() / 60
            if mins >= STUDY_WINDOW_EDGE_MIN_MINUTES:
                first_title = first_busy["first_title"]
                windows.append({
                    "date_key": target_date,
                    "start_dt": day_study_start,
                    "end_dt": min(first_busy["start"], day_study_end),
                    "duration_minutes": int(mins),
                    "description": f"before {first_title}",
                    "after": "morning",
                    "before": first_title,
                    "before_location": first_busy["first_location"],
                    "before_cid": first_busy["first_cid"],
                    "is_remaining": False,
                })

        # 2. Gaps between classes
        for i in range(len(busy_intervals) - 1):
            gap_s = busy_intervals[i]["end"]
            gap_e = busy_intervals[i + 1]["start"]
            mins = (gap_e - gap_s).total_seconds() / 60
            if mins >= STUDY_WINDOW_BETWEEN_MIN_MINUTES:
                prev_title = busy_intervals[i]["last_title"]
                next_title = busy_intervals[i + 1]["first_title"]
                windows.append({
                    "date_key": target_date,
                    "start_dt": gap_s,
                    "end_dt": gap_e,
                    "duration_minutes": int(mins),
                    "description": f"between {prev_title} and {next_title}",
                    "after": prev_title,
                    "before": next_title,
                    "after_location": busy_intervals[i]["last_location"],
                    "before_location": busy_intervals[i + 1]["first_location"],
                    "after_cid": busy_intervals[i]["last_cid"],
                    "before_cid": busy_intervals[i + 1]["first_cid"],
                    "is_remaining": False,
                })

        # 3. After last class
        last_busy = busy_intervals[-1]
        if last_busy["end"] < day_study_end:
            study_after_start = max(last_busy["end"], day_study_start)
            mins = (day_study_end - study_after_start).total_seconds() / 60
            if mins >= STUDY_WINDOW_EDGE_MIN_MINUTES:
                last_title = last_busy["last_title"]
                windows.append({
                    "date_key": target_date,
                    "start_dt": study_after_start,
                    "end_dt": day_study_end,
                    "duration_minutes": int(mins),
                    "description": f"after {last_title}",
                    "after": last_title,
                    "before": "evening",
                    "after_location": last_busy["last_location"],
                    "after_cid": last_busy["last_cid"],
                    "is_remaining": False,
                })

    planned = plan_day(day_events, windows, target_date, STUDY_WINDOW_DAY_START, STUDY_WINDOW_DAY_END,
                       travel=user_travel(target_date, user_id))
    windows, lunch = planned["windows"], planned["lunch"]

    if reference_now and reference_now.date() == target_date:
        if lunch and lunch["end_dt"] <= reference_now:
            lunch = None
        active = []
        for w in windows:
            if w["end_dt"] <= reference_now:
                continue
            if w["start_dt"] < reference_now < w["end_dt"]:
                rem_mins = int((w["end_dt"] - reference_now).total_seconds() / 60)
                if rem_mins >= STUDY_WINDOW_REMAINING_MIN_MINUTES:
                    w_c = dict(w)
                    w_c["start_dt"] = reference_now
                    w_c["duration_minutes"] = rem_mins
                    w_c["is_remaining"] = True
                    active.append(w_c)
            else:
                active.append(w)
        return {"windows": active, "lunch": lunch}

    return {"windows": windows, "lunch": lunch}


def find_weekly_study_gaps(events: list, week_start: Optional[date] = None) -> list:
    """All good study windows Mon-Fri of one week. Shared by the weekly view
    and the voice narration so both always describe the same week. Without
    `week_start`, the Monday of the earliest event's week is used."""
    if week_start is None:
        dates = {ev["date_key"] for ev in events if ev.get("date_key")}
        if not dates:
            return []
        min_date = min(dates)
        week_start = min_date - timedelta(days=min_date.weekday())
    elif isinstance(week_start, datetime):
        week_start = week_start.date()
    all_windows = []
    for offset in range(5):  # Mon to Fri
        d = week_start + timedelta(days=offset)
        all_windows.extend(find_study_windows_for_day(events, d))
    return all_windows


async def send_weekly_calendar(sender, events: list, week_start):
    """Send the weekly schedule with navigation. Matches backup UI."""
    prev_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)
    week_end = week_start + timedelta(days=6)
    week_tasks = _week_tasks_for_range(week_start, week_end)

    if not events:
        text = (
            "🎉 *No Events Planed!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🗓️ *Weekly View*"
        )
    else:
        # Group events by date_key
        events_by_day: dict[Any, list] = {}
        for event in events:
            dk = event["date_key"]
            if dk not in events_by_day:
                events_by_day[dk] = []
            events_by_day[dk].append(event)

        # Header
        header = (
            "✨ *Weekly Schedule* ✨\n"
            f"🗓️ *{week_start:%d %B} - {week_end:%d %B %Y}*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        lines = [header]

        # Process days in order
        for dk in sorted(events_by_day.keys()):
            now_dt = datetime.combine(dk, datetime.min.time())
            day_name = now_dt.strftime("%A")
            day_emoji = _get_day_emoji(day_name)
            
            lines.append(f"\n{day_emoji} *{day_name}, {dk:%d.%m.%Y}*")
            lines.append("━━━━━━━━━━━━━━━━━━")

            day_courses = [_format_event_markdown_block(event) for event in events_by_day[dk]]
            
            lines.append("\n\n".join(day_courses))

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        text = "\n".join(lines)

    if week_tasks:
        task_lines = ["\n✅ *Tasks This Week*", "━━━━━━━━━━━━━━━━━━"]
        for t in week_tasks:
            due = datetime.fromisoformat(t["due_time"]).astimezone(TZ_BERLIN)
            course_line = f" ({_md(t['course'])})" if t.get("course") else ""
            task_lines.append(f"⏰ `{due.strftime('%a %d.%m %H:%M')}` - {_md(t['text'])}{course_line}")
        text += "\n" + "\n".join(task_lines)

    all_study_windows = find_weekly_study_gaps(events, week_start)

    if all_study_windows:
        gap_lines = ["\n📚 *Good Times to Study*", "━━━━━━━━━━━━━━━━━━"]
        by_day: dict[date, list] = {}
        for g in all_study_windows:
            by_day.setdefault(g["date_key"], []).append(g)

        for offset in range(5):
            d = week_start + timedelta(days=offset)
            windows_for_day = by_day.get(d, [])
            if not windows_for_day:
                continue
            gap_lines.append(f"\n🔹 *{d.strftime('%A, %d.%m')}*")
            lunch = plan_study_day(events, d)["lunch"]
            for entry in sorted(windows_for_day + ([lunch] if lunch else []), key=lambda e: e["start_dt"]):
                gap_lines.extend(_plan_entry_lines(entry, indent="   "))
        text += "\n" + "\n".join(gap_lines)

    # Navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("⬅️ Prev Week", callback_data=f"calendar_week|{prev_week.isoformat()}"),
            InlineKeyboardButton("🗓️ This Week", callback_data=f"calendar_week|{(berlin_now().date() - timedelta(days=berlin_now().weekday())).isoformat()}"),
            InlineKeyboardButton("➡️ Next Week", callback_data=f"calendar_week|{next_week.isoformat()}"),
        ],
        [
            InlineKeyboardButton("🗓️ Timetable", callback_data="calendar_timetable"),
            InlineKeyboardButton("⚡ Activity Stream", callback_data="activity_stream"),
        ],
        [
            InlineKeyboardButton("🔊 Listen", callback_data=f"calendar_weekly_voice|{week_start.isoformat()}"),
            InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(sender, "edit_message_text"):
        try:
            await sender.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except TelegramError:
            pass
    await sender.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_calendar_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_week|YYYY-MM-DD navigation callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        _, date_str = query.data.split("|")
        week_start = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())

    try:
        user_id = query.from_user.id
        status_msg = await query.edit_message_text("⏳ Loading schedule...")
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Loading schedule...")
        user_session = get_user_session(user_id)
        session = user_session or state.global_session
        try:
            events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id)
        finally:
            await _stop_loading_animation(stop_evt, anim_task)
        await send_weekly_calendar(query, events, week_start)
    except Exception as e:
        logging.error("Calendar week nav error: %s", e)
        await query.edit_message_text(f"❌ Error: {str(e)[:200]}")


async def handle_calendar_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_today callback -- shows the current day's events."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        status_msg = await query.edit_message_text("📅 Fetching today's events...")
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching today's events...")
        user_session = get_user_session(user_id)
        session = user_session or state.global_session
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        try:
            events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id)
        finally:
            await _stop_loading_animation(stop_evt, anim_task)
        await send_daily_calendar(query, events, today, week_start)
    except Exception as e:
        logging.error("Calendar today error: %s", e)
        await query.edit_message_text(f"❌ Error loading today's events: {str(e)[:200]}")


async def handle_calendar_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_weekly callback -- shows full week."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        status_msg = await query.edit_message_text("🗓️ Fetching weekly schedule...")
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching weekly schedule...")
        user_session = get_user_session(user_id)
        session = user_session or state.global_session
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        try:
            events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id)
        finally:
            await _stop_loading_animation(stop_evt, anim_task)
        await send_weekly_calendar(query, events, week_start)
    except Exception as e:
        logging.error("Calendar weekly error: %s", e)
        await query.edit_message_text(f"❌ Error loading weekly schedule: {str(e)[:200]}")


_ISO_WEEKDAY_LABELS = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    7: "Sunday",
}


async def build_timetable_text(session=None, user_id: Optional[int] = None) -> str:
    """Build a formatted HTML string for the user's recurring semester timetable
    (`/users/{id}/schedule`), enriched with room locations from the current week's
    calendar events when Stud.IP's `/schedule` leaves a room blank."""
    slots = await fetch_user_schedule_api(user_id=user_id, session=session)
    if not slots:
        return "🗓️ <b>Semester Timetable (Stundenplan)</b>\n━━━━━━━━━━━━━━━━━\nℹ️ No recurring timetable slots found for the current semester."

    # Enrich blank locations from the current week's merged calendar events
    room_lookup: dict[tuple, str] = {}
    try:
        today = datetime.now(TZ_BERLIN).date()
        week_start = today - timedelta(days=today.weekday())
        week_evs = await get_calendar_events(session=session or state.global_session, week_start=week_start, days=14, user_id=user_id)
        for ev in week_evs:
            loc = (ev.get("location") or "").strip()
            if not loc:
                continue
            wday = ev["start_dt"].isoweekday()
            hm = ev["start_dt"].strftime("%H:%M")
            room_lookup.setdefault((wday, hm, clean_course_title(ev["title"]).lower()), loc)
    except Exception as exc:
        logging.debug("build_timetable_text room enrichment skipped: %s", exc)

    by_day: dict[int, list[dict]] = {}
    for s in slots:
        by_day.setdefault(s["weekday"], []).append(s)

    lines = ["🗓️ <b>Semester Timetable (Stundenplan)</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for wday in sorted(by_day.keys()):
        day_name = _ISO_WEEKDAY_LABELS.get(wday, f"Day {wday}")
        day_emoji = _get_day_emoji(day_name)
        lines.append(f"\n{day_emoji} <b>{day_name}</b>")
        lines.append("━━━━━━━━━━━━━━━━━━")
        for s in by_day[wday]:
            raw_title = s["title"]
            clean_t = clean_course_title(raw_title)
            icon = _get_course_icon(raw_title)
            start_hm = s["start"][:5]
            end_hm = s["end"][:5]
            loc = s.get("location") or room_lookup.get((wday, start_hm, clean_t.lower()), "")
            loc_icon = _get_location_emoji(loc)
            rec = s.get("recurrence") or {}
            interval = int(rec.get("INTERVAL") or 1)
            freq_badge = " <i>(every 2 weeks)</i>" if interval == 2 else ""
            lines.append(
                f"{icon} <b>{html.escape(clean_t)}</b>{freq_badge}\n"
                f"   🕒 <code>{start_hm} – {end_hm}</code>\n"
                f"   {loc_icon} {html.escape(_safe_loc(loc))}"
            )
    return "\n".join(lines)


async def handle_calendar_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `calendar_timetable` callback -- shows the semester timetable from `/users/{id}/schedule`."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        status_msg = await query.edit_message_text("🗓️ Loading semester timetable...")
        await _show_typing(query)
        stop_evt, anim_task = _start_loading_animation(status_msg, "Loading semester timetable...")
        user_session = get_user_session(user_id)
        session = user_session or state.global_session
        try:
            text = await build_timetable_text(session=session, user_id=user_id)
        finally:
            await _stop_loading_animation(stop_evt, anim_task)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
                InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly"),
            ],
            [
                InlineKeyboardButton("⚡ Activity Stream", callback_data="activity_stream"),
            ],
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logging.error("Timetable error: %s", e)
        await query.edit_message_text(f"❌ Error loading timetable: {str(e)[:200]}")


async def timetable_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/timetable - show the recurring semester timetable from `/users/{id}/schedule`."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    status_msg = await update.message.reply_text("🗓️ Loading semester timetable...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading semester timetable...")
    user_session = get_user_session(user.id)
    session = user_session or state.global_session
    try:
        text = await build_timetable_text(session=session, user_id=user.id)
    except Exception as e:
        logging.error("timetable_command error: %s", e)
        await status_msg.edit_text(f"❌ Error loading timetable: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
            InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly"),
        ],
        [
            InlineKeyboardButton("⚡ Activity Stream", callback_data="activity_stream"),
        ],
    ])
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


_ACTIVITY_TYPE_ICONS = {
    "news": "📢",
    "documents": "📁",
    "forum": "💬",
    "blubber": "💭",
    "wiki": "📝",
    "courseware": "📘",
    "participants": "👥",
    "schedule": "📅",
    "message": "✉️",
}


async def fetch_activity_stream_api(limit: int = 20, activity_filter: Optional[str] = None, user_id: Optional[int] = None, session: Optional[object] = None) -> list[dict]:
    """Fetch recent activities across all courses via `/users/{id}/activitystream`."""
    uid = await studip_api_user_id(session=session, user_id=user_id)
    fetch_limit = max(limit * 3, 40) if activity_filter else limit
    page = await studip_api_get(
        f"/users/{uid}/activitystream",
        params={"page[limit]": min(fetch_limit, 100), "include": "actor,object,context"},
        session=session,
        user_id=user_id,
    )
    included_index = {
        (inc.get("type"), inc.get("id")): inc
        for inc in page.get("included", [])
        if inc.get("type") and inc.get("id")
    }

    results = []
    for item in page.get("data", []):
        attr = item.get("attributes", {})
        act_type = (attr.get("activity-type") or "other").strip().lower()
        if activity_filter:
            if activity_filter == "forum" and act_type not in ("forum", "blubber", "wiki"):
                continue
            elif activity_filter != "forum" and act_type != activity_filter:
                continue

        rels = item.get("relationships", {})
        actor_ref = (rels.get("actor") or {}).get("data") or {}
        ctx_ref = (rels.get("context") or {}).get("data") or {}
        obj_ref = (rels.get("object") or {}).get("data") or {}

        actor_inc = included_index.get((actor_ref.get("type"), actor_ref.get("id")), {})
        ctx_inc = included_index.get((ctx_ref.get("type"), ctx_ref.get("id")), {})
        obj_inc = included_index.get((obj_ref.get("type"), obj_ref.get("id")), {})

        actor_name = (actor_inc.get("attributes") or {}).get("formatted-name") or ""
        ctx_title = (ctx_inc.get("attributes") or {}).get("title") or ""
        courses_dict = state.user_courses_map[user_id] if (user_id and user_id in state.user_courses_map) else state.courses_map
        if not ctx_title and ctx_ref.get("type") == "courses" and ctx_ref.get("id") in courses_dict:
            ctx_title = courses_dict[ctx_ref["id"]]

        obj_attrs = obj_inc.get("attributes") or {}
        obj_title = (obj_attrs.get("title") or obj_attrs.get("name") or "").strip()

        mk_dt = _api_berlin(attr.get("mkdate"))
        when_str = mk_dt.strftime("%d.%m.%Y %H:%M") if mk_dt else ""
        raw_content = attr.get("content") or ""
        summary = _api_html_to_text(raw_content) if raw_content else ""
        if len(summary) > 180:
            summary = summary[:177].rstrip() + "..."

        results.append({
            "id": item.get("id"),
            "title": (attr.get("title") or "Activity").strip(),
            "verb": (attr.get("verb") or "").strip(),
            "activity_type": act_type,
            "actor_name": actor_name.strip(),
            "course_name": clean_course_title(ctx_title) if ctx_title else "",
            "object_title": obj_title,
            "when": when_str,
            "summary": summary,
        })
        if len(results) >= limit:
            break

    return results


async def build_activity_stream_text(activity_filter: Optional[str] = None, limit: int = 10, user_id: Optional[int] = None, session: Optional[object] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Render the Stud.IP activity stream (`/users/{id}/activitystream`) as HTML + filter buttons."""
    items = await fetch_activity_stream_api(limit=limit, activity_filter=activity_filter, user_id=user_id, session=session)
    filter_labels = {
        None: "All Activities",
        "documents": "Files & Documents",
        "news": "Announcements",
        "forum": "Forum, Blubber & Wiki",
    }
    header_sub = filter_labels.get(activity_filter, "All Activities")
    lines = [
        f"⚡ <b>Stud.IP Activity Stream</b> ({html.escape(header_sub)})",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not items:
        lines.append("ℹ️ No matching activities found in the recent stream.")
    else:
        for act in items:
            icon = _ACTIVITY_TYPE_ICONS.get(act["activity_type"], "🔔")
            course_line = f"📘 <b>{html.escape(act['course_name'])}</b>\n" if act["course_name"] else ""
            obj_line = f"📄 <i>{html.escape(act['object_title'])}</i>\n" if (act["object_title"] and act["object_title"] != act["course_name"]) else ""
            summary_line = f"💬 {html.escape(act['summary'])}\n" if (act["summary"] and act["summary"] != act["object_title"]) else ""
            lines.append(
                f"{icon} <b>{html.escape(act['title'])}</b>\n"
                f"{course_line}"
                f"{obj_line}"
                f"{summary_line}"
                f"🕒 <code>{html.escape(act['when'])}</code>"
            )
            lines.append("━━━━━━━━━━━━━━━━━━")

    def _fbtn(label: str, fval: Optional[str]) -> InlineKeyboardButton:
        active = "● " if activity_filter == fval else ""
        cb = f"activity_stream|{fval}" if fval else "activity_stream"
        return InlineKeyboardButton(f"{active}{label}", callback_data=cb)

    keyboard = InlineKeyboardMarkup([
        [
            _fbtn("🌐 All", None),
            _fbtn("📁 Files", "documents"),
            _fbtn("📢 News", "news"),
            _fbtn("💬 Forum/Wiki", "forum"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"activity_stream|{activity_filter}" if activity_filter else "activity_stream"),
            InlineKeyboardButton("📅 Calendar", callback_data="calendar_today"),
        ],
    ])
    return "\n".join(lines), keyboard


async def handle_activity_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `activity_stream` and `activity_stream|<filter>` callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    parts = (query.data or "").split("|", 1)
    activity_filter = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    try:
        user_session = get_user_session(user_id)
        session = user_session or state.global_session
        text, markup = await build_activity_stream_text(activity_filter=activity_filter, limit=10, user_id=user_id, session=session)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        if "Message is not modified" in str(e):
            return
        logging.error("Activity stream error: %s", e)
        await query.edit_message_text(f"❌ Error loading activity stream: {str(e)[:200]}")


async def activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/activity - show the recent Stud.IP activity stream (`/users/{id}/activitystream`)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    status_msg = await update.message.reply_text("⚡ Loading Stud.IP activity stream...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading Stud.IP activity stream...")
    user_session = get_user_session(user.id)
    session = user_session or state.global_session
    try:
        text, markup = await build_activity_stream_text(activity_filter=None, limit=10, user_id=user.id, session=session)
    except Exception as e:
        logging.error("activity_command error: %s", e)
        await status_msg.edit_text(f"❌ Error loading activity stream: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)


# ── Feature 4: Automatic Course Cancellation, Reschedule & Room Change Alerts ──

_ALERT_RULE = "━━━━━━━━━━━━━━━━━"


def _alert_box(heading: str, *lines: str) -> str:
    """Boxed HTML alert: heading, rule, the given lines (each ending in a
    newline), rule."""
    return f"{heading}\n{_ALERT_RULE}\n" + "".join(lines) + _ALERT_RULE


def _room_line(location: str) -> str:
    return f"📍 <b>Room:</b> {html.escape(location)}\n" if location else ""


def _session_change_alert(title: str, date_str: str, cur_time: str, cur_loc: str,
                          cur_cancelled: bool, prev: Optional[dict]) -> Optional[str]:
    """Alert text for an upcoming course session compared with its last seen
    state, or None when nothing worth telling the user changed."""
    course = f"📘 <b>Course:</b> {html.escape(title)}\n"
    if prev is None:
        # First sighting: only alert if already explicitly marked as cancelled
        if not cur_cancelled:
            return None
        return _alert_box("🚫 <b>COURSE SESSION CANCELLED</b>", course, f"📅 <b>Date:</b> {date_str}\n",
                          f"🕒 <b>Time:</b> <code>{cur_time}</code>\n", _room_line(cur_loc))

    was_cancelled = bool(prev.get("is_cancelled", False))
    old_loc = (prev.get("location") or "").strip()
    old_time = prev.get("time") or cur_time
    old_date = prev.get("date") or date_str
    if cur_cancelled and not was_cancelled:
        return _alert_box("🚫 <b>COURSE SESSION CANCELLED</b>", course, f"📅 <b>Date:</b> {date_str}\n",
                          f"🕒 <b>Time:</b> <code>{cur_time}</code>\n", _room_line(cur_loc or old_loc))
    if cur_cancelled:
        return None
    if was_cancelled:
        return _alert_box("🟢 <b>COURSE SESSION RESTORED</b>", course, f"📅 <b>Date:</b> {date_str}\n",
                          f"🕒 <b>Time:</b> <code>{cur_time}</code>\n", _room_line(cur_loc or old_loc),
                          "ℹ️ <i>This previously cancelled session will take place.</i>\n")
    if old_loc and cur_loc and old_loc != cur_loc:
        return _alert_box("📍 <b>COURSE ROOM CHANGED</b>", course,
                          f"📅 <b>Date:</b> {date_str} (<code>{cur_time}</code>)\n",
                          f"🚪 <b>Previous Room:</b> <s>{html.escape(old_loc)}</s>\n",
                          f"🏛️ <b>New Room:</b> <b>{html.escape(cur_loc)}</b>\n")
    if old_date != date_str or old_time != cur_time:
        return _alert_box("⏰ <b>COURSE SESSION RESCHEDULED</b>", course, f"📅 <b>Date:</b> {date_str}\n",
                          f"🕒 <b>Previous Time:</b> <s>{html.escape(old_time)}</s>\n",
                          f"🕒 <b>New Time:</b> <b>{html.escape(cur_time)}</b>\n",
                          f"📍 <b>Room:</b> {html.escape(cur_loc or 'TBA')}\n")
    return None


def _diff_course_events(events: list, ev_state: dict, now: datetime) -> tuple[list[str], bool]:
    """Compare concrete course sessions with the stored state.

    Updates ev_state in place (new entries, pruning after 21 days) and returns
    (alert texts, whether ev_state changed).
    """
    alerts = []
    updated = False
    for ev in events:
        start_dt = ev.get("start_dt")
        end_dt = ev.get("end_dt")
        if not start_dt or not end_dt:
            continue
        raw_title = (ev.get("title") or "Course").strip()
        clean_t = clean_course_title(raw_title) or raw_title
        ev_id = ev.get("id") or f"{start_dt.isoformat()}|{clean_t.lower()}"
        cur_cancelled = bool(ev.get("is_cancelled", False))
        cur_loc = (ev.get("location") or "").strip()
        cur_time = f"{start_dt:%H:%M} - {end_dt:%H:%M}"
        date_str = start_dt.strftime("%a, %d.%m.%Y")
        prev = ev_state.get(ev_id)

        if end_dt >= now:
            alert = _session_change_alert(clean_t, date_str, cur_time, cur_loc, cur_cancelled, prev)
            if alert:
                alerts.append(alert)

        new_entry = {
            "is_cancelled": cur_cancelled,
            "location": cur_loc,
            "time": cur_time,
            "date": date_str,
            "ts": int(now.timestamp()),
        }
        if prev != new_entry:
            ev_state[ev_id] = new_entry
            updated = True

    cutoff_ts = int(now.timestamp()) - 21 * 86400
    for k in [k for k, v in ev_state.items() if isinstance(v, dict) and v.get("ts", 0) < cutoff_ts]:
        ev_state.pop(k, None)
        updated = True
    return alerts, updated


def _timetable_map(slots: list) -> dict:
    """Weekly timetable slots keyed by slot id, in the stored format."""
    result = {}
    for s in slots:
        sid = s.get("id") or f"{s['weekday']}_{s['start']}_{s['title']}"
        result[sid] = {
            "title": clean_course_title(s["title"]) or s["title"],
            "weekday": s["weekday"],
            "weekday_name": _ISO_WEEKDAY_LABELS.get(s["weekday"], f"Day {s['weekday']}"),
            "start": s["start"][:5],
            "end": s["end"][:5],
            "location": s.get("location") or "",
        }
    return result


def _slot_desc(slot: dict) -> str:
    weekday = slot.get("weekday_name", f"Day {slot.get('weekday')}")
    return f"{weekday} {slot.get('start')}-{slot.get('end')}"


def _diff_timetable(old: dict, new: dict) -> list[str]:
    """Alerts for added, moved and removed weekly timetable slots."""
    alerts = []
    for sid, slot in new.items():
        course = f"📘 <b>Course:</b> {html.escape(slot['title'])}\n"
        if sid not in old:
            alerts.append(_alert_box("📅 <b>NEW COURSE ADDED TO TIMETABLE</b>", course,
                                     f"📆 <b>Day:</b> {slot['weekday_name']}\n",
                                     f"🕒 <b>Time:</b> <code>{slot['start']} - {slot['end']}</code>\n",
                                     _room_line(slot["location"])))
            continue
        prev = old[sid]
        time_changed = (prev.get("weekday"), prev.get("start"), prev.get("end")) != (slot["weekday"], slot["start"], slot["end"])
        loc_changed = bool(prev.get("location") and slot["location"] and prev["location"] != slot["location"])
        if time_changed or loc_changed:
            new_desc = f"{slot['weekday_name']} {slot['start']}-{slot['end']}"
            loc_info = f"\n📍 <b>Room:</b> {html.escape(slot['location'])}" if slot["location"] else ""
            alerts.append(_alert_box("🔄 <b>TIMETABLE SCHEDULE UPDATED</b>", course,
                                     f"🕒 <b>Previous Slot:</b> <s>{html.escape(_slot_desc(prev))}</s>\n",
                                     f"🕒 <b>New Slot:</b> <b>{html.escape(new_desc)}</b>{loc_info}\n"))
    for sid, prev in old.items():
        if sid not in new:
            alerts.append(_alert_box("ℹ️ <b>COURSE REMOVED FROM TIMETABLE</b>",
                                     f"📘 <b>Course:</b> {html.escape(prev.get('title', 'Unknown'))}\n",
                                     f"🕒 <b>Previous Slot:</b> {html.escape(_slot_desc(prev))}\n"))
    return alerts


async def check_course_schedule_changes(session, bot) -> None:
    """Inspect upcoming course sessions over the next 14 days and weekly timetable slots
    via Stud.IP JSON:API (/users/{id}/schedule, /courses/{cid}/events, /users/{id}/events)
    and notify immediately if:
      - A session is cancelled (🚫 COURSE SESSION CANCELLED)
      - A previously cancelled session is restored (🟢 COURSE SESSION RESTORED)
      - A room / location changes (📍 COURSE ROOM CHANGED)
      - A session time is rescheduled (⏰ COURSE SESSION RESCHEDULED)
      - A new course is added to the weekly timetable (📅 NEW COURSE ADDED TO TIMETABLE)
      - An existing timetable slot changes day/time/room (🔄 TIMETABLE SCHEDULE UPDATED)
      - A course slot is removed from the weekly timetable (ℹ️ COURSE REMOVED FROM TIMETABLE)
    """
    now = datetime.now(TZ_BERLIN)
    wa_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])

    async def send(alert_text):
        await broadcast(bot, alert_text, parse_mode="HTML", disable_notification=False, reply_markup=wa_kb)

    # 1. Concrete course & user calendar events (14 days ahead)
    try:
        events = await _get_calendar_events_api(now.date(), days=14)
    except Exception as exc:
        logging.debug("check_course_schedule_changes events fetch skipped: %s", exc)
        events = []

    general_cache = load_general_cache()
    ev_state: dict = general_cache.get("course_events_state", {})
    schedule_updates: dict = {}
    alerts, ev_updated = _diff_course_events(events, ev_state, now)
    for alert in alerts:
        await send(alert)

    # 2. Weekly recurring timetable slots (/users/{id}/schedule)
    try:
        current_slots = await fetch_user_schedule_api()
    except Exception as exc:
        logging.debug("check_course_schedule_changes schedule fetch skipped: %s", exc)
        current_slots = None

    sched_updated = False
    if current_slots is not None:
        sched_state: dict = general_cache.get("user_schedule_slots", {})
        new_sched_map = _timetable_map(current_slots)
        if sched_state:
            alerts = _diff_timetable(sched_state, new_sched_map)
            for alert in alerts:
                await send(alert)
            sched_updated = bool(alerts)
        else:
            sched_updated = True  # Initial baseline seeding
        schedule_updates["user_schedule_slots"] = new_sched_map

    if ev_updated or sched_updated:
        schedule_updates["course_events_state"] = ev_state
        # alerts were awaited above; write only our keys onto a fresh copy
        update_general_cache(schedule_updates)


async def build_upcoming_dashboard(session, user_id: int) -> str:
    """Aggregate exam registration deadlines, upcoming exam dates, and personal
    task reminders into one chronologically-sorted overview."""
    now = datetime.now(TZ_BERLIN)
    items = []  # list of (sort_datetime, line)

    try:
        open_exams = await get_open_exam_registrations(session)
        seen_units = set()
        for e in open_exams:
            if e["unit_id"] in seen_units or not e.get("end_date"):
                continue
            seen_units.add(e["unit_id"])
            end_dt = datetime.fromisoformat(e["end_date"])
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=TZ_BERLIN)
            items.append((end_dt, f"🔴 <b>Registration closes:</b> {e['title']} – {_format_relative(end_dt, now, show_time=False)}"))
    except Exception as e:
        logging.warning("Upcoming dashboard: exam registration fetch failed: %s", e)

    try:
        registered = await get_registered_exam_schedule(session)
        today = now.date()
        for exam in registered:
            exam_date = datetime.fromisoformat(exam["date"]).date()
            if exam_date < today:
                continue
            if exam.get("start_datetime"):
                dt = datetime.fromisoformat(exam["start_datetime"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ_BERLIN)
            else:
                dt = datetime(exam_date.year, exam_date.month, exam_date.day, tzinfo=TZ_BERLIN)
            items.append((dt, f"📚 <b>Exam:</b> {exam['title']} – {_format_relative(dt, now)}"))
    except Exception as e:
        logging.warning("Upcoming dashboard: exam date fetch failed: %s", e)

    todos = []
    for t in load_tasks():
        if t["user_id"] != user_id:
            continue
        course_suffix = f" [{t['course']}]" if t.get("course") else ""
        if t.get("due_time"):
            dt = datetime.fromisoformat(t["due_time"])
            items.append((dt, f"✅ <b>Task:</b> {t['text']}{course_suffix} – {_format_relative(dt, now)}"))
        else:
            todos.append(f"{t['text']}{course_suffix}")

    items.sort(key=lambda x: x[0])

    lines = ["🔔 <b>Upcoming</b>", "━━━━━━━━━━━━━━━━━"]
    if items:
        lines.extend(line for _, line in items)
    else:
        lines.append("ℹ️ Nothing urgent right now.")

    if todos:
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("📝 <b>To-dos (no date):</b>")
        lines.extend(f"• {t}" for t in todos)

    return "\n".join(lines)


async def handle_upcoming_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle upcoming_dashboard callback -- one combined, chronologically-sorted
    view of exam registration deadlines, exam dates, and personal task reminders."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    status_msg = await query.message.reply_text("🔔 Fetching your upcoming items...", disable_notification=True)
    await _show_typing(query)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Fetching your upcoming items...")

    try:
        session = await login_studip()
        text = await build_upcoming_dashboard(session, user_id)
    except Exception as e:
        logging.error("Upcoming dashboard error: %s", e)
        await query.message.reply_text(f"❌ Error loading upcoming items: {str(e)[:200]}")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    await query.message.reply_text(text, parse_mode="HTML")


async def schedule_fastenroll_job(chat_id: int, sem_id: Optional[str], target_time_str: Optional[str], target_date_str: Optional[str], context: ContextTypes.DEFAULT_TYPE) -> str:
    """Validate, persist and launch a fast-enroll job. Returns the reply text to show the user."""
    from unibot.tasks import _run_fastenroll_task
    from unibot.enrollment import validate_fastenroll_args
    error = validate_fastenroll_args(sem_id, target_time_str, target_date_str)
    if error or sem_id is None or target_time_str is None:  # the validator rejects missing values
        return f"⚠️ {error}"

    existing = fastenroll_tasks.get(chat_id, {}).get(sem_id)
    if existing and not existing.done():
        return f"A fast-enroll job is already scheduled for course {sem_id}. Cancel it first with /fastenroll_cancel {sem_id}."

    # The bot logs into a single shared Stud.IP account regardless of which
    # Telegram chat is talking to it, so the same course must not be scheduled
    # twice from two different chats -- that would fire two redundant enrollment
    # attempts at the same instant.
    all_pending = load_pending()
    for other_chat_id_str, other_jobs in all_pending.items():
        if int(other_chat_id_str) != chat_id and sem_id in other_jobs:
            return (
                f"⚠️ Course {sem_id} is already scheduled from another Telegram chat connected to this bot. "
                "Cancel it there first if you want to reschedule it here."
            )

    when = f"{target_date_str} {target_time_str}" if target_date_str else f"{target_time_str} (next occurrence)"
    save_pending_job(chat_id, sem_id, target_time_str, target_date_str)
    task = asyncio.create_task(_run_fastenroll_task(chat_id, sem_id, target_time_str, target_date_str, context))
    fastenroll_tasks.setdefault(chat_id, {})[sem_id] = task
    return f"🎯 Fast enroll scheduled for course {sem_id} at {when} (Europe/Berlin)."
STUDY_PLAN_EXAM_WINDOW_DAYS = 21


STUDY_PLAN_MIN_PIECE_MINUTES = 20  # don't start a task in the last few minutes of a block
STUDY_PLAN_PREP_URGENT_DAYS = 2  # class prep becomes top priority this many days before the class


STUDY_PLAN_SHORT_SESSION_MINUTES = 45  # per block for a course the student keeps skipping


def schedule_study_sessions(gaps: list, rows: list, exams: list, week_start, short_courses=frozenset()) -> dict:
    """Fill next week's free blocks with concrete tasks (minutes, not whole
    lectures): a long task is split across blocks, short ones share one.
    Work items, per course in teaching order:
    - catch-up: unfinished tasks of lectures already held,
    - exam prep: past papers for courses with an exam within
      STUDY_PLAN_EXAM_WINDOW_DAYS,
    - class prep (dated courses, which expect students to come prepared):
      material of classes up to the Monday after the plan week, due
      before that class; urgent from STUDY_PLAN_PREP_URGENT_DAYS before,
    - preview: the next lecture of a caught-up undated course.
    Each block takes the most urgent item first; within the same urgency
    it stays with the course it started with (less context switching),
    then prefers the course with the most work left / closest exam.
    A course in `short_courses` (names: the student skipped most of its
    recent blocks) gets at most STUDY_PLAN_SHORT_SESSION_MINUTES per block,
    so its sessions are short enough to actually happen.
    Returns {"sessions": [...], "stats": {...}} where stats has the needed
    vs. scheduled minutes per course and class prep that didn't fit."""
    from unibot.study_plan import _split_gaps_into_blocks, _unit_label
    exam_days: dict = {}
    for e in exams:
        cid = e.get("matched_cid")
        if cid and (cid not in exam_days or e["days_until"] < exam_days[cid]):
            exam_days[cid] = e["days_until"]

    prep_until = week_start + timedelta(days=7)
    items = []
    for row in rows:
        cid = row["cid"]
        units = [("catch-up", None, u) for u in row["backlog"]]
        if cid in exam_days:
            units += [("exam prep", None, u) for u in row["exam_practice"]]
        if row["dated"]:
            for u in row["upcoming"]:
                if u["date"] < week_start:
                    units.append(("catch-up", None, u))  # its class happens before the plan week starts
                elif u["date"] <= prep_until:
                    units.append(("class prep", u["date"], u))
        elif row["upcoming"] and not row["backlog"]:
            units.append(("preview", None, row["upcoming"][0]))
        for order, (reason, due, u) in enumerate(units):
            for t in u["tasks"]:
                if t["remaining"] > 0:
                    items.append({
                        "cid": cid, "course": row["name"], "unit": u["file"], "unit_label": _unit_label(u),
                        "task_key": t["key"], "title": t["title"], "remaining": t["remaining"], "total": t["remaining"],
                        "reason": reason, "due": due, "order": order,
                    })

    score = {r["cid"]: r["backlog_minutes"] / 60 + (STUDY_PLAN_EXAM_WINDOW_DAYS + 1 - exam_days[r["cid"]] if r["cid"] in exam_days else 0) for r in rows}
    needed: dict[str, Any] = {}
    for it in items:
        if it["reason"] != "preview":
            needed[it["cid"]] = needed.get(it["cid"], 0) + it["remaining"]

    def urgency(it, day):
        if it["reason"] == "class prep":
            slack = (it["due"] - day).days
            if slack <= 0:
                return 1  # too late to prepare before class: plain catch-up now
            return 0 if slack <= STUDY_PLAN_PREP_URGENT_DAYS else 1
        return 2 if it["reason"] == "preview" else 1

    blocks = _split_gaps_into_blocks(gaps)
    sessions = []
    for start, end in blocks:
        day = start.date()
        left = int((end - start).total_seconds() / 60)
        pieces, block_cid = [], None
        used: dict[str, int] = {}  # minutes per course in this block
        while left >= STUDY_PLAN_MIN_PIECE_MINUTES:
            pending = sorted(
                (it for it in items if it["remaining"] > 0),
                key=lambda it: (urgency(it, day), block_cid is not None and it["cid"] != block_cid, -score.get(it["cid"], 0), it["cid"], it["order"]),
            )
            chosen = None
            for it in pending:
                room = left
                if it["course"] in short_courses:
                    room = min(left, STUDY_PLAN_SHORT_SESSION_MINUTES - used.get(it["cid"], 0))
                    if room < STUDY_PLAN_MIN_PIECE_MINUTES:
                        continue
                take = min(it["remaining"], room)
                if 0 < it["remaining"] - take < STUDY_PLAN_MIN_PIECE_MINUTES:
                    take = it["remaining"] - STUDY_PLAN_MIN_PIECE_MINUTES  # don't leave a crumb for later
                if take >= it["remaining"] or take >= STUDY_PLAN_MIN_PIECE_MINUTES:
                    chosen = (it, take)
                    break
            if not chosen:
                break
            it, take = chosen
            pieces.append({
                "cid": it["cid"], "course": it["course"], "unit": it["unit"], "unit_label": it["unit_label"],
                "task_key": it["task_key"], "title": it["title"], "minutes": take, "reason": it["reason"],
                "task_minutes": it["total"],
            })
            it["remaining"] -= take
            left -= take
            used[it["cid"]] = used.get(it["cid"], 0) + take
            score[it["cid"]] = score.get(it["cid"], 0) - take / 60
            block_cid = block_cid or it["cid"]
        if pieces:
            sessions.append({
                "day": f"{start:%A}",
                "date": day.isoformat(),
                "start": f"{start:%H:%M}",
                "end": f"{end:%H:%M}",
                "pieces": pieces,
            })

    # "part 2/3" labels for tasks split across blocks (task keys are only
    # unique per course: two courses can both have a "20261013" folder)
    counts: dict = {}
    for s in sessions:
        for p in s["pieces"]:
            k = (p["cid"], p["task_key"])
            counts[k] = counts.get(k, 0) + 1
    seen: dict = {}
    for s in sessions:
        for p in s["pieces"]:
            k = (p["cid"], p["task_key"])
            if counts[k] > 1:
                seen[k] = seen.get(k, 0) + 1
                p["part"] = f"{seen[k]}/{counts[k]}"

    scheduled: dict = {}
    for s in sessions:
        for p in s["pieces"]:
            if p["reason"] != "preview":
                scheduled[p["cid"]] = scheduled.get(p["cid"], 0) + p["minutes"]
    missed_prep = sorted({(it["course"], it["due"]) for it in items if it["reason"] == "class prep" and it["remaining"] > 0}, key=lambda x: x[1])
    stats = {
        "needed": needed,
        "scheduled": scheduled,
        "free_minutes": sum(int((e - s).total_seconds() / 60) for s, e in blocks),
        "missed_prep": [(course, due.isoformat()) for course, due in missed_prep],
        "names": {r["cid"]: r["name"] for r in rows},
    }
    return {"sessions": sessions, "stats": stats}
