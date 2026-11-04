"""Voice notes and photos: transcription, intent routing and Q&A over the user's data.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import difflib
import html
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from exam_reminder import (
    compute_transcript_summary,
    get_all_exam_dates,
    get_grades,
    get_open_exam_registrations,
    get_registered_exam_schedule,
    get_registered_exams,
)
from fast_enroll import get_open_courses
from studip_utils import TZ_BERLIN, berlin_now

from unibot import state
from unibot.storage import (
    add_wardrobe_item,
    cleanup_link_cache,
    get_primary_user_name,
    get_user_name,
    link_cache,
    load_default_semester,
    load_tasks,
    save_tasks,
    set_wardrobe_reference_photo,
)
from unibot.formatting import _short_id, clean_course_title
from unibot.telegram_ui import _download_telegram_file, get_main_keyboard, nav_names
from unibot.auth import is_user_allowed, login_studip
from unibot.studip_api import BASE_URL, _normalize_url, list_courses, list_files
from unibot.ai import _call_openrouter, _extract_json_object
from unibot.weather import get_weather_snapshot
from unibot.mensa import (
    _deliver_food_recommendation,
    build_food_recommendation,
    get_menu_navigation_keyboard,
    get_todays_menu_enhanced,
    load_food_preferences,
)
from unibot.wardrobe import describe_wardrobe_item, tag_wardrobe_item
from unibot.schedule import get_calendar_events
from unibot.monitoring import _run_check_now
from unibot.exams import exam_action_cache, send_exam_registration_menu
from unibot.study_plan import (
    _render_enrolled_courses_for_semester,
    _render_open_courses_for_semester,
    search_course_material,
)
from unibot.tasks import extract_exam_reminder_task
from unibot.enrollment import send_fastenroll_list
from unibot.morning import send_daily_plan_voice, send_text_and_voice, send_weekly_plan_voice


nav_stack = {}  # user_id -> [url1, url2, ...]


# ── Voice intent routing (free-tier OpenRouter LLM) ─────────────────────────
# A fresh voice note (no wizard step pending) is classified into one of
# VOICE_INTENTS instead of always being treated as a new task. Any failure
# anywhere in this pipeline (missing API key, network error, bad JSON) must
# resolve to {"intent": "unclear", ...} so the feature degrades to exactly
# today's task-creation behavior rather than breaking the voice flow.

VOICE_INTENTS = [
    "create_task", "create_exam_reminder_task", "course_enroll", "course_deenroll",
    "exam_register", "exam_deregister", "check_grades", "check_exam_dates",
    "check_status", "show_menu",
    "show_last_file", "list_course_files", "exam_dates_all", "upcoming_dashboard",
    "calendar_today", "calendar_weekly", "set_default_semester", "run_check",
    "fastenroll_list", "read_daily_plan", "read_weekly_plan", "ask_question", "recommend_meal", "unclear",
]

# Friendly button labels for every real (non-"unclear") intent, used both by
# the "did you mean...?" clarification buttons (see _route_voice_intent) and
# nowhere else — kept as a single source of truth so a new intent only needs
# one label added here to work in both places.
INTENT_LABELS = {
    "create_task": "📝 Create a task",
    "create_exam_reminder_task": "⏰ Set an exam reminder",
    "course_enroll": "🎓 Enroll in a course",
    "course_deenroll": "🚪 Sign out of a course",
    "exam_register": "📝 Register for an exam",
    "exam_deregister": "🗑️ Deregister from an exam",
    "check_grades": "📜 Show my grades",
    "check_exam_dates": "📚 Show my upcoming exam dates",
    "check_status": "ℹ️ Show bot status",
    "show_menu": "🍽️ Show today's menu",
    "recommend_meal": "🍽️ Recommend something to eat",
    "show_last_file": "📄 Show a course's latest file",
    "list_course_files": "📁 List a course's files",
    "exam_dates_all": "📋 Show all exam dates",
    "upcoming_dashboard": "🔔 Show what's coming up",
    "calendar_today": "📅 Show today's schedule",
    "calendar_weekly": "📆 Show this week's schedule",
    "set_default_semester": "📆 Set default semester",
    "run_check": "🔁 Run a manual sync",
    "fastenroll_list": "⚡ List Fast Enroll jobs",
    "read_daily_plan": "🔊 Read today's plan aloud",
    "read_weekly_plan": "🔊 Read this week's plan aloud",
    "ask_question": "💬 Ask a question about my studies",
}

CLASSIFY_INTENT_SYSTEM_PROMPT = """You classify a spoken command (transcribed from Turkish or English) sent to a university assistant Telegram bot into exactly one intent.

