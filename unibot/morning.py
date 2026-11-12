"""Morning summary, spoken daily/weekly plans and voice delivery.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN, aread_bytes, berlin_now, format_hours as _format_hours, html_escape as _h

from unibot import course_registry, db, state
from unibot.preferences import campus_anchor, food_filter, get_pref
from unibot.storage import (
    AI_PERSONAL_OFF_NOTE,
    get_ai_personal_enabled,
    get_cached_outfit_today,
    get_recently_worn_item_ids,
    get_user_name,
    get_wardrobe_entry,
    personal_record,
    save_personal_record,
    load_general_cache,
    tasks_due_between,
    task_due,
    user_tasks,
    record_worn_items,
    save_cached_outfit_today,
    save_general_cache,
)
from unibot.formatting import (
    TELEGRAM_HTML_LIMIT,
    _get_course_icon,
    _get_location_emoji,
    _safe_loc,
    clean_course_title,
    truncate_html,
)
from unibot.telegram_ui import _download_telegram_file, _show_typing
from unibot.auth import (
    ACTIVE,
    acting_as,
    current_user_id,
    get_user_session,
    has_stored_login,
    is_user_admin,
    is_user_allowed,
    lifecycle_state,
)
from unibot.ai import (
    _ai_patience,
    _call_openrouter,
    _pcm_to_ogg_voice,
    personal_ai_allowed,
    synthesize_speech,
)
from unibot.weather import build_weather_text_line, get_weather_snapshot
from unibot.mensa import _friendly_menu_category_label, build_food_recommendation, fetch_dish_photo_url
from unibot.wardrobe import generate_outfit_tryon_image, outfit_caption, pick_outfit_from_wardrobe
from unibot.study_day import WECHLOY, campus_of, switch_warning, tight_campus_switches
from unibot.schedule import (
    _lunch_menu_highlight,
    _week_tasks_for_range,
    find_weekly_study_gaps,
    get_calendar_events,
    plan_study_day,
)


MORNING_AI_TIMEOUT_SECONDS = 60
MORNING_AI_ATTEMPTS = 3


def _private_context_current(user_id: int) -> bool:
    """Check lifecycle generation for work initiated outside a user scope."""
    with acting_as(user_id):
        return db.private_context_is_current(user_id)


def forget_prepared_morning_user(user_id: int) -> None:
    """Remove a student's data from prepared and waiting morning summaries."""
    pending = list(state._prepared_morning.items()) + list(state._morning_delivery_data.items())
    for key, prepared in pending:
        discard_prepared_morning_user(prepared, user_id)
        if not prepared.get("texts"):
            for voice_path in (prepared.get("voice_paths") or {}).values():
                try:
                    os.remove(voice_path)
                except OSError:
                    logging.debug("Could not remove prepared voice", exc_info=True)
            task = state._morning_delivery_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            state._prepared_morning.pop(key, None)
            state._morning_delivery_data.pop(key, None)
            prepared.clear()


def discard_prepared_morning_user(prepared: dict, user_id: int) -> None:
    """Remove one student's content from a prepared summary and its temp audio."""
    for field in ("texts", "photos", "outfits", "voice_paths", "menu_buttons"):
        values = prepared.get(field)
        if not isinstance(values, dict):
            continue
        removed = values.pop(user_id, None)
        values.pop(str(user_id), None)
        if field == "voice_paths" and removed:
            try:
                os.remove(removed)
            except OSError:
                logging.debug("Could not remove deleted user's prepared voice", exc_info=True)


async def prepare_morning_summary(bot, user_ids) -> Optional[dict]:
    """See _prepare_morning_summary. Nobody waits on this (it runs up to
    MORNING_PREPARE_LEAD_MINUTES before delivery), so every AI call inside
    gets a longer timeout and retries instead of falling back to a
    template on the free model's first slow response."""
    token = _ai_patience.set((MORNING_AI_TIMEOUT_SECONDS, MORNING_AI_ATTEMPTS))
    try:
        return await _prepare_morning_summary(bot, user_ids)
    finally:
        _ai_patience.reset(token)


def _format_schedule_section(today_events: list) -> str:
    if not today_events:
        return "📅 <b>No classes today!</b> Enjoy your day. ✨"
    course_blocks = []
    for ev in today_events:
        course_icon = _get_course_icon(ev.get("title", ""))
        display_title = clean_course_title(ev.get("title", ""))
        loc_icon = _get_location_emoji(ev.get("location", ""))
        if ev.get("is_cancelled"):
            course_blocks.append(f"🚫 <s>{_h(display_title)}</s> <b>(cancelled)</b>\n   🕒 <code>{ev['time']}</code>")
            continue
        course_blocks.append(
            f"{course_icon} <b>{_h(display_title)}</b>\n"
            f"   🕒 <code>{ev['time']}</code>\n"
            f"   {loc_icon} {_h(_safe_loc(ev['location']))}"
        )
    warnings = [_h(switch_warning(s)) for s in tight_campus_switches(today_events)]
    return "\n".join(["📅 <b>Today's Schedule:</b>", "\n\n".join(course_blocks)]
                     + (["", *warnings] if warnings else []))


