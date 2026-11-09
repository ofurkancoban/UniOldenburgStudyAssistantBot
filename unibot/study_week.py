"""Beyond the single day: the study windows as a calendar feed for the
student's phone, common free time with classmates who opted in, and the
Sunday report (free time used, deadline streak).

Part of the unibot package.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN, format_hours
from unibot import state
from unibot.auth import acting_as, get_user_session, has_stored_login, is_user_allowed
from unibot.config import CLOUDFLARE_TUNNEL
from unibot.schedule import get_calendar_events, plan_study_day
from unibot.storage import (
    calendar_token,
    get_study_buddy_enabled,
    get_user_name,
    reset_calendar_token,
    set_study_buddy_enabled,
    study_block_log,
    task_outcomes,
    user_for_calendar_token,
)

CALENDAR_FEED_WEEKS = 2          # this week and the next
BUDDY_MIN_MINUTES = 60           # a common window shorter than this is not worth meeting for
TASK_ON_TIME_GRACE_SECONDS = 3600  # "Done" on the deadline reminder still counts as on time
WEEKLY_REPORT_WEEKDAY, WEEKLY_REPORT_HOUR = 6, 18  # Sunday evening


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def plan_entries(user_id: int, week_start: date, session=None) -> list[dict]:
    """The student's study windows and lunch breaks for Mon-Fri of one week
    (see schedule.plan_study_day), from their own timetable; [] without a
    stored login."""
    session = session or get_user_session(user_id)
    if not has_stored_login(session):
        return []
    with acting_as(user_id):
        events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id) or []
        entries: list[dict] = []
        for offset in range(5):
            plan = plan_study_day(events, week_start + timedelta(days=offset), user_id=user_id)
            entries += plan["windows"] + ([plan["lunch"]] if plan["lunch"] else [])
    return sorted(entries, key=lambda e: e["start_dt"])


# ── calendar feed (iCalendar, RFC 5545) ───────────────────────────────────────
def _ics_text(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> list[str]:
    """Lines longer than 75 octets continue on the next line after a space."""
    out: list[str] = []
    current = ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(current)
            current = ch
        else:
            current += ch
    out.append(current)
    return [out[0]] + [" " + part for part in out[1:]]


def _ics_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(entries: list[dict], user_id: int, now: Optional[datetime] = None) -> str:
    """An iCalendar of study windows ("busy", so the phone calendar shows the
    time as taken) and lunch breaks."""
    stamp = _ics_time(now or datetime.now(TZ_BERLIN))
    owner = hashlib.sha256(str(user_id).encode()).hexdigest()[:12]
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//UniAssistant//Study blocks//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:Study blocks", "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for e in entries:
        if e.get("kind") == "lunch":
            summary, kind = f"🥗 Lunch at {e['mensa']}", "lunch"
            description, location = "Lunch break between the study blocks.", e["mensa"]
        else:
            study_type = e.get("study_type") or {}
            summary = f"📚 Study block: {study_type.get('label', 'Study')}"
            kind = "study"
            parts = [e.get("description") or "", study_type.get("hint") or ""]
            if e.get("travel_minutes"):
                parts.append(f"Starts after {e['travel_minutes']} min travel between the campuses.")
            description = " ".join(p for p in parts if p)
            location = e.get("study_spot") or ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:{kind}-{e['start_dt']:%Y%m%dT%H%M}-{owner}@uniassistant",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_time(e['start_dt'])}",
            f"DTEND:{_ics_time(e['end_dt'])}",
            f"SUMMARY:{_ics_text(summary)}",
            f"DESCRIPTION:{_ics_text(description)}",
            "TRANSP:OPAQUE",
            "STATUS:CONFIRMED",
        ]
        if location:
            lines.append(f"LOCATION:{_ics_text(location)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _ics_fold(line)) + "\r\n"


async def calendar_for(user_id: int, today: Optional[date] = None) -> str:
    today = today or datetime.now(TZ_BERLIN).date()
    entries: list[dict] = []
    for week in range(CALENDAR_FEED_WEEKS):
        entries += await plan_entries(user_id, _week_start(today) + timedelta(weeks=week))
    return build_ics([e for e in entries if e["end_dt"].date() >= today], user_id)


async def feed_handler(request: web.Request) -> web.Response:
    """GET /cal/<token>.ics: the student's live calendar feed."""
    token = request.match_info.get("token", "").removesuffix(".ics")
    user_id = user_for_calendar_token(token)
    if user_id is None or not is_user_allowed(user_id):
        return web.Response(status=404, text="Not found")
    try:
        body = await calendar_for(user_id)
    except Exception as e:
        logging.warning("Calendar feed failed for %s: %s", user_id, e)
        return web.Response(status=503, text="Try again later")
    return web.Response(text=body, content_type="text/calendar", charset="utf-8",
                        headers={"Cache-Control": "private, max-age=900"})


def register_calendar_feed(server) -> None:
    server.add_route("GET", "/cal/{token}", feed_handler)