Intents, each with example phrasings in both languages:
- create_task: "yeni görev ekle, yarın rapor teslim et" / "add a task, submit the report tomorrow" — a plain to-do, not tied to a specific exam's date
- create_exam_reminder_task: "Computational Intelligence sınavından 3 gün önce hatırlat" / "remind me about the Computational Intelligence exam 3 days before" / "sınav günü hatırlat" — specifically asks to be reminded relative to a named exam's date (N days/hours before it, or on the day)
- course_enroll: "Computational Economics dersine kayıt olmak istiyorum" / "sign me up for Computational Economics"
- course_deenroll: "Lineer Cebir dersinden kaydımı sil" / "unenroll me from Linear Algebra"
- exam_register: "Makro İktisat sınavına kayıt ol" / "register me for the Macroeconomics exam"
- exam_deregister: "sınav kaydımı iptal et" / "deregister me from my exam"
- check_grades: "notlarımı göster" / "show me my grades" / "transkriptimi göster"
- check_exam_dates: "yaklaşan sınavlarım neler" / "what are my upcoming exams" — only MY registered exams
- check_status: "bot durumunu göster" / "show bot status"
- show_menu: "bugünün yemek menüsünü göster" / "what's on the menu today" / "yemekhanede ne var" — wants to see the full menu
- recommend_meal: "bugün ne yesem" / "what should I eat today" / "recommend me something for lunch" / "yemekhaneden ne önerirsin" — wants ONE dish picked for them, not the full menu
- show_last_file: "Computational Intelligence dersinin son dosyasını göster" / "list the last file of Computational Intelligence" / "show me the latest file for Linear Algebra" — asks for only the single most recently uploaded file of a named course
- list_course_files: "Linear Algebra dersinin dosyalarını listele" / "list the files of Linear Algebra" / "show me all files for Computational Economics" — asks to see every file of a named course, not just the latest one
- exam_dates_all: "tüm sınav tarihlerini göster" / "show every exam date" / "list all exams in the curriculum" — every exam across the whole curriculum, not just mine
- upcoming_dashboard: "önümüzdeki günlerde neler var" / "what's coming up" / "show my upcoming schedule" — a combined view of tasks, exam dates and deadlines
- calendar_today: "bugünkü derslerim neler" / "what's my schedule today" / "show today's lectures" — wants to SEE today's schedule as text
- read_daily_plan: "günlük planımı sesli oku" / "read me today's schedule" / "read my daily plan out loud" / "tell me what I have today" — specifically wants today's schedule spoken aloud as a voice message, not shown as text
- read_weekly_plan: "haftalık planımı sesli oku" / "read me this week's schedule" / "read my week out loud" — same, but for the whole week
- ask_question: "toplam kaç kredim var" / "what's my GPA" / "when is my next exam" / "do I have any free time this week" / "kaç görevim kaldı" / "what should I wear today" / "is it going to rain" / "bugün şemsiye alsam mı" / "do I need a jacket today" — an open-ended question that needs combining, reasoning, or judgment over their data (including weather), not a direct "show me X" request matching one of the other intents above. This is its own clear intent, not something to hedge into "unclear" — weather/clothing questions ALWAYS belong here even though the word "today" appears in them (that alone doesn't make it calendar_today, which is specifically for wanting to see the schedule as text)
- calendar_weekly: "bu haftaki ders programımı göster" / "show this week's schedule" / "what's my week plan"
- set_default_semester: "varsayılan dönemi ayarla" / "set my default semester" / "change my default semester" — opens the semester picker, doesn't require a semester to be named
- run_check: "her şeyi manuel kontrol et" / "run a manual sync" / "check for updates now" — force-refreshes messages/announcements/files/forum right now
- fastenroll_list: "planlanmış fast enroll işlerimi listele" / "list my scheduled fast enroll jobs" / "show my pending enrollments"
- unclear: anything that doesn't clearly match one of the above

For course_enroll, course_deenroll, exam_register, exam_deregister, create_exam_reminder_task, show_last_file, and list_course_files, also extract the course or exam name mentioned (in whatever language it was said) as "query"; otherwise "query" is null.

If — and only if — you pick "unclear", also set "alternatives" to an array of the 1-2 intents (from the list above, excluding "unclear") that are the next most plausible reading of the phrase; use an empty array if nothing else is remotely plausible. For every other intent, "alternatives" is an empty array.

Respond with strict JSON only, no other text: {"intent": "<intent>", "query": "<name or null>", "alternatives": ["<intent>", ...]}"""


async def build_qa_context(user_id=None) -> str:
    """Gather a compact plain-text snapshot of the student's current
    academic data — this week's schedule, registered upcoming exams,
    transcript summary, personal tasks — for the free-form ask_question
    intent to answer against. Each source is independently best-effort
    (fetched inside its own try/except): one failing (e.g. a slow StuMS
    page) just leaves that section out of the context rather than blocking
    the others or the whole answer."""
    parts = []

    session = None
    try:
        session = await login_studip()
    except Exception as e:
        logging.warning("build_qa_context: login failed: %s", e)

    try:
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=state.global_session, week_start=week_start)
        if events:
            lines = [
                f"- {ev.get('date_key')}: {clean_course_title(ev.get('title', '')) or 'event'} at {ev.get('time', '')}"
                for ev in sorted(events, key=lambda e: (e.get("date_key") or today, e.get("time") or ""))
            ]
            parts.append("This week's schedule:\n" + "\n".join(lines))
    except Exception as e:
        logging.warning("build_qa_context: calendar failed: %s", e)

    try:
        weather = await get_weather_snapshot(today)
        if weather:
            wet_note = "wet (rain/drizzle/snow expected)" if weather["is_wet"] else "dry"
            parts.append(
                f"Weather in {weather['location_name']} today, 8-11 AM (leaving-home window): "
                f"{weather['description']}, {wet_note}, {weather['temp_min']:.0f}-{weather['temp_max']:.0f}°C, "
                f"{weather['precip_chance']}% chance of precipitation."
            )
    except Exception as e:
        logging.warning("build_qa_context: weather failed: %s", e)

    if session:
        try:
            exams = await get_registered_exam_schedule(session)
            if exams:
                lines = [f"- {e['title']}: {e.get('date_line') or e.get('date')}" for e in exams]
                parts.append("Registered upcoming exams:\n" + "\n".join(lines))
        except Exception as e:
            logging.warning("build_qa_context: registered exams failed: %s", e)

        try:
            grades = await get_grades(session)
            summary = compute_transcript_summary(grades)
            gpa_text = summary["gpa"] if summary["gpa"] is not None else "not yet available"
            parts.append(
                f"Transcript: weighted average grade {gpa_text}, total credits earned {summary['total_credits']}, "
                f"{len(summary['passed'])} module(s) passed, {len(summary['failed'])} module(s) failed."
            )
        except Exception as e:
            logging.warning("build_qa_context: transcript failed: %s", e)

    try:
        tasks = load_tasks()
        if user_id is not None:
            tasks = [t for t in tasks if t.get("user_id") == user_id]
        if tasks:
            lines = [f"- {t['text']}" + (f" (due {t['due_time']})" if t.get("due_time") else "") for t in tasks]
            parts.append("Personal tasks:\n" + "\n".join(lines))
    except Exception as e:
        logging.warning("build_qa_context: tasks failed: %s", e)

    return "\n\n".join(parts) if parts else "No data could be loaded right now."


QA_SYSTEM_PROMPT = """You are a helpful university assistant answering a spoken question from a student, using ONLY the data provided below the question: never invent facts that aren't in it. Answer conversationally and concisely (2-4 sentences), in plain spoken sentences suitable both for reading as a text message and for being read aloud as a voice message: no markdown, no bullet points, no emojis, no headers. If the data doesn't contain enough to answer, say so honestly rather than guessing. If weather data is given and the question is about the weather or what to wear/bring, answer using the condition (dry/rainy/etc, that matters more than the exact temperature) with a practical suggestion. Course material excerpts, when given, are passages from the student's own course files picked by similarity to the question: use them for questions about lecture content, assignments or course organisation, mention which course or file the answer comes from, and ignore excerpts that don't actually answer the question."""


async def answer_question_with_context(question: str, context_data: str, name: Optional[str] = None) -> Optional[str]:
    """Ask the chat LLM (OpenRouter) to answer a free-form spoken question
    using the snapshot from build_qa_context. Returns None on any failure —
    callers should tell the user to try again rather than send a blank or
    made-up answer."""
    system_prompt = QA_SYSTEM_PROMPT
    if name:
        system_prompt += f" The student's name is {name} — you may address them by it naturally, but don't force it into every sentence."
    user_prompt = f"Data:\n{context_data}\n\nQuestion: {question}"
    answer = await _call_openrouter(system_prompt, user_prompt, temperature=0.7)
    return answer.strip() if answer else None


async def classify_voice_intent(transcript: str) -> dict:
    """Classify a fresh (no wizard pending) voice transcript into one of
    VOICE_INTENTS. Always returns {"intent", "query", "alternatives"};
    falls back to {"intent": "unclear", "query": None, "alternatives": []}
    on any failure. "alternatives" is only ever non-empty when "intent" is
    "unclear" — see _route_voice_intent for how those become "did you
    mean...?" buttons instead of silently defaulting to task creation.

    Retries once on a malformed/unparseable response before giving up —
    the free-tier model occasionally returns something _extract_json_object
    can't parse (truncated, prose-wrapped, etc), and a single retry clears
    most of those without meaningfully hurting latency."""
    for attempt in range(2):
        raw = await _call_openrouter(CLASSIFY_INTENT_SYSTEM_PROMPT, transcript)
        parsed = _extract_json_object(raw)
        if parsed and parsed.get("intent") in VOICE_INTENTS:
            alternatives = [a for a in (parsed.get("alternatives") or []) if a in INTENT_LABELS]
            return {"intent": parsed["intent"], "query": parsed.get("query"), "alternatives": alternatives[:2]}
        if attempt == 0:
            logging.warning("classify_voice_intent: unparseable response, retrying: %r", raw)
    return {"intent": "unclear", "query": None, "alternatives": []}


async def _upcoming_exams(session):
    """Return [(title, exam_datetime), ...] for every still-upcoming exam
    among the student's own registered exams (get_registered_exam_schedule
    — deliberately not the full curriculum's exam dates, since a countdown
    should reflect what the student is actually sitting), sorted
    soonest-first. [] if there are none / the fetch failed. Used for the
    morning summary's "⏳ N days until <exam>" countdown lines (one per
    upcoming exam, not just the nearest)."""
    try:
        registered = await get_registered_exam_schedule(session)
    except Exception as e:
        logging.warning("_upcoming_exams: fetch failed: %s", e)
        return []

    now = berlin_now()
    upcoming = []
    for e in registered:
        if not e.get("date"):
            continue
        if e.get("start_datetime"):
            dt = datetime.fromisoformat(e["start_datetime"])
        else:
            dt = datetime.fromisoformat(e["date"]).replace(hour=9, minute=0)
        if dt >= now:
            upcoming.append((e["title"], dt))

    upcoming.sort(key=lambda x: x[1])
    return upcoming


async def _find_exam_datetime(session, query_text: Optional[str]):
    """Find the best-matching exam for a spoken course/exam name and return
    (title, exam_datetime, has_exact_time), or (None, None, False) if nothing
    scores confidently enough. Pools candidates from both the student's own
    registered exams (get_registered_exam_schedule, which can carry an exact
    start time) and the full curriculum's exam dates (get_all_exam_dates,
    date only — defaults to 09:00 local).

    The same title can appear more than once across semesters (a retake, or
    a past sitting still listed), so among near-equally good text matches a
    today-or-later one is preferred — silently picking a past sitting would
    create a reminder that's already overdue. But recency never overrides a
    clearly better text match: a much stronger match wins even if its date
    has already passed, since substituting a different, worse-matching
    course by date alone would be actively misleading.
    """
    if not query_text:
        return None, None, False

    try:
        registered = await get_registered_exam_schedule(session)
    except Exception as e:
        logging.warning("_find_exam_datetime: registered schedule fetch failed: %s", e)
        registered = []
    try:
        all_exams = await get_all_exam_dates(session)
    except Exception as e:
        logging.warning("_find_exam_datetime: all exam dates fetch failed: %s", e)
        all_exams = []

    pool = []  # (title, datetime, has_exact_time)
    for e in registered:
        if not e.get("date"):
            continue
        if e.get("start_datetime"):
            pool.append((e["title"], datetime.fromisoformat(e["start_datetime"]), True))
        else:
            pool.append((e["title"], datetime.fromisoformat(e["date"]).replace(hour=9, minute=0), False))
    for e in all_exams:
        if e.get("exam_date"):
            pool.append((e["title"], datetime.fromisoformat(e["exam_date"]).replace(hour=9, minute=0), False))

    if not pool:
        return None, None, False

    query_lower = query_text.strip().lower()
    scored = []
    for title, dt, has_time in pool:
        title_lower = (title or "").lower()
        if not title_lower:
            continue
        score = difflib.SequenceMatcher(None, query_lower, title_lower).ratio()
        if query_lower in title_lower or title_lower in query_lower:
            score = max(score, 0.85)
        scored.append((score, title, dt, has_time))

    if not scored:
        return None, None, False
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_title, best_dt, best_has_time = scored[0]
    if best_score < 0.45:
        return None, None, False

    today_start = berlin_now().replace(hour=0, minute=0, second=0, microsecond=0)
    if best_dt < today_start:
        for score, title, dt, has_time in scored[1:]:
            if dt >= today_start and score >= best_score - 0.15:
                return title, dt, has_time

    return best_title, best_dt, best_has_time


async def _match_course(course_query: Optional[str]):
    """Fuzzy-match a spoken course name to one of the student's actual
    Stud.IP courses (list_courses()). Returns (course_name, cid), or
    (None, None) if nothing matches confidently.

    Uses a stricter bar than the enroll/exam flows: those always show the
    matched title on a button before anything happens, so a shaky match is
    easy to notice and just not tap. Callers of this (file-listing voice
    commands) hand over real files/downloads directly with no such
    checkpoint, so a wrong match (e.g. StuMS exam titles like "Computational
    Economics" often share only a generic word like "Economics" with the
    actual, unrelated Stud.IP course name) needs a higher bar.
    """
    if not course_query:
        return None, None
    try:
        courses = await list_courses()
    except Exception as e:
        logging.warning("_match_course: list_courses failed: %s", e)
        return None, None
    match_cid = _best_title_match(course_query, courses, min_score=0.6)
    if not match_cid:
        return None, None
    course_name = next((name for name, cid in courses if cid == match_cid), course_query)
    return course_name, match_cid


async def _find_latest_file(course_query: Optional[str]):
    """Fuzzy-match a spoken course name (via _match_course) and return
    (course_name, cid, file_item) for the most recently modified file at
    that course's top-level Stud.IP file listing (list_files) — a live
    fetch, not the cached "last 5 files across all courses" that
    show_last_files uses, so it reflects the course's actual current state
    even if the watcher hasn't noticed a given file yet.

    Returns (None, None, None) if the course name can't be matched;
    (course_name, cid, None) if the course matched but has no files at its
    top level (subfolders aren't crawled).
    """
    course_name, match_cid = await _match_course(course_query)
    if not match_cid:
        return None, None, None

    try:
        items = await list_files(match_cid)
    except Exception as e:
        logging.warning("_find_latest_file: list_files failed for %s: %s", match_cid, e)
        return course_name, match_cid, None

    files = [it for it in items if it.get("type") == "file" and it.get("modified_iso")]
    if not files:
        return course_name, match_cid, None
    files.sort(key=lambda it: it["modified_iso"], reverse=True)
    return course_name, match_cid, files[0]


def _best_title_match(query: Optional[str], candidates: list, min_score: float = 0.45):
    """Fuzzy-match a spoken course/exam name against a list of (title, payload)
    candidates (e.g. (course_name, sem_id), (exam_title, exam_dict)). Returns
    the payload of the best match, or None if nothing scores at least
    `min_score` — callers must fall back to showing the full list in that
    case. Raise min_score for a path with no visible "is this what you
    meant?" checkpoint before acting (e.g. Stud.IP course names vs. StuMS
    exam titles for the same module often share only a generic word like
    "Economics", which scores deceptively high at the default threshold)."""
    if not query or not candidates:
        return None
    query_lower = query.strip().lower()
    if not query_lower:
        return None

    best_payload = None
    best_score = 0.0
    for title, payload in candidates:
        title_lower = (title or "").lower()
        if not title_lower:
            continue
        score = difflib.SequenceMatcher(None, query_lower, title_lower).ratio()
        if query_lower in title_lower or title_lower in query_lower:
            score = max(score, 0.85)
        if score > best_score:
            best_score = score
            best_payload = payload

    return best_payload if best_score >= min_score else None


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route an incoming photo by whatever pending_step is waiting (same
    context.user_data field the text/voice wizards use, just a new reader
    of it — there's no generic photo-wizard router like handle_pending_step,
    since only the wardrobe feature expects photos today)."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return

    step = context.user_data.get("pending_step")
    file_id = update.message.photo[-1].file_id

    if step == "wardrobe_add_item":
        status_msg = await update.message.reply_text("📸 Tagging...", disable_notification=True)
        image_bytes = await _download_telegram_file(context.bot, file_id)
        tags = await tag_wardrobe_item(image_bytes) if image_bytes else None
        if not tags:
            await status_msg.edit_text("⚠️ Couldn't recognize that item — try a clearer photo of just this one piece of clothing, or /status to cancel.")
            return  # pending_step stays set so a retry just works
        # Best effort: without it the GWDG try-on describes the item later.
        tags["description"] = await describe_wardrobe_item(tags, image_bytes)
        item = add_wardrobe_item(user_id, tags, file_id=file_id)
        context.user_data.pop("pending_step", None)
        subtype_note = f"/{item['subtype']}" if item.get("subtype") else ""
        await status_msg.edit_text(f"👔 Added: {item['label']} ({item['type']}{subtype_note}, {item['warmth']} warmth)", reply_markup=get_main_keyboard())
        return

    if step == "wardrobe_set_reference":
        set_wardrobe_reference_photo(user_id, file_id)
        context.user_data.pop("pending_step", None)
        await update.message.reply_text("🖼️ Reference photo updated — I'll reuse it every morning.", reply_markup=get_main_keyboard())
        return

    await update.message.reply_text("📸 Got a photo — if you want to add it to your wardrobe, go to /status → 👔 My Wardrobe → 📸 Add Clothing Item first.")


async def handle_voice_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirmed a voice transcription is correct. If it was answering a
    pending wizard step, feed it into that step as before; otherwise this is a
    fresh voice command, so classify its intent and route it."""
    from unibot.menus import handle_pending_step
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    pending = context.user_data.pop("pending_voice", None)
    if not pending:
        await query.edit_message_text("⚠️ This confirmation has expired — please send a new voice note.")
        return

    chat_id = update.effective_chat.id
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    if pending["step"]:
        context.user_data["pending_step"] = pending["step"]
        await handle_pending_step(update, context, text_override=pending["text"])
        return

    await query.edit_message_text(f"🎙️ Heard: “{pending['text']}”\n\n🤔 Understanding...")
    result = pending.get("intent_result") or await classify_voice_intent(pending["text"])
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await _route_voice_intent(update, context, result, pending["text"])