def _format_study_windows_section(events: list, today_date, menu: Optional[str] = None) -> str:
    """The day's study windows with their work type and study spot, and the
    lunch break (with the mensa's main dishes when `menu` is given)."""
    plan = plan_study_day(events, today_date)
    entries = plan["windows"] + ([plan["lunch"]] if plan["lunch"] else [])
    if not entries:
        return ""
    study_lines = ["📚 <b>Good Times to Study:</b>"]
    for g in sorted(entries, key=lambda e: e["start_dt"]):
        span = f"<code>{g['start_dt']:%H:%M}–{g['end_dt']:%H:%M}</code>"
        if g.get("kind") == "lunch":
            dishes = f" ({_h(menu)})" if menu else ""
            study_lines.append(f"🥗 {span} Lunch at {_h(g['mensa'])}{dishes}")
            continue
        desc = g.get("description", "")
        desc_formatted = desc[0].upper() + desc[1:] if desc else "Study window"
        study_lines.append(f"🕒 {span} ({_format_hours(g['duration_minutes'])}) - {_h(desc_formatted)}")
        kind = g.get("study_type") or {}
        details = [f"{kind['emoji']} {kind['label']}"] if kind else []
        if g.get("study_spot"):
            details.append(f"📍 {g['study_spot']}")
        if details:
            study_lines.append("   " + _h(" · ".join(details)))
    return "\n\n" + "\n".join(study_lines)


def _load_today_tasks(today_date, user_id: Optional[int] = None) -> list:
    """The user's own tasks due today (`user_id` defaults to the user the
    current update acts for; without one there are none)."""
    owner = user_id if user_id is not None else current_user_id()
    try:
        return tasks_due_between(user_tasks(owner), today_date, today_date) if owner is not None else []
    except Exception as e:
        logging.error("Summary tasks fetch error: %s", e)
        return []


def _format_tasks_section(today_tasks: list) -> str:
    if not today_tasks:
        return ""
    task_lines = ["✅ <b>Today's Tasks:</b>"]
    for t in today_tasks:
        due = task_due(t)
        if due is None:
            continue
        course_line = f" [{_h(t['course'])}]" if t.get("course") else ""
        task_lines.append(f"⏰ <code>{due.strftime('%H:%M')}</code> - {_h(t['text'])}{course_line}")
    return "\n\n" + "\n".join(task_lines)


def _format_lunch_section(food: Optional[dict]) -> str:
    if food and food.get("dish") and food.get("category"):
        return f"\n\n🍽️ <b>Lunch pick:</b> {_h(food['dish'])} ({_h(_friendly_menu_category_label(food['category']))})"
    return ""


def _format_exam_countdown(exams: list, today_date) -> str:
    """Countdown to every still-upcoming registered exam, not just the nearest
    one. It lives inside the pinned summary, so it updates once a day along
    with everything else."""
    if not exams:
        return ""
    lines = ["⏳ <b>Upcoming Exams</b>"]
    for exam_title, exam_dt in exams:
        days_left = (exam_dt.date() - today_date).days
        if days_left <= 0:
            day_label = "today"
        elif days_left == 1:
            day_label = "tomorrow"
        else:
            day_label = f"in {days_left} days"
        lines.append(f"• <b>{html.escape(exam_title)}</b> {day_label} ({exam_dt:%d.%m.%Y})")
    return "\n\n" + "\n".join(lines)


