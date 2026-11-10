"""Fixes from the twelfth review. Tasks: a reminder time already gone is
refused instead of firing at once, an AI-read deadline already gone leaves
a plain to-do, a malformed snooze button doesn't lose the reminder, and an
unreadable stored time doesn't break the task list. Morning: a cancelled
class is shown as cancelled and not spoken as if it took place, and one
unreadable task doesn't hide the day's other tasks."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

import exam_reminder  # noqa: E402
from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import course_registry, enrollment, morning, storage, tasks, voice  # noqa: E402

UID = 9701


class PastReminderTests(unittest.TestCase):
    def test_time_already_gone_is_refused(self):
        past = datetime.now(TZ_BERLIN) - timedelta(days=2)
        for text in (past.strftime("%d.%m.%Y %H:%M"),):
            with self.assertRaises(ValueError) as caught:
                tasks.parse_task_due_time(text)
            self.assertTrue(str(caught.exception).startswith(tasks.TASK_TIME_PAST))

    def test_future_and_skip_still_work(self):
        self.assertIsNone(tasks.parse_task_due_time("skip"))
        self.assertGreater(tasks.parse_task_due_time("in 2 hours"), datetime.now(TZ_BERLIN))

    def test_ai_deadline_gone_leaves_a_plain_task(self):
        yesterday = (datetime.now(TZ_BERLIN) - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(TZ_BERLIN) + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertIsNone(tasks._resolve_task_due_time(yesterday, "10:00"))
        self.assertTrue(tasks._resolve_task_due_time(tomorrow, None).startswith(tomorrow + "T09:00"))


class SnoozeButtonTests(unittest.TestCase):
    def test_malformed_snooze_keeps_the_reminder(self):
        storage.remember_user_callback(UID, "task:t1", {"id": "t1", "text": "Lab report"})
        for data in ("task_reminder_snooze|t1", "task_reminder_snooze|t1|99y"):
            query = mock.AsyncMock()
            query.data = data
            query.from_user = types.SimpleNamespace(id=UID)
            with mock.patch.object(tasks, "is_user_allowed", return_value=True):
                asyncio.run(tasks.handle_task_reminder_action(types.SimpleNamespace(callback_query=query), None))
        self.assertTrue(storage.recall_user_callback(UID, "task:t1", forget=False))

    def test_bad_course_index_saves_without_course(self):
        query = mock.AsyncMock()
        query.data = "task_course|x"
        query.from_user = types.SimpleNamespace(id=UID)
        context = types.SimpleNamespace(user_data={"task_wizard": {"text": "Read", "course_options": [("A", "a")]}})
        self.addCleanup(storage.save_user_tasks, UID, [])
        with mock.patch.object(tasks, "is_user_allowed", return_value=True):
            asyncio.run(tasks.handle_task_course_pick(types.SimpleNamespace(callback_query=query), context))
        self.assertEqual([t["course"] for t in storage.user_tasks(UID)], [None])


class TaskListTests(unittest.TestCase):
    def test_unreadable_time_is_skipped(self):
        self.addCleanup(storage.save_user_tasks, UID, [])
        storage.save_user_tasks(UID, [{"id": "a", "text": "Broken", "due_time": "not a date"},
                                      {"id": "b", "text": "Fine", "due_time": "2030-01-01T10:00:00+01:00"}])
        message = mock.AsyncMock()
        asyncio.run(tasks.send_task_list(message, UID))
        texts = [c.args[0] for c in message.reply_text.call_args_list]
        self.assertEqual(len(texts), 1)
        self.assertIn("Fine", texts[0])


def _class(title, hour, cancelled=False):
    start = datetime(2026, 11, 11, hour, tzinfo=TZ_BERLIN)
    return {"title": f"Vorlesung: {title}", "time": f"{hour}:00 - {hour + 2}:00", "location": "A1",
            "date_key": start.date(), "start_dt": start, "end_dt": start + timedelta(hours=2),
            "is_cancelled": cancelled}


class CancelledClassTests(unittest.TestCase):
    EVENTS = [_class("Statistics", 10), _class("Econometrics", 14, cancelled=True)]

    def test_summary_marks_it(self):
        text = morning._format_schedule_section(self.EVENTS)
        self.assertIn("<s>Econometrics</s> <b>(cancelled)</b>", text)
        self.assertIn("<b>Statistics</b>", text)

    def test_plain_speech_says_cancelled(self):
        day = self.EVENTS[0]["date_key"]
        text = morning.build_daily_plan_speech_text(self.EVENTS, day)
        self.assertIn("You have 1 event", text)
        self.assertIn("Cancelled: Econometrics", text)
        week = morning.build_weekly_plan_speech_text(self.EVENTS, day, day)
        self.assertNotIn("Statistics, Econometrics", week)
        self.assertIn("Cancelled: Econometrics", week)

    def test_ai_is_told_it_is_cancelled(self):
        prompts = []

        async def call(system, user, temperature=None):
            prompts.append(user)
            return "ok"

        day = self.EVENTS[0]["date_key"]
        with mock.patch.object(morning, "_call_openrouter", call), \
                mock.patch.object(morning, "find_weekly_study_gaps", lambda e: []):
            asyncio.run(morning.build_ai_daily_plan_narration(self.EVENTS, day))
            asyncio.run(morning.build_ai_weekly_plan_narration(self.EVENTS, day, day))
        for prompt in prompts:
            schedule = prompt.split("Cancelled")[0]
            self.assertNotIn("Econometrics", schedule)
            self.assertIn("Cancelled", prompt)


class MorningTasksTests(unittest.TestCase):
    def test_one_unreadable_task_leaves_the_others(self):
        self.addCleanup(storage.save_user_tasks, UID, [])
        day = datetime(2026, 11, 11).date()
        storage.save_user_tasks(UID, [{"id": "a", "text": "Broken", "due_time": "nope"},
                                      {"id": "b", "text": "Hand in", "due_time": "2026-11-11T15:00:00"}])
        found = morning._load_today_tasks(day, user_id=UID)
        self.assertEqual([t["id"] for t in found], ["b"])
        self.assertIn("15:00", morning._format_tasks_section(found))



class VoiceExamReminderTests(unittest.TestCase):
    def test_exam_already_over_gets_no_reminder(self):
        exam_dt = (datetime.now(TZ_BERLIN) - timedelta(days=5)).replace(tzinfo=None)
        message = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID))
        added = []
        with mock.patch.object(voice, "extract_exam_reminder_task", mock.AsyncMock(return_value={
                    "course_query": "Algorithms", "offset_value": 1, "offset_unit": "days"})), \
                mock.patch.object(voice, "login_studip", mock.AsyncMock()), \
                mock.patch.object(voice, "_find_exam_datetime", mock.AsyncMock(return_value=("Algorithms", exam_dt, True))), \
                mock.patch.object(voice, "add_user_task", lambda uid, task: added.append(task)):
            asyncio.run(voice._voice_intent_create_exam_reminder_task(
                update, types.SimpleNamespace(bot=mock.AsyncMock()), {}, "remind me", "create_exam_reminder_task",
                None, [], message))
        self.assertEqual(added, [])
        self.assertIn("already passed", message.reply_text.call_args.args[0])



class GradeNoticeTests(unittest.TestCase):
    def _check(self, grades, cache):
        sent = []

        async def broadcast(bot, text, parse_mode=None, reply_markup=None):
            sent.append(text)

        def save(data, user_id=None):
            cache.clear()
            cache.update(data)

        with mock.patch.object(exam_reminder, "get_grades", mock.AsyncMock(return_value=grades)), \
                mock.patch.object(exam_reminder, "load_exam_cache", lambda *a, **k: dict(cache)), \
                mock.patch.object(exam_reminder, "save_exam_cache", save):
            asyncio.run(exam_reminder.check_grade_reminders(types.SimpleNamespace(user_id=UID), None, broadcast))
        return sent

    def test_first_grade_of_a_new_student_is_announced(self):
        cache: dict = {}
        self.assertEqual(self._check([], cache), [])  # nothing final yet: the baseline is empty
        first = [{"number": "M1", "title": "Statistics", "status_title": "passed", "grade": "1,7"}]
        sent = self._check(first, cache)
        self.assertTrue(any("Statistics" in t and "1,7" in t for t in sent))

    def test_a_result_without_a_grade_is_not_announced_again(self):
        cache: dict = {}
        grades = [{"number": "M2", "title": "Seminar", "status_title": "passed", "grade": ""},
                  {"number": "M3", "title": "Lab", "status_title": "passed", "grade": " 2,0 "}]
        self._check(grades, cache)  # baseline
        self.assertEqual(self._check(grades, cache), [])



class FastEnrollOwnerTests(unittest.TestCase):
    MINE, THEIRS = 9711, 9712
    PENDING = {str(MINE): {"a" * 32: {"target_time_str": "08:00:00"}},
               str(THEIRS): {"b" * 32: {"target_time_str": "09:00:00"}}}

    def test_list_shows_only_own_jobs(self):
        with mock.patch.object(enrollment, "load_pending", return_value=self.PENDING):
            text, markup = enrollment._build_fastenroll_list_content(self.MINE)
        self.assertIn("a" * 32, text)
        self.assertNotIn("b" * 32, text)
        self.assertNotIn(str(self.THEIRS), str(markup.to_dict()))

    def test_another_students_job_cannot_be_cancelled(self):
        cancel = mock.AsyncMock(return_value="cancelled")
        query = mock.AsyncMock()
        query.data = f"fastenroll_cancel|{self.THEIRS}|{'b' * 32}"
        query.message.chat_id = self.MINE
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=self.MINE))
        with mock.patch.object(enrollment, "is_user_allowed", return_value=True), \
                mock.patch.object(enrollment, "cancel_fastenroll_job", cancel):
            asyncio.run(enrollment.fastenroll_cancel_callback(update, None))
        cancel.assert_not_awaited()

        message = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=self.MINE),
                                       effective_chat=types.SimpleNamespace(id=self.MINE), message=message)
        with mock.patch.object(enrollment, "is_user_allowed", return_value=True), \
                mock.patch.object(enrollment, "load_pending", return_value=self.PENDING), \
                mock.patch.object(enrollment, "cancel_fastenroll_job", cancel):
            asyncio.run(enrollment.fastenroll_cancel_command(update, types.SimpleNamespace(args=["b" * 32])))
        self.assertEqual(cancel.call_args.args[0], self.MINE)


class CourseAlertRedrawTests(unittest.TestCase):
    def test_opening_the_same_course_again_is_quiet(self):
        from telegram.error import BadRequest
        query = mock.AsyncMock()
        query.data = "calert|c|c1"
        query.from_user = types.SimpleNamespace(id=UID)
        query.edit_message_text.side_effect = BadRequest("Message is not modified")
        with mock.patch.object(course_registry, "is_user_allowed", return_value=True):
            asyncio.run(course_registry.handle_course_alerts(types.SimpleNamespace(callback_query=query), None))
        query.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