async def handle_voice_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User said the transcription was wrong — discard it and let them retry."""
    query = update.callback_query
    await query.answer("Discarded")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    context.user_data.pop("pending_voice", None)
    await query.edit_message_text("🎙️ Okay — send another voice note, or type your answer instead.")


class _UpdateMessageShim:
    """Wraps a callback-query-originated Update so handlers written for a
    plain message update (which read update.message) can be reused as-is.
    python-telegram-bot's Update.message is the raw message field and is
    None for callback-query updates — update.effective_message is the one
    that resolves across both, so this shim exposes that as .message while
    forwarding every other attribute to the real update."""

    def __init__(self, real_update: Update):
        self._real_update = real_update
        self.message = real_update.effective_message

    def __getattr__(self, name):
        return getattr(self._real_update, name)


async def _voice_intent_create_task(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: create_task."""
    from unibot.menus import handle_pending_step
    context.user_data["pending_step"] = "task_text"
    context.user_data.pop("task_wizard", None)
    await handle_pending_step(update, context, text_override=transcript)
    return True


async def _voice_intent_unclear(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: unclear."""
    if not alternatives:
        # Nothing plausible enough to even guess at — the old default of
        # silently turning it into a task would just create garbage, so
        # ask instead of assuming.
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📝 Just make it a task", callback_data="voice_clarify|create_task")]])
        context.user_data["pending_voice_clarify"] = {"transcript": transcript, "query": query_text}
        await message.reply_text("🤔 I didn't catch a clear command in that. Want me to save it as a task instead?", reply_markup=keyboard)
        return True

    context.user_data["pending_voice_clarify"] = {"transcript": transcript, "query": query_text}
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(INTENT_LABELS[alt], callback_data=f"voice_clarify|{alt}")] for alt in alternatives]
        + [[InlineKeyboardButton("📝 Just make it a task", callback_data="voice_clarify|create_task")]]
    )
    await message.reply_text("🤔 Not quite sure what you meant — did you mean:", reply_markup=keyboard)
    return True


async def _voice_intent_create_exam_reminder_task(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: create_exam_reminder_task."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    extracted = await extract_exam_reminder_task(transcript)
    course_query = extracted["course_query"] or query_text
    offset_value = extracted["offset_value"]
    if not course_query or offset_value is None:
        await message.reply_text(
            "🤷 Couldn't tell which exam or how far ahead to remind you — try e.g. "
            "\"remind me about the Computational Intelligence exam 3 days before\"."
        )
        return True
    try:
        session = await login_studip()
        exam_title, exam_dt, has_time = await _find_exam_datetime(session, course_query)
    except Exception as e:
        logging.error("_route_voice_intent: create_exam_reminder_task fetch failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    if not exam_title:
        await message.reply_text(
            f"🎙️ Couldn't find an exam matching \"{course_query}\" — check the spelling, "
            "or set the reminder manually via ➕ Add Task."
        )
        return True

    delta = timedelta(hours=offset_value) if extracted["offset_unit"] == "hours" else timedelta(days=offset_value)
    due_dt = (exam_dt - delta).replace(tzinfo=TZ_BERLIN)
    task = {
        "id": str(uuid.uuid4())[:8],
        "user_id": update.effective_user.id,
        "text": f"{exam_title} exam",
        "due_time": due_dt.isoformat(),
        "course": exam_title,
        "created_at": datetime.now(TZ_BERLIN).isoformat(),
        "notified": False,
    }
    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)

    exam_when = exam_dt.strftime("%d.%m.%Y %H:%M") if has_time else exam_dt.strftime("%d.%m.%Y (time unknown)")
    past_warning = "\n⚠️ That exam date has already passed — double-check this is the sitting you meant." if exam_dt.date() < berlin_now().date() else ""
    await message.reply_text(
        f"✅ Reminder set for {due_dt.strftime('%d.%m.%Y %H:%M')} — {exam_title} exam ({exam_when}){past_warning}"
    )
    return True


async def _voice_intent_check_status(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: check_status."""
    from unibot.system import status_command
    await status_command(_UpdateMessageShim(update), context)
    return True


async def _voice_intent_check_grades(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: check_grades."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📜 View Transcript", callback_data="transcript_summary")]])
    await message.reply_text("🎙️ Sounds like you want your grades:", reply_markup=keyboard)
    return True