def _compose_morning_text(today_date, sections: list) -> str:
    header = (
        "☀️ <b>GOOD MORNING!</b> ☀️\n"
        f"🗓️ <b>Today:</b> {today_date:%d %B %Y, %A}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )
    return truncate_html(f"{header}{''.join(sections)}\n\n━━━━━━━━━━━━━━━━━━", TELEGRAM_HTML_LIMIT)


async def _fetch_today_events(session, today_date, user_id: Optional[int] = None) -> tuple[list, list, bool]:
    """(this week's events, today's events, fetch succeeded)."""
    try:
        week_start = today_date - timedelta(days=today_date.weekday())
        events = await get_calendar_events(session=session, week_start=week_start, user_id=user_id)
    except Exception as e:
        logging.error("Summary schedule fetch error: %s", e)
        return [], [], False
    return events, [ev for ev in events if ev["date_key"] == today_date], getattr(events, "complete", True)


async def _fetch_lunch_pick(loc_id: str = "2", user_id: Optional[int] = None) -> Optional[dict]:
    """Same AI-judged pick as the "🍽️ What should I eat?" button, at the
    mensa with Speiseplan `loc_id` ("2" Uhlhornsweg, "3" Wechloy). A failure
    just omits the section rather than blocking the rest of the summary."""
    try:
        return await build_food_recommendation(loc_id, user_id=user_id)
    except Exception as e:
        logging.error("Summary food recommendation error: %s", e)
        return None


async def _fetch_exam_countdown(session, today_date) -> str:
    from unibot.voice import _upcoming_exams
    try:
        return _format_exam_countdown(await _upcoming_exams(session), today_date)
    except Exception as e:
        logging.error("Summary exam countdown error: %s", e)
        return ""


async def _prepare_lunch_photo(food: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    """Photo of the lunch pick when one can be matched on the
    Studierendenwerk's public Speiseplan site, plus its caption."""
    if not (food and food.get("dish")):
        return None, None
    photo_url = None
    try:
        photo_url = await fetch_dish_photo_url(food["dish"], loc_id=food.get("loc_id") or "2")
    except Exception as e:
        logging.warning("Summary food photo fetch error: %s", e)
    place = f" ({_friendly_menu_category_label(food['category'])})" if food.get("category") else ""
    return photo_url, f"🍽️ <b>Today's lunch pick{html.escape(place)}:</b> {html.escape(food['dish'])}"


async def _prepare_outfits(bot, user_ids, weather_of: dict) -> dict:
    """Daily outfit suggestion with a virtual try-on image per user. Opt-in
    (toggle in /status) and skipped for users with an empty wardrobe or no
    reference photo. Every step is best-effort."""
    outfits: dict = {}
    for uid in user_ids:
        if not is_user_admin(uid) or not get_pref(uid, "outfit") or not get_ai_personal_enabled(uid):
            continue
        with acting_as(uid):
            if not db.private_context_is_current(uid):
                continue
            try:
                # A manual "Suggest Outfit Now" tap may have already run
                # today; deliver_morning_summary resends that one instead.
                if get_cached_outfit_today(uid):
                    continue
                wardrobe_entry = get_wardrobe_entry(uid)
                items = wardrobe_entry.get("items", [])
                ref_file_id = wardrobe_entry.get("reference_photo_file_id")
                if not items or not ref_file_id:
                    continue
                recent_ids = get_recently_worn_item_ids(uid)
                outfit = await pick_outfit_from_wardrobe(items, weather_of.get(uid), recent_ids, user_id=uid)
                if not db.private_context_is_current(uid) or not get_ai_personal_enabled(uid) or not outfit:
                    continue
                reference_bytes = await _download_telegram_file(bot, ref_file_id)
                if not db.private_context_is_current(uid) or not get_ai_personal_enabled(uid):
                    continue
                image_bytes = await generate_outfit_tryon_image(
                    bot, reference_bytes, outfit["chosen_items"], user_id=uid
                ) if reference_bytes else None
                if db.private_context_is_current(uid) and get_ai_personal_enabled(uid):
                    outfits[uid] = {"outfit": outfit, "image_bytes": image_bytes}
            except Exception as e:
                logging.warning("Outfit suggestion failed for %s: %s", uid, e)
    return outfits


async def _prepare_morning_voice(today_events, today_date, today_tasks, weather, food,
                                 name: Optional[str] = None,
                                 include_schedule: bool = True,
                                 user_id: Optional[int] = None) -> Optional[str]:
    """AI-narrated voice message, improvised fresh every morning (see
    build_ai_daily_plan_narration) and converted to OGG now, so delivery is
    just an upload. A TTS/ffmpeg hiccup just skips it."""
    try:
        if user_id is not None and not get_ai_personal_enabled(user_id):
            return None
        narration = await build_ai_daily_plan_narration(
            today_events if include_schedule else [], today_date, name=name,
            today_tasks=today_tasks, weather=weather, food=food,
            include_schedule=include_schedule, user_id=user_id,
        ) or build_daily_plan_speech_text(today_events, today_date, include_schedule=include_schedule)
        if user_id is not None and not get_ai_personal_enabled(user_id):
            return None
        voice_path = None
        result = await synthesize_speech(
            narration,
            can_send=lambda: user_id is None or personal_ai_allowed(user_id),
        )
        if user_id is not None and not get_ai_personal_enabled(user_id):
            return None
        if result:
            voice_path = await _pcm_to_ogg_voice(*result)
        if not voice_path:
            logging.warning("prepare_morning_summary: voice message unavailable (TTS or ffmpeg failed)")
        return voice_path
    except Exception as e:
        logging.error("Morning voice summary failed: %s", e)
        return None


async def _prepare_morning_summary(bot, user_ids) -> Optional[dict]:
    """Build everything the morning summary sends, without sending anything:
    the text (schedule, weather, tasks, AI lunch pick, exam countdown), the
    lunch photo URL, each user's outfit (AI pick + try-on image) and the
    AI-narrated voice message (already synthesized to an OGG file). The
    slow AI parts run here, ahead of time, so deliver_morning_summary can
    send everything at once at the scheduled time."""
    if not state.global_session:
        return None

    today_date = datetime.now(TZ_BERLIN).date()
    # Weather for the morning commute at each student's own place (omitted,
    # not an empty section, if Open-Meteo is unreachable); students at the
    # same place share one request.
    weather_of: dict[int, Optional[dict]] = {}
    # The lunch pick and its photo are per mensa: made once for each mensa
    # someone has lunch at today (one AI call each), shared by its students.
    # One pick per mensa and diet: students with the same filter share it.
    picks: dict[tuple, tuple] = {}

    async def lunch_at(loc_id: str, uid: Optional[int] = None) -> tuple:
        food_rules = food_filter(uid)
        key = (loc_id, food_rules["diet"], tuple(sorted(food_rules["avoid_codes"])),
               tuple(sorted(food_rules["avoid_keywords"])), food_rules["max_price"],
               bool(uid is not None and get_ai_personal_enabled(uid)))
        if key not in picks:
            food = await _fetch_lunch_pick(loc_id, user_id=uid)
            picks[key] = (food, *await _prepare_lunch_photo(food))
        return picks[key]

    # Per student: their own timetable, study windows, tasks, exams, lunch and voice.
    texts: dict[int, str] = {}
    voice_paths: dict[int, str] = {}
    menu_buttons: dict[int, str] = {}
    photos: dict[int, tuple] = {}
    for uid in user_ids:
        # one student's failure leaves only their own summary out, never the slot's
        try:
            with acting_as(uid):
                if not db.private_context_is_current(uid):
                    continue
                weather_of[uid] = await get_weather_snapshot(today_date, user_id=uid)
                if not db.private_context_is_current(uid):
                    continue
                texts[uid], voice, menu_buttons[uid], photos[uid] = await _prepare_user_summary(
                    uid, today_date, weather_of[uid], lunch_at)
                if not db.private_context_is_current(uid):
                    if voice:
                        try:
                            os.remove(voice)
                        except OSError:
                            logging.debug("Could not remove stale prepared voice", exc_info=True)
                    for part in (texts, menu_buttons, photos):
                        part.pop(uid, None)
                    continue
        except Exception:
            logging.error("Morning summary for %s could not be prepared", uid, exc_info=True)
            for part in (texts, menu_buttons, photos):
                part.pop(uid, None)
            continue
        if voice:
            voice_paths[uid] = voice

    return {
        "texts": texts,
        "photos": photos,
        "outfits": await _prepare_outfits(bot, user_ids, weather_of),
        "voice_paths": voice_paths,
        "menu_buttons": menu_buttons,
    }


def _menu_button_for(plan: dict, today_events: list) -> str:
    """The "Today's Menu" callback for the mensa of the student's campus
    today: the lunch break's, else the first class's; Uhlhornsweg by default."""
    lunch = plan.get("lunch")
    campus = lunch.get("campus") if lunch else None
    if campus is None:
        campus = next((campus_of(e.get("location")) for e in today_events if campus_of(e.get("location"))), None)
    if campus is None:
        campus = campus_anchor(current_user_id())
    return "mensa_loc|3|today|new" if campus == WECHLOY else "menu_nav|2/|new"


async def _prepare_user_summary(uid: int, today_date, weather, lunch_at) -> tuple[str, Optional[str], str, tuple]:
    """(summary text, voice file or None, menu button callback, (lunch photo
    url, caption)) for one student, built from their own session: never
    another student's classes, tasks or exams. `lunch_at(loc_id)` gives the
    (pick, photo url, caption) of the mensa on the student's campus."""
    # like the calendar reminders: no stored login, no timetable (a fresh
    # session must not sign in on anyone else's behalf)
    own = get_user_session(uid)
    session = own if has_stored_login(own) else None
    events, today_events, calendar_available = (
        await _fetch_today_events(session, today_date, user_id=uid)
        if session else ([], [], False)
    )
    today_tasks = _load_today_tasks(today_date, user_id=uid)
    plan = plan_study_day(events, today_date) if calendar_available else {"lunch": None}
    menu_button = _menu_button_for(plan, today_events)
    # the student picks what the summary contains (preferences.morning_sections)
    show = get_pref(uid, "morning_sections")
    food, photo_url, photo_caption = (await lunch_at("3" if menu_button.startswith("mensa_loc|3") else "2", uid)
                                      if show["mensa"] else (None, None, None))
    menu = await _lunch_menu_highlight(plan["lunch"], session) if show["mensa"] and calendar_available else None
    weather_shown = weather if show["weather"] else None
    text = _compose_morning_text(today_date, [
        (_format_schedule_section(today_events) if calendar_available else
         "📅 <b>Today's schedule couldn't be loaded.</b> Sign in or try again later.") if show["classes"] else "",
        (_format_study_windows_section(events, today_date, menu=menu) if calendar_available else
         "\n\n📚 <b>Study windows couldn't be calculated because the schedule is unavailable.</b>")
        if show["study"] else "",
        f"\n\n{build_weather_text_line(weather_shown)}" if weather_shown else "",
        _format_tasks_section(today_tasks),
        _format_lunch_section(food) if show["mensa"] else "",
        await _fetch_exam_countdown(session, today_date) if session and show["exams"] else "",
    ])
    # the spoken version is written and voiced by the AI service: only with the student's permission
    voice = await _prepare_morning_voice(today_events, today_date, today_tasks,
                                         weather_shown, food, name=get_user_name(uid),
                                         include_schedule=show["classes"], user_id=uid) \
        if get_ai_personal_enabled(uid) and calendar_available else None
    return text, voice, menu_button, (photo_url, photo_caption)


async def deliver_morning_summary(bot, user_ids, prepared: dict, on_delivered=None) -> list[int]:
    """Send a prepare_morning_summary result: text (pinned), lunch photo,
    outfit, voice message, back to back."""
    menu_buttons = prepared.get("menu_buttons") or {}

    # Pinning isn't automatic in Telegram - a pinned message stays pinned
    # forever until explicitly unpinned, so keeping only "today's" summary
    # pinned means unpinning yesterday's (tracked per-chat in general_cache,
    # since pin state is per-chat) before pinning the new one. Best-effort:
    # a pin/unpin failure (e.g. missing chat permission) never blocks the
    # summary itself from going out.
    delivered: list = []

    texts = prepared.get("texts") or {}
    for uid in user_ids:
        if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                or not _private_context_current(uid)):
            continue
        if uid not in texts:
            continue  # joined after the summary was prepared
        with acting_as(uid):
            if not db.private_context_is_current(uid):
                continue
            try:
                sent_msg = await bot.send_message(
                    chat_id=uid,
                    text=texts[uid],
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "🍴 Today's Menu", callback_data=menu_buttons.get(uid, "menu_nav|2/|new"))]])
                )
            except Exception as e:
                logging.error("Failed to send summary to %s: %s", uid, e)
                continue
            if not db.private_context_is_current(uid):
                continue
            delivered.append(uid)
            if on_delivered is not None:
                try:
                    on_delivered(uid)
                except Exception:
                    logging.error("Failed to record morning summary delivery for %s", uid, exc_info=True)

            prev_msg_id = personal_record(uid, "pinned_morning_summary")
            if not db.private_context_is_current(uid):
                continue
            if prev_msg_id:
                try:
                    await bot.unpin_chat_message(chat_id=uid, message_id=prev_msg_id)
                except Exception as e:
                    logging.warning("Failed to unpin previous morning summary for %s: %s", uid, e)
            try:
                await bot.pin_chat_message(chat_id=uid, message_id=sent_msg.message_id, disable_notification=True)
                save_personal_record(uid, "pinned_morning_summary", sent_msg.message_id)
            except Exception as e:
                logging.warning("Failed to pin morning summary for %s: %s", uid, e)

    for uid in delivered:
        if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                or not _private_context_current(uid)):
            continue
        photo_url, caption = (prepared.get("photos") or {}).get(uid) or (None, None)
        if not photo_url:
            continue
        with acting_as(uid):
            if not db.private_context_is_current(uid):
                continue
            try:
                await bot.send_photo(chat_id=uid, photo=photo_url, caption=caption, parse_mode="HTML")
            except Exception as e:
                logging.error("Failed to send lunch photo to %s: %s", uid, e)

    for uid in delivered:
        if (not is_user_allowed(uid) or lifecycle_state(uid) != ACTIVE
                or not _private_context_current(uid)):
            continue
        if not is_user_admin(uid) or not get_pref(uid, "outfit") or not get_ai_personal_enabled(uid):
            continue
        with acting_as(uid):
            try:
                if not db.private_context_is_current(uid):
                    continue
                # Checked again at delivery: a manual "Suggest Outfit Now"
                # between preparation and now wins, so the user never gets
                # two different outfits for the same day.
                cached = get_cached_outfit_today(uid)
                if cached:
                    caption = outfit_caption(cached)
                    if cached.get("photo_file_id"):
                        await bot.send_photo(chat_id=uid, photo=cached["photo_file_id"], caption=caption, parse_mode="HTML")
                    else:
                        await bot.send_message(chat_id=uid, text=caption, parse_mode="HTML")
                    if not db.private_context_is_current(uid):
                        continue
                    continue
                entry = prepared["outfits"].get(uid)
                if not entry:
                    continue
                outfit = entry["outfit"]
                caption = outfit_caption(outfit)
                photo_file_id = None
                if entry["image_bytes"]:
                    sent = await bot.send_photo(chat_id=uid, photo=io.BytesIO(entry["image_bytes"]), caption=caption, parse_mode="HTML")
                    if not db.private_context_is_current(uid):
                        continue
                    photo_file_id = sent.photo[-1].file_id if sent.photo else None
                else:
                    # Every image provider failed/unavailable - still
                    # deliver the text-only pick rather than nothing, since
                    # the weather-aware reasoning is useful on its own.
                    await bot.send_message(chat_id=uid, text=caption, parse_mode="HTML")
                    if not db.private_context_is_current(uid):
                        continue
                record_worn_items(uid, [it["id"] for it in outfit["chosen_items"]])
                save_cached_outfit_today(uid, outfit["description"], photo_file_id, outfit.get("color_tip", ""), [it["label"] for it in outfit["chosen_items"]])
            except Exception as e:
                logging.warning("Outfit suggestion failed for %s: %s", uid, e)

    for uid, voice_path in (prepared.get("voice_paths") or {}).items():
        try:
            if uid in delivered and is_user_allowed(uid) and lifecycle_state(uid) == ACTIVE:
                with acting_as(uid):
                    if not db.private_context_is_current(uid):
                        continue
                    voice_bytes = await aread_bytes(voice_path)
                    if (is_user_allowed(uid) and lifecycle_state(uid) == ACTIVE
                            and db.private_context_is_current(uid) and get_ai_personal_enabled(uid)):
                        await bot.send_voice(chat_id=uid, voice=voice_bytes)
        except Exception as e:
            logging.error("Morning voice: failed to send to %s: %s", uid, e)
        finally:
            try:
                os.remove(voice_path)
            except OSError:
                pass

    # course alerts held over the quiet hours come right after the summary
    if delivered:
        try:
            await course_registry.flush_alert_queues(bot, user_ids=delivered, with_morning=True)
        except Exception:
            logging.warning("Held course alerts after the morning summary failed", exc_info=True)
    return delivered