def feed_url(user_id: int) -> Optional[str]:
    """The https feed link, when the web server is reachable from outside."""
    server = state.login_server
    if server is None:
        return None
    base = server.base_url()
    if not base.startswith("https://"):
        return None
    return f"{base}/cal/{calendar_token(user_id)}.ics"


def calendar_sync_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    url = feed_url(user_id)
    lines = ["📅 <b>Calendar sync</b>", "",
             "Your study blocks and lunch breaks for this and next week, shown as <b>busy</b> in your phone's calendar.",
             "", "📄 <b>One tap:</b> open the file below and add it to your calendar."]
    rows = [[InlineKeyboardButton("📄 Send calendar file (.ics)", callback_data="cal_sync|file")]]
    if url:
        webcal = "webcal://" + url.removeprefix("https://")
        lines += ["", "🔄 <b>Live link</b> (updates by itself):",
                  f"Apple Calendar: <code>{html.escape(webcal)}</code>",
                  f"Other apps: <code>{html.escape(url)}</code>"]
        rows.append([InlineKeyboardButton("➕ Add to Google Calendar",
                                          url="https://calendar.google.com/calendar/render?cid=" + quote(webcal, safe=""))])
        if CLOUDFLARE_TUNNEL == "quick":
            lines.append("<i>⚠️ The live link changes when the bot restarts (temporary address); "
                         "the file always works.</i>")
        rows.append([InlineKeyboardButton("🔑 New link (old one stops working)", callback_data="cal_sync|reset")])
    else:
        lines += ["", "<i>The live link is not available right now; the file always works.</i>"]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_calendar_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cal_sync, cal_sync|file, cal_sync|reset."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return
    data = query.data or ""
    action = data.split("|")[1] if "|" in data else ""
    if action == "file":
        await query.answer("Preparing your calendar...")
        body = await calendar_for(user_id)
        await query.message.reply_document(document=io.BytesIO(body.encode("utf-8")), filename="study-blocks.ics",
                                           caption="📅 Open this file to add your study blocks to your calendar.")
        return
    if action == "reset":
        reset_calendar_token(user_id)
        await query.answer("New link created")
    else:
        await query.answer()
    text, markup = calendar_sync_view(user_id)
    if action == "reset":
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── study buddy ───────────────────────────────────────────────────────────────
def common_windows(mine: list[dict], theirs: list[dict], min_minutes: int = BUDDY_MIN_MINUTES) -> list[dict]:
    """Overlaps of two students' study windows (lunch breaks excluded) of at
    least `min_minutes`: {"start_dt", "end_dt", "minutes", "spot"} where spot
    is the study spot when both are on the same campus."""
    out = []
    for a in (e for e in mine if e.get("kind") != "lunch"):
        for b in (e for e in theirs if e.get("kind") != "lunch"):
            start, end = max(a["start_dt"], b["start_dt"]), min(a["end_dt"], b["end_dt"])
            minutes = int((end - start).total_seconds() // 60)
            if minutes >= min_minutes:
                same = a.get("campus") and a.get("campus") == b.get("campus")
                out.append({"start_dt": start, "end_dt": end, "minutes": minutes,
                            "spot": a.get("study_spot") if same else None})
    return sorted(out, key=lambda w: w["start_dt"])


def buddy_candidates(user_id: int) -> list[tuple[int, list[str]]]:
    """Other students who opted in and share at least one course with this
    one: [(user_id, shared course names)]."""
    if not get_study_buddy_enabled(user_id):
        return []
    mine = state.user_courses_map.get(user_id) or {}
    out = []
    for other, courses in state.user_courses_map.items():
        if other == user_id or not get_study_buddy_enabled(other) or not is_user_allowed(other):
            continue
        shared = sorted({mine[cid] for cid in mine.keys() & (courses or {}).keys()})
        if shared:
            out.append((other, shared))
    return out


async def study_buddy_matches(user_id: int, week_start: date, after: Optional[datetime] = None) -> list[dict]:
    """[{"user_id", "name", "courses", "windows"}] of opted-in classmates with
    common free time in that week (only windows ending after `after`)."""
    candidates = buddy_candidates(user_id)
    if not candidates:
        return []
    mine = await plan_entries(user_id, week_start)
    matches = []
    for other, shared in candidates:
        windows = common_windows(mine, await plan_entries(other, week_start))
        if after is not None:
            windows = [w for w in windows if w["end_dt"] > after]
        if windows:
            matches.append({"user_id": other, "name": get_user_name(other) or "A classmate",
                            "courses": shared, "windows": windows})
    return matches


def format_buddy_lines(matches: list[dict], limit: int = 3) -> list[str]:
    lines = []
    for m in matches:
        first = html.escape(str(m["name"]).split()[0])
        lines.append(f"👥 <b>Common free time with {first}</b> ({html.escape(', '.join(m['courses'][:2]))}):")
        for w in m["windows"][:limit]:
            spot = f" · 📍 {html.escape(w['spot'])}" if w.get("spot") else ""
            lines.append(f"   • {w['start_dt']:%a %H:%M}–{w['end_dt']:%H:%M} ({format_hours(w['minutes'])}){spot}")
    if lines:
        lines.append("<i>You could study together in the library.</i>")
    return lines


async def study_buddy_view(user_id: int, now: Optional[datetime] = None) -> tuple[str, InlineKeyboardMarkup]:
    now = now or datetime.now(TZ_BERLIN)
    enabled = get_study_buddy_enabled(user_id)
    lines = ["👥 <b>Study Buddy</b>", "",
             "Find common free time with classmates from your courses who use the bot too.",
             "<i>Only the free windows you share are shown to each other, never your timetable. "
             "Both of you have to turn this on.</i>", "",
             f"Status: {'✅ On' if enabled else '🔲 Off'}"]
    if enabled:
        matches = await study_buddy_matches(user_id, _week_start(now.date()), after=now)
        lines += [""] + (format_buddy_lines(matches) or ["No common free time with an opted-in classmate this week yet."])
    rows = [[InlineKeyboardButton("🔲 Turn off" if enabled else "✅ Turn on", callback_data="buddy|toggle")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def handle_study_buddy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """buddy (show) and buddy|toggle."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return
    await query.answer()
    toggle = query.data == "buddy|toggle"
    if toggle:
        set_study_buddy_enabled(user_id, not get_study_buddy_enabled(user_id))
    text, markup = await study_buddy_view(user_id)
    if toggle:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── Sunday report ─────────────────────────────────────────────────────────────
def deadline_weeks(outcomes: list[dict], now: datetime) -> dict[date, dict]:
    """Per week (its Monday) of a deadline: {"on_time", "missed": [texts]}.
    Deadlines still ahead (or within the grace) are left out."""
    weeks: dict[date, dict] = {}
    for o in outcomes:
        try:
            due = datetime.fromisoformat(o["due"]).astimezone(TZ_BERLIN)
        except (KeyError, TypeError, ValueError):
            continue
        late_after = due.timestamp() + TASK_ON_TIME_GRACE_SECONDS
        done = o.get("done")
        if done is None and now.timestamp() <= late_after:
            continue  # still open, not late yet
        week = weeks.setdefault(_week_start(due.date()), {"on_time": 0, "missed": []})
        if done is not None and done <= late_after:
            week["on_time"] += 1
        else:
            week["missed"].append(o.get("text") or "a deadline")
    return weeks


def deadline_streak(outcomes: list[dict], now: datetime) -> tuple[int, list[str]]:
    """(weeks in a row, up to this one, with every deadline done on time;
    the deadlines missed this week). Weeks without deadlines don't break it."""
    weeks = deadline_weeks(outcomes, now)
    this_week = _week_start(now.date())
    missed_now = weeks.get(this_week, {}).get("missed", [])
    streak = 0
    for monday in sorted((w for w in weeks if w <= this_week), reverse=True):
        if weeks[monday]["missed"]:
            break
        streak += 1
    return streak, missed_now


async def weekly_report(user_id: int, now: Optional[datetime] = None) -> str:
    """The Sunday mini report: this week's free time between classes and
    how much of it went into study blocks, the deadline streak and next
    week's common free time with study buddies."""
    now = now or datetime.now(TZ_BERLIN)
    monday = _week_start(now.date())
    windows = [e for e in await plan_entries(user_id, monday) if e.get("kind") != "lunch"]
    free = sum(w["duration_minutes"] for w in windows)
    week_from = datetime.combine(monday, datetime.min.time(), tzinfo=TZ_BERLIN).timestamp()
    blocks = study_block_log(user_id, week_from, now.timestamp())
    used = sum(b.get("minutes") or 0 for b in blocks if b["status"] == "done")

    lines = [f"📊 <b>Weekly report</b> ({monday:%d.%m}–{monday + timedelta(days=6):%d.%m})", ""]
    if free:
        share = min(100, round(100 * used / free))
        lines.append(f"⏳ Free time between classes this week: <b>{format_hours(free)}</b>; "
                     f"<b>{format_hours(used) if used else '0 min'}</b> of it went into planned study blocks "
                     f"(efficiency: {share}%) {'🎯' if share >= 60 else '💪'}")
    else:
        lines.append("⏳ No free study windows this week.")
    if blocks:
        done = sum(b["status"] == "done" for b in blocks)
        lines.append(f"📚 Study blocks: {done} of {len(blocks)} done")

    streak, missed = deadline_streak(task_outcomes(user_id), now)
    if missed:
        lines.append(f"⚠️ {len(missed)} deadline(s) missed this week: " + html.escape("; ".join(missed[:3])))
    elif streak >= 2:
        lines.append(f"🔥 {streak} weeks in a row with every deadline done on time! ({streak}-week streak)")
    elif streak == 1:
        lines.append("✅ Every deadline this week done on time.")

    buddies = await study_buddy_matches(user_id, monday + timedelta(days=7))
    if buddies:
        lines += ["", "<b>Next week</b>"] + format_buddy_lines(buddies, limit=2)
    return "\n".join(lines)


def weekly_report_due(now: datetime) -> bool:
    return now.weekday() == WEEKLY_REPORT_WEEKDAY and now.hour >= WEEKLY_REPORT_HOUR