async def _voice_intent_check_exam_dates(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: check_exam_dates."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📚 My Exam Dates", callback_data="exam_dates_list")]])
    await message.reply_text("🎙️ Sounds like you want your upcoming exam dates:", reply_markup=keyboard)
    return True


async def _voice_intent_show_last_file(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: show_last_file."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        course_name, cid, file_item = await _find_latest_file(query_text)
    except Exception as e:
        logging.error("_route_voice_intent: show_last_file failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    if not cid:
        await message.reply_text(f"🎙️ Couldn't match a course to \"{query_text}\" — try ⬇️ Files from the menu instead.")
        return True
    if not file_item:
        await message.reply_text(f"ℹ️ No files found at the top level of {course_name}.")
        return True

    sid = _short_id()
    link_cache[sid] = {
        "cid": cid,
        "name": file_item["name"],
        "url": file_item.get("url"),
        "ts": time.time(),
    }
    text = (
        "━━━━━━━━━━━━━━━━━\n"
        "<b>📁 Latest File</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"<b>📚 Course:</b> {html.escape(course_name)}\n"
        f"<b>📄 Name:</b> {html.escape(file_item['name'])}\n"
        f"<b>📅 Date:</b> {html.escape(file_item['modified'])}\n"
        f"<b>💾 Size:</b> {html.escape(str(file_item['size']))}\n"
        "━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")]])
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return True