def morning_summary_sent(today_str: str, slot: str, user_ids=None) -> bool:
    # Delivery state is per student: a legacy process-wide marker only meant
    # that the old batch attempted the send, not that every student received it.
    if user_ids is not None:
        return all(
            (record := personal_record(uid, "morning_summary_sent") or {}).get("date") == today_str
            and slot in (record.get("slots") or [])
            for uid in user_ids
        )
    cache = load_general_cache()
    sent = cache.get("morning_summary_sent") or {}
    if sent.get("date") == today_str:
        if slot in (sent.get("slots") or []):
            return True
    # before per-student times there was one 07:00 summary, marked by date only
    return slot == "07:00" and cache.get("last_morning_summary") == today_str


def _mark_morning_summary_sent(today_str: str, slot: str, user_ids=None) -> None:
    if user_ids is not None:
        for uid in user_ids:
            previous = personal_record(uid, "morning_summary_sent") or {}
            slots = list(previous.get("slots") or []) if previous.get("date") == today_str else []
            if slot not in slots:
                slots.append(slot)
            save_personal_record(uid, "morning_summary_sent", {"date": today_str, "slots": slots})
        return
    # Re-load rather than reusing a cache loaded earlier - deliver_morning_summary
    # does its own separate load/save of general_cache.json (to track which
    # message is currently pinned), and saving a stale copy would silently
    # overwrite that, breaking the unpin-before-pin logic every day.
    cache = load_general_cache()
    sent = cache.get("morning_summary_sent") or {}
    slots = list(sent.get("slots") or []) if sent.get("date") == today_str else []
    if slot not in slots:
        slots.append(slot)
    cache["morning_summary_sent"] = {"date": today_str, "slots": slots}
    cache["last_morning_summary"] = today_str
    save_general_cache(cache)


