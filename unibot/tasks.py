"""Personal tasks and reminders.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import NetworkError, RetryAfter, TelegramError

from fast_enroll import clear_pending_job, run_fast_enroll
from studip_utils import TZ_BERLIN

from unibot.storage import (
    add_user_task,
    recall_user_callback,
    record_task_outcome,
    remember_user_callback,
    remove_user_tasks,
    save_user_tasks,
    students_with_tasks,
    user_tasks,
)
from unibot.telegram_ui import get_main_keyboard, markup_for, user_data
from unibot.auth import acting_as, get_user_session, is_user_allowed, login_studip
from unibot.studip_api import list_courses
from unibot.ai import _call_openrouter, _extract_json_object
from unibot.schedule import fastenroll_tasks
from unibot.monitoring import announcement_task_cache, task_suggestion_cache


# A fired reminder's task is kept in the student's own private record
# (storage.remember_user_callback, key "task:<id>") so its Done/Snooze
# buttons still work after the task left the list, and after a restart.


async def _run_fastenroll_task(chat_id: int, sem_id: str, target_time_str: str, target_date_str: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    async def on_progress(msg: str):
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"⏱️ {msg}", disable_notification=True)
        except Exception:
            logging.warning("fastenroll progress notify failed", exc_info=True)

    try:
        # always the student who scheduled it: a job restored after a restart
        # runs outside any update, where the acting user would be the bot's
        with acting_as(chat_id):
            session = await login_studip(user_id=chat_id)
            success, message = await run_fast_enroll(session, sem_id, target_time_str, target_date_str, on_progress=on_progress)
        icon = "✅" if success else "❌"
        await context.bot.send_message(chat_id=chat_id, text=f"{icon} {message}")
    except asyncio.CancelledError:
        logging.info("fastenroll job for chat %s, course %s was cancelled", chat_id, sem_id)
        raise
    except Exception as e:
        logging.error("fastenroll failed: %s", e, exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Fast enroll failed: {e}")
    finally:
        fastenroll_tasks.get(chat_id, {}).pop(sem_id, None)
        clear_pending_job(chat_id, sem_id)


# ── Personal tasks / reminders (todo list with an optional due time) ───────────
TASK_ASK_TEXT = "📝 What's the task?"
TASK_ASK_TIME = "⏰ When should I remind you? (e.g. 'tomorrow 15:00', '20.09 09:00', 'in 2 hours') Type 'skip' for no reminder time."
TASK_SKIP_WORDS = {"skip", "yok", "hayır", "hayir", "none", "-"}


def parse_task_due_time(text: str):
    """Parse a task's due time from free text. Returns None if the user opted to
    skip setting a time (a plain to-do with no reminder). Raises ValueError if the
    text doesn't match any supported pattern.
    """
    t = text.strip()
    if t.lower() in TASK_SKIP_WORDS:
        return None
    try:
        return _parse_task_due_time(t, datetime.now(TZ_BERLIN))
    except ValueError as exc:
        if str(exc).startswith((TASK_TIME_UNKNOWN, TASK_TIME_INVALID)):
            raise
        # datetime's own message ("day is out of range for month") means
        # nothing to a student; the date itself does not exist
        raise ValueError(f"{TASK_TIME_INVALID} {text}") from exc


TASK_TIME_UNKNOWN = "Could not understand the date/time:"
TASK_TIME_INVALID = "That date or time doesn't exist:"


def _parse_task_due_time(t: str, now: datetime) -> datetime:

    m = re.match(r"^(\d+)\s*(?:saat|hours?)\s*sonra$", t, re.IGNORECASE) or re.match(r"^in\s+(\d+)\s*hours?$", t, re.IGNORECASE)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    m = re.match(r"^(\d+)\s*(?:gün|gun|days?)\s*sonra$", t, re.IGNORECASE) or re.match(r"^in\s+(\d+)\s*days?$", t, re.IGNORECASE)
    if m:
        return now + timedelta(days=int(m.group(1)))

    m = re.match(r"^(?:yarın|yarin|tomorrow)\s+(\d{1,2}):(\d{2})$", t, re.IGNORECASE)
    if m:
        target_date = (now + timedelta(days=1)).date()
        return datetime(target_date.year, target_date.month, target_date.day, int(m.group(1)), int(m.group(2)), tzinfo=TZ_BERLIN)

    m = re.match(r"^(?:bugün|bugun|today)\s+(\d{1,2}):(\d{2})$", t, re.IGNORECASE)
    if m:
        target_date = now.date()
        return datetime(target_date.year, target_date.month, target_date.day, int(m.group(1)), int(m.group(2)), tzinfo=TZ_BERLIN)

    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$", t)
    if m:
        day, month, year, hour, minute = map(int, m.groups())
        return datetime(year, month, day, hour, minute, tzinfo=TZ_BERLIN)

    m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})$", t)
    if m:
        day, month, hour, minute = map(int, m.groups())
        datetime(2000, month, day, hour, minute)  # a leap year: raises only for a date that never exists
        # the next time this day comes round; 29.02 waits for a leap year
        for year in range(now.year, now.year + 9):
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=TZ_BERLIN)
            except ValueError:
                continue
            if candidate >= now:
                return candidate
        raise ValueError(f"{TASK_TIME_INVALID} {t}")

    raise ValueError(f"{TASK_TIME_UNKNOWN} {t}")


async def send_tasks_menu(sender):
    keyboard = [
        [InlineKeyboardButton("➕ Add Task", callback_data="task_add"), InlineKeyboardButton("📋 My Tasks", callback_data="task_list")],
        [InlineKeyboardButton("📘 Filter by Course", callback_data="task_by_course")],
    ]
    await sender.reply_text("✅ <b>Tasks &amp; Deadlines:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def send_task_list(message, user_id: int):
    tasks = user_tasks(user_id)
    if not tasks:
        await message.reply_text("ℹ️ No tasks yet. Use ➕ Add Task to create one.")
        return

    reminders = sorted((t for t in tasks if t.get("due_time")), key=lambda t: t["due_time"])
    plain = [t for t in tasks if not t.get("due_time")]

    for t in reminders:
        due = datetime.fromisoformat(t["due_time"])
        course_line = f" [{t['course']}]" if t.get("course") else ""
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"task_delete|{t['id']}")]])
        await message.reply_text(f"⏰ {due.strftime('%d.%m.%Y %H:%M')} - {t['text']}{course_line}", reply_markup=keyboard)

    for t in plain:
        course_line = f" [{t['course']}]" if t.get("course") else ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✔️ Done", callback_data=f"task_done|{t['id']}"),
            InlineKeyboardButton("❌ Delete", callback_data=f"task_delete|{t['id']}"),
        ]])
        await message.reply_text(f"📝 {t['text']}{course_line}", reply_markup=keyboard)


async def handle_task_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from unibot.menus import send_course_picker
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    if query.data == "task_add":
        context.user_data.pop("task_wizard", None)
        user_data(context)["pending_step"] = "task_text"
        await query.message.reply_text(TASK_ASK_TEXT, reply_markup=get_main_keyboard())
        return

    if query.data == "task_list":
        await send_task_list(query.message, user_id)
        return

    if query.data == "task_by_course":
        await send_course_picker(query.message, context)
        return

    action, task_id = query.data.split("|", 1)
    # the scope holds only the student's own tasks, so a button can never act on another's
    tasks = user_tasks(user_id)
    own = next((t for t in tasks if t["id"] == task_id), None)
    if own is None and action in ("task_done", "task_delete"):
        await query.edit_message_text("⚠️ This task no longer exists.")
        return
    if action == "task_done" and own is not None:
        record_task_outcome(user_id, own, time.time())
        save_user_tasks(user_id, [t for t in tasks if t is not own])
        await query.edit_message_text("✔️ Done!")
    elif action == "task_delete" and own is not None:
        save_user_tasks(user_id, [t for t in tasks if t is not own])
        await query.edit_message_text("🗑️ Deleted.")


async def _handle_task_wizard_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str, text: str) -> bool:
    """If `step` is one of the task wizard steps, handle it and return True."""
    state = context.user_data.setdefault("task_wizard", {})

    if step == "task_text":
        try:
            extracted = await extract_task_from_text(text)
        except Exception as e:
            logging.warning("extract_task_from_text failed: %s", e)
            extracted = None

        user_id = update.effective_user.id if update.effective_user else None
        if extracted and extracted.get("due_time"):
            # A date/time was already stated in the same message (e.g.
            # "yarın rapor teslim et saat 15:00'te") - skip the separate
            # "when is this due?" question entirely.
            state["text"] = extracted["text"]
            state["due_time"] = extracted["due_time"]
            context.user_data.pop("pending_step", None)
            await _send_task_course_picker(update.effective_message, context, user_id=user_id)
            return True

        # No date found (or extraction unavailable) - fall back to the
        # original text and the explicit follow-up question, unchanged.
        state["text"] = text
        user_data(context)["pending_step"] = "task_time"
        await update.effective_message.reply_text(TASK_ASK_TIME, reply_markup=get_main_keyboard())
        return True

    if step == "task_time":
        user_id = update.effective_user.id if update.effective_user else None
        task_text = state.get("text")
        if not task_text:
            context.user_data.pop("task_wizard", None)
            context.user_data.pop("pending_step", None)
            return False
        try:
            due_dt = parse_task_due_time(text)
        except ValueError as e:
            await update.effective_message.reply_text(f"⚠️ {e}")
            await update.effective_message.reply_text(TASK_ASK_TIME, reply_markup=get_main_keyboard())
            return True  # pending_step stays "task_time" - ask again

        # Text and time are set; course tagging is button-driven from here, so
        # the free-text wizard step ends and pending_step is cleared.
        state["due_time"] = due_dt.isoformat() if due_dt else None
        context.user_data.pop("pending_step", None)
        await _send_task_course_picker(update.effective_message, context, user_id=user_id)
        return True

    return False


async def _send_task_course_picker(message, context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int] = None):
    """Ask which of the student's currently-enrolled Stud.IP courses (if any)
    this task belongs to, before finally saving it."""
    try:
        user_sess = get_user_session(user_id) if user_id else None
        courses = await list_courses(session=user_sess, user_id=user_id)
    except Exception as e:
        logging.warning("Task course picker: failed to fetch courses: %s", e)
        courses = []

    state = context.user_data.setdefault("task_wizard", {})
    state["course_options"] = courses

    keyboard = [
        [InlineKeyboardButton(f"📘 {name[:40]}", callback_data=f"task_course|{i}")]
        for i, (name, cid) in enumerate(courses)
    ]
    keyboard.append([InlineKeyboardButton("⏭ No course (general)", callback_data="task_course_skip")])
    await message.reply_text("📚 Tag this to one of your courses? (optional)", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_task_course_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finalize and save the task once a course (or "no course") is chosen."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    state = context.user_data.get("task_wizard")
    if not state or "text" not in state:
        await query.edit_message_text("⚠️ This task setup has expired -- please start again with ➕ Add Task.")
        return

    course_name = None
    if query.data != "task_course_skip":
        idx = int(query.data.split("|", 1)[1])
        options = state.get("course_options", [])
        if 0 <= idx < len(options):
            course_name = options[idx][0]

    task = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        "text": state["text"],
        "due_time": state.get("due_time"),
        "course": course_name,
        "created_at": datetime.now(TZ_BERLIN).isoformat(),
        "notified": False,
    }
    add_user_task(user_id, task)
    context.user_data.pop("task_wizard", None)

    course_line = f" [{course_name}]" if course_name else ""
    if task["due_time"]:
        due_dt = datetime.fromisoformat(task["due_time"])
        await query.edit_message_text(f"✅ Reminder set: {due_dt.strftime('%d.%m.%Y %H:%M')} – {task['text']}{course_line}")
    else:
        await query.edit_message_text(f"✅ Task added: {task['text']}{course_line}")


def _save_task_and_confirmation_text(user_id: int, text: str, due_time: Optional[str], course: Optional[str]) -> str:
    """Save a task built from an AI extraction/conversion (announcement or
    forum suggestion) and return the confirmation line to show the user.
    Shared by handle_task_suggestion_add and handle_announcement_to_task."""
    task = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        "text": text,
        "due_time": due_time,
        "course": course,
        "created_at": datetime.now(TZ_BERLIN).isoformat(),
        "notified": False,
    }
    add_user_task(user_id, task)

    course_line = f" [{course}]" if course else ""
    if due_time:
        due_dt = datetime.fromisoformat(due_time)
        return f"✅ Reminder set: {due_dt.strftime('%d.%m.%Y %H:%M')} – {text}{course_line}"
    return f"✅ Task added: {text}{course_line}"


async def handle_task_suggestion_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tasksug_add|<sid> -- save the task the "📝 Add as task?" button
    on a new forum notification suggested (see extract_task_suggestion /
    task_suggestion_cache). The course is already known from the source
    post, so this saves directly with no course-picker step, unlike the
    ➕ Add Task wizard."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer()
        return

    sid = query.data.split("|", 1)[1]
    info = task_suggestion_cache.get(sid)  # every follower may add it
    if not info:
        await query.answer("This suggestion has expired.", show_alert=True)
        return
    await query.answer()

    confirmation = _save_task_and_confirmation_text(user_id, info["text"], info.get("due_time"), info.get("course"))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await query.message.reply_text(confirmation)


async def handle_announcement_to_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ann_to_task|<sid> - the always-present "📝 Add as Task" button
    on announcement notifications. Unlike the forum suggestion button (pre-
    computed, only shown when a deadline was already detected), this button
    is always there and the LLM conversion (convert_announcement_to_task)
    runs on tap, since it's only worth the call when the student actually
    wants it."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer()
        return

    sid = query.data.split("|", 1)[1]
    info = announcement_task_cache.get(sid)  # kept: a failed conversion can be retried
    if not info:
        await query.answer("This announcement has expired.", show_alert=True)
        return
    await query.answer("Converting to task...")

    try:
        result = await convert_announcement_to_task(info["course"], info.get("subject"), info["body"])
    except Exception as e:
        logging.warning("convert_announcement_to_task failed: %s", e)
        result = None

    if not result:
        await query.message.reply_text("⚠️ Couldn't convert this to a task right now - try again in a bit, or add it manually via ➕ Add Task.")
        return

    confirmation = _save_task_and_confirmation_text(user_id, result["text"], result.get("due_time"), info["course"])
    try:
        await query.edit_message_reply_markup(reply_markup=markup_for(
            user_id, InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])))
    except TelegramError:
        pass
    await query.message.reply_text(confirmation)


TASK_SNOOZE_OPTIONS = [("15m", "⏰ +15 min"), ("1h", "⏰ +1 hour"), ("1d", "⏰ Tomorrow")]


async def check_task_reminders(bot):
    """Send any reminder-tasks whose due time has passed, then drop them (one-shot).
    Plain to-dos (no due_time) are left untouched until the user marks them done.
    Each student's tasks are read and changed in their own scope. The fired
    task is kept in the student's private record (keyed by its own id)
    so the reminder message's "✔️ Done" / snooze buttons can still act on it
    after it's already been removed from the student's list."""
    now = datetime.now(TZ_BERLIN)
    for uid in students_with_tasks():
        fired_ids: set = set()
        for task in user_tasks(uid):
            due = _task_due(task)
            if due is None or due > now:
                continue
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✔️ Done", callback_data=f"task_reminder_done|{task['id']}")],
                [InlineKeyboardButton(label, callback_data=f"task_reminder_snooze|{task['id']}|{code}") for code, label in TASK_SNOOZE_OPTIONS],
            ])
            try:
                await bot.send_message(chat_id=uid, text=f"⏰ <b>Reminder:</b> {html.escape(str(task['text']))}", parse_mode="HTML", reply_markup=keyboard)
            except (NetworkError, RetryAfter) as e:
                # Telegram unreachable or rate limiting for now: the task stays and fires next minute
                logging.warning("Task reminder %s not sent yet: %s", task['id'], e)
                continue
            except Exception as e:
                # the chat is gone or the student blocked the bot: retrying won't help
                logging.error("Failed to send task reminder %s: %s", task['id'], e)
            else:
                remember_user_callback(uid, f"task:{task['id']}", task)
                record_task_outcome(uid, task)  # due now; done or not comes later
            fired_ids.add(task["id"])
        if fired_ids:
            # removed by id on a fresh read: tasks added while sending survive
            remove_user_tasks(uid, fired_ids)


def _task_due(task: dict) -> Optional[datetime]:
    """A task's due time as an aware Berlin datetime, or None when it has
    none or it can't be read. A time stored without a zone counts as Berlin
    time, so it can still be compared with the clock."""
    raw = task.get("due_time")
    if not raw:
        return None
    try:
        due = datetime.fromisoformat(str(raw))
    except ValueError:
        logging.warning("Task %s has an unreadable due time; skipped", task.get("id"))
        return None
    return due if due.tzinfo else due.replace(tzinfo=TZ_BERLIN)


def _snooze_offset(code: str) -> timedelta:
    return {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "1d": timedelta(days=1)}[code]


async def handle_task_reminder_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle task_reminder_done|<id> and task_reminder_snooze|<id>|<code>
    - the buttons on a fired reminder (see check_task_reminders /
    stored reminder). Snoozing re-saves the task with a new due_time;
    "Done" just acknowledges, since the task is already gone from the list."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer()
        return

    parts = query.data.split("|")
    action, task_id = parts[0], parts[1]
    # looked up in the tapping student's own record: another student's
    # reminder button (forwarded, in a group) finds nothing
    task = recall_user_callback(user_id, f"task:{task_id}", forget=True)
    if task:
        task = {k: v for k, v in task.items() if k != "ts"}
    if not task:
        await query.answer("This reminder has expired.", show_alert=True)
        return
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    if action == "task_reminder_done":
        record_task_outcome(user_id, task, time.time())
        await query.message.reply_text(f"✔️ Done: {task['text']}")
        return

    code = parts[2]
    new_due = datetime.now(TZ_BERLIN) + _snooze_offset(code)
    task["due_time"] = new_due.isoformat()
    add_user_task(user_id, task)
    await query.message.reply_text(f"⏰ Snoozed - reminding you again at {new_due.strftime('%d.%m.%Y %H:%M')}: {task['text']}")


TASK_SUGGESTION_SYSTEM_PROMPT = """You read one announcement or forum post from a university course and decide whether it mentions a concrete task or deadline the student should be reminded about - a submission deadline, an exam/quiz date, something they need to bring/do/upload by a specific day. Ignore anything with no actionable date (general course info, a topic being "covered later in the semester", opinions, small talk, a reply that doesn't add a new date).

Respond with strict JSON only, no other text: {"has_task": true/false, "task_text": "<short actionable summary in English, e.g. 'Submit problem set 3' or 'Upload presentation slides'>" or null, "due_date": "<YYYY-MM-DD>" or null, "due_time": "<HH:MM 24h>" or null}.

If has_task is false, every other field must be null. If a date is mentioned but no specific time, leave due_time null. Resolve relative dates ("by Friday", "next Monday", "in two weeks") against today's date, which you're given in the prompt."""


def _resolve_task_due_time(due_date: Optional[str], due_time_str: Optional[str]) -> Optional[str]:
    """Combine a YYYY-MM-DD date and optional HH:MM time (from an LLM
    task-extraction JSON response) into an ISO due_time string, defaulting
    to 09:00 if a date was given with no specific time. Returns None if
    due_date is missing or unparseable."""
    if not due_date:
        return None
    try:
        time_part = due_time_str or "09:00"
        due_dt = datetime.strptime(f"{due_date} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BERLIN)
        return due_dt.isoformat()
    except ValueError:
        return None


async def extract_task_suggestion(course_name: str, thread_title: Optional[str], body_text: str) -> Optional[dict]:
    """Ask the LLM whether an announcement/forum post body mentions an
    actionable deadline/task, for the "📝 Add as task?" suggestion button
    on new forum notifications. Returns {"text", "due_time"} (due_time is
    an ISO string or None) on a detected task, or None if no task was
    found or the call/parse failed - callers should just skip the
    suggestion button in that case, never treat it as an error."""
    today_str = datetime.now(TZ_BERLIN).strftime("%A, %B %d, %Y")
    user_text = f"Today is {today_str}.\nCourse: {course_name}\n" + (f"Thread: {thread_title}\n" if thread_title else "") + f"\n{body_text}"
    raw = await _call_openrouter(TASK_SUGGESTION_SYSTEM_PROMPT, user_text)
    parsed = _extract_json_object(raw)
    if not parsed or not parsed.get("has_task") or not parsed.get("task_text"):
        return None
    return {"text": str(parsed["task_text"])[:200], "due_time": _resolve_task_due_time(parsed.get("due_date"), parsed.get("due_time"))}


ANNOUNCEMENT_TO_TASK_SYSTEM_PROMPT = """You convert a university course announcement into a short, actionable personal to-do for the student reading it - rephrase it in task language ("Review ...", "Submit ...", "Prepare ...", "Read ...", "Bring ...") rather than restating the announcement. Always produce a task_text, even for a purely informational announcement with no deadline (e.g. "Review posted lecture slides for <course>") - this button is tapped deliberately by the student, so there's always something reasonable to turn it into.

If the announcement clearly mentions a concrete date/deadline (a submission date, an exam date, a specific day something is due), extract it; otherwise leave due_date/due_time null - not every announcement has one, and that's fine, it just becomes a plain to-do with no reminder time.

Respond with strict JSON only, no other text: {"task_text": "<short actionable task>", "due_date": "<YYYY-MM-DD>" or null, "due_time": "<HH:MM 24h>" or null}. Resolve relative dates ("by Friday", "next Monday") against today's date, which you're given in the prompt."""


async def convert_announcement_to_task(course_name: str, subject: Optional[str], body_text: str) -> Optional[dict]:
    """Ask the LLM to rephrase an announcement as a task, for the always-
    present "📝 Add as Task" button on announcement notifications (unlike
    extract_task_suggestion, this doesn't gate on whether a deadline was
    found - it's only called when the student deliberately taps the
    button, so it always tries to produce something). Returns
    {"text", "due_time"}, or None if the call/parse failed."""
    today_str = datetime.now(TZ_BERLIN).strftime("%A, %B %d, %Y")
    user_text = f"Today is {today_str}.\nCourse: {course_name}\n" + (f"Subject: {subject}\n" if subject else "") + f"\n{body_text}"
    raw = await _call_openrouter(ANNOUNCEMENT_TO_TASK_SYSTEM_PROMPT, user_text)
    parsed = _extract_json_object(raw)
    if not parsed or not parsed.get("task_text"):
        return None
    return {"text": str(parsed["task_text"])[:200], "due_time": _resolve_task_due_time(parsed.get("due_date"), parsed.get("due_time"))}


TASK_FROM_TEXT_SYSTEM_PROMPT = """You read a person's own task/to-do request (typed or spoken, Turkish or English) for a university assistant bot's task list, and extract a clean task description plus a due date/time - but only if they actually stated one in the same sentence.

Respond with strict JSON only, no other text: {"task_text": "<short, clean task description, same language as the request, just tidied up - e.g. 'yarın rapor teslim et' -> 'Rapor teslim et', 'add a task submit the report tomorrow' -> 'Submit the report'>", "due_date": "<YYYY-MM-DD>" or null, "due_time": "<HH:MM 24h>" or null}.

due_date/due_time are ONLY set if the person stated an actual date/time themselves (e.g. "tomorrow", "yarın 15:00", "next Friday", "20 Ekim", "in 3 days"); if they only described the task with no date at all, both must be null - never invent one. Resolve relative dates against today's date, given in the prompt."""


async def extract_task_from_text(text: str) -> Optional[dict]:
    """Ask the LLM to clean up a task description and pull out a due
    date/time if one was actually stated in the same sentence (e.g. "yarın
    rapor teslim et saat 15:00'te") - lets task creation (voice or typed,
    see _handle_task_wizard_reply's "task_text" step) skip the separate
    "when is this due?" question when the date was already given upfront,
    the same way create_exam_reminder_task already resolves an exam's date
    in one shot instead of asking a follow-up. Returns {"text", "due_time"}
    (due_time ISO or None), or None on any failure - callers should fall
    back to the existing explicit ask-for-time step in that case."""
    today_str = datetime.now(TZ_BERLIN).strftime("%A, %B %d, %Y")
    raw = await _call_openrouter(TASK_FROM_TEXT_SYSTEM_PROMPT, f"Today is {today_str}.\n\n{text}")
    parsed = _extract_json_object(raw)
    if not parsed or not parsed.get("task_text"):
        return None
    return {"text": str(parsed["task_text"])[:200], "due_time": _resolve_task_due_time(parsed.get("due_date"), parsed.get("due_time"))}


EXAM_REMINDER_TASK_SYSTEM_PROMPT = """You extract the details of an exam reminder request (spoken in Turkish or English) for a university assistant bot. The user wants a task/reminder created some amount of time before a named exam.

Extract:
- "course_query": the course or exam name mentioned (in whatever language/form it was said)
- "offset_value": a non-negative integer, how far before the exam to remind them (0 if they said "on the day of the exam")
- "offset_unit": either "days" or "hours" (default "days" if unstated but a number of days-like phrase is used; infer "hours" only if hours were explicitly said)

Examples:
"Computational Intelligence sınavından 3 gün önce hatırlat" -> {"course_query": "Computational Intelligence", "offset_value": 3, "offset_unit": "days"}
"remind me about the Macroeconomics exam 2 hours before" -> {"course_query": "Macroeconomics", "offset_value": 2, "offset_unit": "hours"}
"Lineer Cebir sınav günü hatırlat" -> {"course_query": "Lineer Cebir", "offset_value": 0, "offset_unit": "days"}

If the course/exam name or the offset can't be determined, use null for that field.

Respond with strict JSON only, no other text: {"course_query": "<name or null>", "offset_value": <int or null>, "offset_unit": "<days|hours>"}"""


async def extract_exam_reminder_task(transcript: str) -> dict:
    """Extract {"course_query", "offset_value", "offset_unit"} from a spoken
    "remind me about <exam> N days/hours before" request. Falls back to all
    None/"days" on any failure - the caller must treat missing course_query or
    offset_value as "couldn't understand" rather than guessing."""
    raw = await _call_openrouter(EXAM_REMINDER_TASK_SYSTEM_PROMPT, transcript)
    parsed = _extract_json_object(raw)
    if not parsed:
        return {"course_query": None, "offset_value": None, "offset_unit": "days"}
    offset_value = parsed.get("offset_value")
    try:
        offset_value = int(offset_value) if offset_value is not None else None
    except (TypeError, ValueError):
        offset_value = None
    offset_unit = parsed.get("offset_unit") if parsed.get("offset_unit") in ("days", "hours") else "days"
    return {
        "course_query": parsed.get("course_query") or None,
        "offset_value": offset_value,
        "offset_unit": offset_unit,
    }