async def _voice_intent_list_course_files(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: list_course_files."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        course_name, cid = await _match_course(query_text)
    except Exception as e:
        logging.error("_route_voice_intent: list_course_files match failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    if not cid:
        await message.reply_text(f"🎙️ Couldn't match a course to \"{query_text}\" — try ⬇️ Files from the menu instead.")
        return True
    try:
        root_url = f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
        items = await list_files(cid, root_url)
    except Exception as e:
        logging.error("_route_voice_intent: list_course_files fetch failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    if not items:
        await message.reply_text(f"ℹ️ No files found at the top level of {course_name}.")
        return True

    # Same cache shape / callback_data as the button-driven Files browser
    # (send_folder), so tapping an item here opens a subfolder or
    # downloads a file exactly like it would from that flow.
    cleanup_link_cache()
    user_id = update.effective_user.id
    nav_stack[user_id] = [root_url]
    nav_names[user_id] = [course_name]
    keyboard = []
    for f in items:
        sid = _short_id()
        cache_entry = {"cid": cid, "name": f["name"], "ts": time.time(), "user_id": user_id}
        if f.get("url"):
            cache_entry["url"] = _normalize_url(f["url"])
        link_cache[sid] = cache_entry
        if f["type"] == "folder":
            keyboard.append([InlineKeyboardButton(f"📁 {f['name']}"[:64], callback_data=f"folder|{sid}")])
        else:
            meta = f" ({f['size']}, {f['modified']})"
            keyboard.append([InlineKeyboardButton(f"📄 {f['name']}{meta}"[:64], callback_data=f"file|{sid}")])

    await message.reply_text(
        f"📁 <b>{html.escape(course_name)}</b>\n{len(items)} item(s) at top level:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True


async def _voice_intent_show_menu(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: show_menu."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        session = await login_studip()
        prefs = load_food_preferences()
        menu_text, prev, next_, menu_date, dish_items, header_text = await get_todays_menu_enhanced(
            session, avoid_codes=prefs["avoid_codes"], avoid_keywords=prefs["avoid_keywords"]
        )
        await message.reply_text(menu_text, parse_mode="HTML", reply_markup=get_menu_navigation_keyboard(prev, next_))
    except Exception as e:
        logging.error("_route_voice_intent: show_menu failed: %s", e)
        await message.reply_text(f"❌ Error loading menu: {str(e)[:200]}")
    return True


async def _voice_intent_recommend_meal(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: recommend_meal."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    recommendation = await build_food_recommendation()
    if not recommendation:
        await message.reply_text("❌ Couldn't reach the Mensa menu right now, try again in a bit.")
        return True
    await _deliver_food_recommendation(message, recommendation)
    return True


async def _voice_intent_course_enroll(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: course_enroll."""
    from unibot.menus import _show_semester_picker
    default = load_default_semester()
    if not default.get("id"):
        await message.reply_text("🎙️ No default semester set, so I can't match a course by name yet — pick a semester:")
        await _show_semester_picker(message, "course_sem", "📚 Pick a semester to see courses currently open for enrolment:")
        return True
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        session = await login_studip()
        open_courses = await get_open_courses(session, semester_id=default["id"])
        enrolled_cids = {cid for _, cid in await list_courses(semester_id=default["id"])}
        open_courses = [c for c in open_courses if c["sem_id"] not in enrolled_cids]
    except Exception as e:
        logging.error("_route_voice_intent: course_enroll fetch failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    match_sem_id = _best_title_match(query_text, [(c["course"], c["sem_id"]) for c in open_courses])
    if match_sem_id:
        matched = next(c for c in open_courses if c["sem_id"] == match_sem_id)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Enroll in {matched['course'][:40]}?", callback_data=f"course_enroll_ask|{match_sem_id}")]])
        await message.reply_text(f"🎙️ Found a match: {matched['course']}", reply_markup=keyboard)
    else:
        await message.reply_text("🎙️ Couldn't confidently match that course — here's the full list:")
        await _render_open_courses_for_semester(message, default["id"])
    return True


async def _voice_intent_course_deenroll(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: course_deenroll."""
    from unibot.menus import _show_semester_picker
    default = load_default_semester()
    semester_id = default.get("id")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        courses = await list_courses(semester_id=semester_id)
    except Exception as e:
        logging.error("_route_voice_intent: course_deenroll fetch failed: %s", e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    match_cid = _best_title_match(query_text, courses)
    if match_cid:
        title = next(t for t, cid in courses if cid == match_cid)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚪 Sign out of {title[:40]}?", callback_data=f"course_deenroll_ask|{match_cid}")]])
        await message.reply_text(f"🎙️ Found a match: {title}", reply_markup=keyboard)
    elif semester_id:
        await message.reply_text("🎙️ Couldn't confidently match that course — here's your enrolled list:")
        await _render_enrolled_courses_for_semester(message, semester_id)
    else:
        await message.reply_text("🎙️ Couldn't confidently match that course — pick a semester:")
        await _show_semester_picker(message, "deenroll_sem", "🚪 Pick a semester to see its enrolled courses:")
    return True


async def _voice_intent_exam_register(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: exam_register / exam_deregister."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        session = await login_studip()
        if intent == "exam_register":
            exams = await get_open_exam_registrations(session)
            action_type = "anmelden"
        else:
            exams = await get_registered_exams(session)
            action_type = "abmelden"
    except Exception as e:
        logging.error("_route_voice_intent: %s fetch failed: %s", intent, e)
        await message.reply_text(f"❌ Error: {str(e)[:200]}")
        return True
    matched_exam = _best_title_match(query_text, [(e["title"], e) for e in exams])
    if matched_exam:
        sid = str(uuid.uuid4())[:8]
        exam_action_cache[sid] = {"unit_id": matched_exam["unit_id"], "action_type": action_type, "title": matched_exam["title"]}
        label = "📝 Register" if intent == "exam_register" else "🗑️ Deregister"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{label}: {matched_exam['title'][:40]}?", callback_data=f"exam_ask|{sid}")]])
        await message.reply_text(f"🎙️ Found a match: {matched_exam['title']}", reply_markup=keyboard)
    else:
        await message.reply_text("🎙️ Couldn't confidently match that exam — here's the full list:")
        await send_exam_registration_menu(message)
    return True


async def _voice_intent_exam_dates_all(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: exam_dates_all."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 All Exams", callback_data="exam_dates_all")]])
    await message.reply_text("🎙️ Sounds like you want every exam date in the curriculum:", reply_markup=keyboard)
    return True


async def _voice_intent_upcoming_dashboard(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: upcoming_dashboard."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Upcoming", callback_data="upcoming_dashboard")]])
    await message.reply_text("🎙️ Sounds like you want a combined view of what's coming up:", reply_markup=keyboard)
    return True


async def _voice_intent_calendar_today(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: calendar_today."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📅 Today's Schedule", callback_data="calendar_today")]])
    await message.reply_text("🎙️ Sounds like you want today's schedule:", reply_markup=keyboard)
    return True