async def _deliver_morning_summary_at(bot, user_ids, prepared: dict, send_at: datetime, today_str: str,
                                      slot: str = "07:00") -> None:
    """Background task: wait until send_at, then deliver."""
    try:
        delay = (send_at - datetime.now(TZ_BERLIN)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        logging.info("🌞 Delivering the %s morning summary...", slot)
        await deliver_morning_summary(
            bot, user_ids, prepared,
            on_delivered=lambda uid: _mark_morning_summary_sent(today_str, slot, [uid]),
        )
    except Exception as e:
        logging.error("❌ Morning summary delivery failed: %s", e)


async def handle_calendar_today_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_today_voice callback ("🔊 Listen" on the daily
    schedule) - synthesize today's plan via OpenRouter TTS and send it as a
    voice message."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=get_user_session(user_id), week_start=week_start, user_id=user_id)
        await send_daily_plan_voice(query.message, events, today, user_id=user_id)
    except Exception as e:
        logging.error("Calendar today voice error: %s", e)
        await query.message.reply_text(f"❌ Error generating voice schedule: {str(e)[:200]}")


async def handle_calendar_weekly_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_weekly_voice|YYYY-MM-DD callback ("🔊 Listen" on the
    weekly schedule) - synthesize that week's plan via OpenRouter TTS and
    send it as a voice message."""
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
        events = await get_calendar_events(session=get_user_session(user_id), week_start=week_start, user_id=user_id)
        await send_weekly_plan_voice(query.message, events, week_start, user_id=user_id)
    except Exception as e:
        logging.error("Calendar weekly voice error: %s", e)
        await query.message.reply_text(f"❌ Error generating voice schedule: {str(e)[:200]}")


def build_daily_plan_speech_text(events: list, target_date, include_schedule: bool = True) -> str:
    """Build a plain-English (no Markdown/HTML) description of one day's
    schedule, suitable as TTS input - reuses the same event dicts
    send_daily_calendar renders (title, time, location, status)."""
    if not include_schedule:
        return f"Here's your personal briefing for {target_date:%A, %B %d}."
    day_events = [ev for ev in events if ev["date_key"] == target_date]
    today_events = [ev for ev in day_events if not ev.get("is_cancelled")]
    cancelled = _cancelled_sentence(day_events)
    if not today_events:
        return f"You have no events scheduled on {target_date:%A, %B %d}. Enjoy your free day!{cancelled}"

    parts = [f"Here is your schedule for {target_date:%A, %B %d}. You have {len(today_events)} event{'s' if len(today_events) != 1 else ''} today."]
    for ev in today_events:
        title = clean_course_title(ev.get("title", "")) or "an event"
        time_range = ev.get("time", "")
        location = _safe_loc(ev.get("location", ""))
        sentence = f"{title} at {time_range}"
        if location and location not in ("-", "TBA", "N/A"):
            sentence += f", in {location}"
        parts.append(sentence + ".")
    return " ".join(parts) + cancelled


def _cancelled_sentence(events: list) -> str:
    """" Cancelled: X at 10:00." for the cancelled classes, or ""."""
    gone = [f"{clean_course_title(ev.get('title', '')) or 'an event'} at {ev.get('time', '')}"
            for ev in events if ev.get("is_cancelled")]
    return f" Cancelled: {', '.join(gone)}." if gone else ""


DAILY_PLAN_NARRATION_SYSTEM_PROMPT = """You are recording a short, warm, upbeat spoken morning briefing for a university student - it will be converted to speech and sent as a voice message, so write it exactly as you'd say it out loud, not as a written list.

Structure:
1. Open with a genuine, encouraging line about the day ahead - invent fresh wording every time, never reuse a stock phrase, make it feel spontaneous rather than a template.
2. If weather data is given, mention what it'll actually be like stepping outside (dry/rainy/sunny etc - the precise temperature matters far less than that) and suggest what to bring or wear, naturally, as part of the flow - not a separate weather-report segment.
3. If a schedule is provided, naturally weave in what's on, roughly when, and where - like a friend casually telling them their day, not reading a bureaucratic agenda. If the prompt says the schedule is hidden, do not mention classes or imply there are none.
4. If any tasks are due today, mention them too, woven into the same conversational flow - not as a separate announcement.
5. If a lunch pick is given, mention it naturally too - what it is and roughly where to get it - as part of the same flow, not a separate food-review segment.
6. Close with one short, warm line wishing them well.

Rules: plain spoken sentences only - no markdown, no emojis, no bullet points, no headers. Keep it under 130 words (a bit more if there's weather, tasks, and/or a lunch pick to mention too). Vary your phrasing and structure every time you're asked this, even for an identical schedule, since this repeats every single morning and must never sound canned or repetitive."""


async def build_ai_daily_plan_narration(today_events: list, target_date, name: Optional[str] = None,
                                       today_tasks: Optional[list] = None, weather: Optional[dict] = None,
                                       food: Optional[dict] = None, include_schedule: bool = True,
                                       user_id: Optional[int] = None) -> Optional[str]:
    """Ask the chat LLM (OpenRouter) to write a warm, freshly-improvised
    spoken narration of the day's schedule, weather, any tasks due today,
    and a lunch pick - deliberately not a fixed template, so the opening
    line and phrasing differ every time this is called, even for an
    identical schedule. If `name` is given (see get_user_name/
    get_primary_user_name), the narration addresses them by it. `weather`
    is a get_weather_snapshot() dict - what matters for the narration is
    the condition (dry/rainy/etc), not the exact temperature. `food` is a
    build_food_recommendation() result. Returns None on any failure;
    callers should fall back to build_daily_plan_speech_text so the voice
    message always has content."""
    held = [ev for ev in today_events if not ev.get("is_cancelled")]
    user_prompt = f"Today is {target_date:%A, %B %d}."
    if include_schedule:
        if held:
            schedule_summary = "\n".join(
                f"- {clean_course_title(ev.get('title', '')) or 'an event'} at {ev.get('time', '')} in {_safe_loc(ev.get('location', ''))}"
                for ev in held
            )
        else:
            schedule_summary = "No events scheduled - a free day."
        if len(held) < len(today_events):
            schedule_summary += f"\nCancelled today (mention briefly, they don't take place):{_cancelled_sentence(today_events)}"
        user_prompt += f"\nSchedule:\n{schedule_summary}"
    else:
        user_prompt += "\nThe student chose to hide classes. Do not mention their schedule or imply that they have no events."

    if weather:
        wet_note = "expect it to be wet (rain/drizzle/snow)" if weather["is_wet"] else "it should stay dry"
        user_prompt += (
            f"\n\nWeather in {weather['location_name']} around when they'll be heading out (8-11 AM): {weather['description']}, "
            f"{wet_note}, roughly {weather['temp_min']:.0f}-{weather['temp_max']:.0f}°C. "
            f"Mention the location name ({weather['location_name']}) naturally along with what to expect, and suggest what to bring/wear."
        )

    if today_tasks:
        tasks_summary = "\n".join(
            f"- {t['text']}" + (f" (due {due:%H:%M})" if (due := task_due(t)) else "")
            for t in today_tasks
        )
        user_prompt += f"\n\nTasks due today:\n{tasks_summary}\nMention these naturally too, not as a separate rigid list."

    if food and food.get("dish") and food.get("category"):
        user_prompt += (
            f"\n\nLunch pick for today: {food['dish']} at {_friendly_menu_category_label(food['category'])}. "
            "Mention it naturally too, briefly."
        )

    if name:
        user_prompt += f"\nAddress the listener by name at least once, naturally (their name is {name})."
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return None
    narration = await _call_openrouter(
        DAILY_PLAN_NARRATION_SYSTEM_PROMPT, user_prompt, temperature=1.15,
        can_send=lambda: user_id is None or personal_ai_allowed(user_id),
    )
    return narration.strip() if narration else None


def build_weekly_plan_speech_text(events: list, week_start, week_end) -> str:
    """Build a plain-English (no Markdown/HTML) description of one week's
    schedule, suitable as TTS input - the deterministic fallback for
    build_ai_weekly_plan_narration, same role as build_daily_plan_speech_text
    for the daily version."""
    cancelled = _cancelled_sentence(events)
    events = [ev for ev in events if not ev.get("is_cancelled")]
    if not events:
        return (f"You have no events scheduled for the week of {week_start:%B %d} to {week_end:%B %d}. "
                f"Enjoy the free week!{cancelled}")

    events_by_day: dict = {}
    for ev in events:
        events_by_day.setdefault(ev["date_key"], []).append(ev)

    parts = [f"Here is your schedule for the week of {week_start:%B %d} to {week_end:%B %d}."]
    for dk in sorted(events_by_day.keys()):
        day_name = dk.strftime("%A")
        titles = ", ".join(clean_course_title(ev.get("title", "")) or "an event" for ev in events_by_day[dk])
        parts.append(f"On {day_name}, you have {titles}.")
    return " ".join(parts) + cancelled


WEEKLY_PLAN_NARRATION_SYSTEM_PROMPT = """You are recording a short, warm, upbeat spoken weekly briefing for a university student - it will be converted to speech and sent as a voice message, so write it exactly as you'd say it out loud, not as a written list.

Structure:
1. Open with a genuine, encouraging line about the week ahead - invent fresh wording every time, never reuse a stock phrase, make it feel spontaneous rather than a template.
2. Naturally walk through the week day by day (or group similar/light days together): what's on, roughly when - like a friend giving them the lay of the week, not reading a bureaucratic agenda. Call out the busiest day(s) and any noticeably free day(s) if that's genuinely useful.
3. If any tasks are due during the week, mention them too, woven into the same conversational flow - not as a separate announcement.
4. If any good study windows (free gaps squeezed between two classes) are listed, mention the best one or two naturally, e.g. as a nudge for revision or homework - not a rigid list of every single one.
5. Close with one short, warm line wishing them a good week.

Rules: plain spoken sentences only - no markdown, no emojis, no bullet points, no headers. Keep it under 160 words. Vary your phrasing and structure every time you're asked this, even for an identical week, since this must never sound canned or repetitive."""


async def build_ai_weekly_plan_narration(events: list, week_start, week_end, name: Optional[str] = None,
                                         week_tasks: Optional[list] = None,
                                         user_id: Optional[int] = None) -> Optional[str]:
    """Ask the chat LLM (OpenRouter) to write a warm, freshly-improvised
    spoken narration of the week's schedule and any tasks due that week -
    the weekly counterpart of build_ai_daily_plan_narration. Returns None
    on any failure; callers should fall back to build_weekly_plan_speech_text
    so the voice message always has content."""
    cancelled = _cancelled_sentence(events)
    events = [ev for ev in events if not ev.get("is_cancelled")]
    if events:
        events_by_day: dict = {}
        for ev in events:
            events_by_day.setdefault(ev["date_key"], []).append(ev)
        schedule_lines = []
        for dk in sorted(events_by_day.keys()):
            day_events = ", ".join(
                f"{clean_course_title(ev.get('title', '')) or 'an event'} at {ev.get('time', '')}"
                for ev in events_by_day[dk]
            )
            schedule_lines.append(f"- {dk.strftime('%A')}: {day_events}")
        schedule_summary = "\n".join(schedule_lines)
    else:
        schedule_summary = "No events scheduled - a free week."

    user_prompt = f"Week of {week_start:%B %d} to {week_end:%B %d}.\nSchedule:\n{schedule_summary}"
    if cancelled:
        user_prompt += f"\nCancelled this week (mention briefly, they don't take place):{cancelled}"

    if week_tasks:
        tasks_summary = "\n".join(
            f"- {t['text']}" + (f" (due {due:%A %H:%M})" if (due := task_due(t)) else "")
            for t in week_tasks
        )
        user_prompt += f"\n\nTasks due this week:\n{tasks_summary}\nMention these naturally too, not as a separate rigid list."

    study_gaps = find_weekly_study_gaps(events)
    if study_gaps:
        gaps_summary = "\n".join(
            f"- {g['start_dt']:%A} {g['start_dt']:%H:%M}-{g['end_dt']:%H:%M} ({g['duration_minutes'] // 60}h{g['duration_minutes'] % 60:02d}m), between {g['after']} and {g['before']}"
            for g in study_gaps
        )
        user_prompt += f"\n\nFree windows between classes this week, good for studying:\n{gaps_summary}\nMention the best one or two naturally as a nudge for revision/homework, not all of them, and not as a rigid list."

    if name:
        user_prompt += f"\nAddress the listener by name at least once, naturally (their name is {name})."
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return None
    narration = await _call_openrouter(
        WEEKLY_PLAN_NARRATION_SYSTEM_PROMPT, user_prompt, temperature=1.15,
        can_send=lambda: user_id is None or personal_ai_allowed(user_id),
    )
    return narration.strip() if narration else None


async def _send_voice_to_users(bot, user_ids, text: str):
    """Synthesize `text` once via OpenRouter TTS and send the resulting
    voice message to every user_id, reusing the same converted file rather
    than re-synthesizing per recipient. Used for the automatic morning
    voice summary, which has no single "sender" to reply to and no text
    fallback of its own to send - the text morning summary already went
    out separately, so a TTS/ffmpeg failure here just logs and skips."""
    result = await synthesize_speech(text)
    if not result:
        logging.warning("_send_voice_to_users: TTS unavailable, skipping voice message")
        return
    pcm_bytes, sample_rate = result

    ogg_path = await _pcm_to_ogg_voice(pcm_bytes, sample_rate)
    if not ogg_path:
        logging.warning("_send_voice_to_users: ffmpeg conversion failed, skipping voice message")
        return
    try:
        voice_bytes = await aread_bytes(ogg_path)
        for uid in user_ids:
            try:
                await bot.send_voice(chat_id=uid, voice=voice_bytes)
            except Exception as e:
                logging.error("_send_voice_to_users: failed to send to %s: %s", uid, e)
    finally:
        try:
            os.remove(ogg_path)
        except OSError:
            pass


async def _try_send_voice(sender, text: str, user_id=None) -> bool:
    """Best-effort: synthesize `text` via OpenRouter TTS, convert to
    OGG/Opus, and send it as a voice message on `sender`. Returns True on
    success, False on any failure (TTS unavailable, ffmpeg missing/failed,
    or the send itself raising) - callers decide what, if anything, to do
    about a False result (e.g. fall back to sending the text)."""
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return False
    result = await synthesize_speech(
        text,
        can_send=lambda: user_id is None or personal_ai_allowed(user_id),
    )
    if not result:
        return False
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return False
    pcm_bytes, sample_rate = result

    ogg_path = await _pcm_to_ogg_voice(pcm_bytes, sample_rate)
    if not ogg_path:
        return False

    try:
        if user_id is not None and not get_ai_personal_enabled(user_id):
            return False
        await sender.reply_voice(voice=await aread_bytes(ogg_path))
        return True
    except Exception as e:
        logging.error("_try_send_voice: failed to send voice message: %s", e)
        return False
    finally:
        try:
            os.remove(ogg_path)
        except OSError:
            pass


async def send_daily_plan_voice(sender, events: list, target_date, user_id=None):
    """Build today's schedule as a warm, freshly-improvised spoken
    narration (falling back to a plain deterministic description if the
    LLM call fails), synthesize it via OpenRouter TTS, and send it as a
    Telegram voice message. Falls back to sending the text itself (never
    silently does nothing) if TTS is unavailable, ffmpeg is missing, or
    synthesis fails for any reason.

    `user_id` personalizes the narration with that user's name (see
    get_user_name); without a known name the narration uses none."""
    today_events = [ev for ev in events if ev["date_key"] == target_date]
    owner = user_id if user_id is not None else current_user_id()
    if not get_ai_personal_enabled(owner):
        # nothing leaves the server: the plain schedule, built here, as text
        await sender.reply_text(f"{build_daily_plan_speech_text(events, target_date)}\n\n{AI_PERSONAL_OFF_NOTE}")
        return
    name = get_user_name(owner)
    try:
        today_tasks = tasks_due_between(user_tasks(owner), target_date, target_date) if owner is not None else []
    except Exception as e:
        logging.warning("send_daily_plan_voice: tasks fetch failed: %s", e)
        today_tasks = []
    weather = await get_weather_snapshot(target_date, user_id=owner)
    text = await build_ai_daily_plan_narration(
        today_events, target_date, name=name, today_tasks=today_tasks, weather=weather, user_id=owner
    ) or build_daily_plan_speech_text(events, target_date)
    await _show_typing(sender, action=ChatAction.RECORD_VOICE)

    if not await _try_send_voice(sender, text, owner):
        await sender.reply_text("⚠️ Couldn't generate voice right now - here's the schedule as text instead:\n\n" + text)


async def send_weekly_plan_voice(sender, events: list, week_start, user_id=None):
    """Same idea as send_daily_plan_voice, but for the whole week - a warm,
    freshly-improvised narration of the week's schedule and any tasks due
    that week, synthesized and sent as a voice message. Falls back to a
    plain deterministic description, and further to plain text, on any
    failure."""
    week_end = week_start + timedelta(days=6)
    owner = user_id if user_id is not None else current_user_id()
    if not get_ai_personal_enabled(owner):
        await sender.reply_text(f"{build_weekly_plan_speech_text(events, week_start, week_end)}\n\n{AI_PERSONAL_OFF_NOTE}")
        return
    name = get_user_name(owner)
    week_tasks = _week_tasks_for_range(week_start, week_end, user_id=owner)
    text = await build_ai_weekly_plan_narration(
        events, week_start, week_end, name=name, week_tasks=week_tasks, user_id=owner
    ) or build_weekly_plan_speech_text(events, week_start, week_end)
    await _show_typing(sender, action=ChatAction.RECORD_VOICE)

    if not await _try_send_voice(sender, text, owner):
        await sender.reply_text("⚠️ Couldn't generate voice right now - here's the week as text instead:\n\n" + text)


async def send_text_and_voice(sender, text: str, user_id=None):
    """Send `text` as a normal message, then also synthesize and send it as
    a voice message via the same free OpenRouter TTS pipeline as 🔊 Listen.
    Best-effort for the voice half - a TTS/ffmpeg failure just means no
    voice note follows, since the text has already gone out and callers
    shouldn't have to handle a second failure mode on top of their own."""
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return
    await sender.reply_text(text)
    if user_id is not None and not get_ai_personal_enabled(user_id):
        return
    await _show_typing(sender, action=ChatAction.RECORD_VOICE)
    await _try_send_voice(sender, text, user_id)