async def _voice_intent_calendar_weekly(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: calendar_weekly."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly")]])
    await message.reply_text("🎙️ Sounds like you want this week's schedule:", reply_markup=keyboard)
    return True


async def _voice_intent_read_daily_plan(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: read_daily_plan."""
    try:
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=state.global_session, week_start=week_start)
        voice_user_id = update.effective_user.id if update.effective_user else None
        await send_daily_plan_voice(message, events, today, user_id=voice_user_id)
    except Exception as e:
        logging.error("_route_voice_intent: read_daily_plan failed: %s", e)
        await message.reply_text(f"❌ Error generating voice schedule: {str(e)[:200]}")
    return True


async def _voice_intent_read_weekly_plan(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: read_weekly_plan."""
    try:
        today = berlin_now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=state.global_session, week_start=week_start)
        voice_user_id = update.effective_user.id if update.effective_user else None
        await send_weekly_plan_voice(message, events, week_start, user_id=voice_user_id)
    except Exception as e:
        logging.error("_route_voice_intent: read_weekly_plan failed: %s", e)
        await message.reply_text(f"❌ Error generating voice schedule: {str(e)[:200]}")
    return True


async def _voice_intent_ask_question(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: ask_question."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    qa_user_id = update.effective_user.id if update.effective_user else None
    try:
        context_data = await build_qa_context(user_id=qa_user_id)
        passages = await search_course_material(transcript)
        if passages:
            context_data += "\n\nCourse material excerpts (from the student's Stud.IP course files, closest to the question first):\n" + "\n\n".join(
                f"[{p['course']} | {p['file']}]\n{p['text']}" for p in passages
            )
        name = (get_user_name(qa_user_id) if qa_user_id is not None else None) or get_primary_user_name()
        answer = await answer_question_with_context(transcript, context_data, name=name)
    except Exception as e:
        logging.error("_route_voice_intent: ask_question failed: %s", e)
        await message.reply_text(f"❌ Error answering that: {str(e)[:200]}")
        return True
    if not answer:
        await message.reply_text("🤷 Couldn't come up with an answer right now — try again in a bit.")
        return True
    await send_text_and_voice(message, answer)
    return True


async def _voice_intent_set_default_semester(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: set_default_semester."""
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📆 Set Default Semester", callback_data="set_default_semester")]])
    await message.reply_text("🎙️ Sounds like you want to set your default semester:", reply_markup=keyboard)
    return True


async def _voice_intent_run_check(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: run_check."""
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    await _run_check_now(_UpdateMessageShim(update), context)
    return True


async def _voice_intent_fastenroll_list(update, context, result, transcript, intent, query_text, alternatives, message):
    """Voice intent: fastenroll_list."""
    await send_fastenroll_list(message, update.effective_chat.id)
    return True


_VOICE_INTENT_HANDLERS = {
    "create_task": _voice_intent_create_task,
    "unclear": _voice_intent_unclear,
    "create_exam_reminder_task": _voice_intent_create_exam_reminder_task,
    "check_status": _voice_intent_check_status,
    "check_grades": _voice_intent_check_grades,
    "check_exam_dates": _voice_intent_check_exam_dates,
    "show_last_file": _voice_intent_show_last_file,
    "list_course_files": _voice_intent_list_course_files,
    "show_menu": _voice_intent_show_menu,
    "recommend_meal": _voice_intent_recommend_meal,
    "course_enroll": _voice_intent_course_enroll,
    "course_deenroll": _voice_intent_course_deenroll,
    "exam_register": _voice_intent_exam_register,
    "exam_deregister": _voice_intent_exam_register,
    "exam_dates_all": _voice_intent_exam_dates_all,
    "upcoming_dashboard": _voice_intent_upcoming_dashboard,
    "calendar_today": _voice_intent_calendar_today,
    "calendar_weekly": _voice_intent_calendar_weekly,
    "read_daily_plan": _voice_intent_read_daily_plan,
    "read_weekly_plan": _voice_intent_read_weekly_plan,
    "ask_question": _voice_intent_ask_question,
    "set_default_semester": _voice_intent_set_default_semester,
    "run_check": _voice_intent_run_check,
    "fastenroll_list": _voice_intent_fastenroll_list,
}


async def _route_voice_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, transcript: str):
    """Dispatch a freshly-classified voice command (no wizard was pending) to
    the matching existing action.

    Destructive actions (enroll, sign out, exam register/deregister) are
    never executed directly here — this only figures out which existing
    button the user meant, by fuzzy-matching the spoken course/exam name
    against the same candidate lists the real menus use, and presents that
    one button using the exact callback_data the real menu would generate.
    Tapping it drives the real, unmodified confirm/submit handler. A
    low-confidence or missing match falls back to showing the full list, the
    same one the button-driven flow would have shown.
    """
    from unibot.menus import handle_pending_step
    intent = result.get("intent", "unclear")
    query_text = result.get("query")
    alternatives = result.get("alternatives") or []
    message = update.effective_message

    handler = _VOICE_INTENT_HANDLERS.get(intent)
    if handler is not None and await handler(update, context, result, transcript, intent, query_text, alternatives, message):
        return

    # Unreachable given VOICE_INTENTS, but degrade to task creation rather
    # than silently dropping the voice note if a new intent is ever added
    # here without a branch.
    context.user_data["pending_step"] = "task_text"
    context.user_data.pop("task_wizard", None)
    await handle_pending_step(update, context, text_override=transcript)


async def handle_voice_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped one of the "did you mean...?" buttons offered when
    classify_voice_intent came back "unclear" — re-run the same routing
    logic, this time with the intent forced to whichever one they picked."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    pending = context.user_data.pop("pending_voice_clarify", None)
    if not pending:
        await query.edit_message_text("⚠️ This suggestion has expired — please send a new voice note.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    chosen_intent = query.data.split("|", 1)[1]
    result = {"intent": chosen_intent, "query": pending["query"], "alternatives": []}
    await _route_voice_intent(update, context, result, pending["transcript"])
