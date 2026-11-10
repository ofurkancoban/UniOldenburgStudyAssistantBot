"""Fixes from the twelfth review (tasks): a reminder time already gone is
refused instead of firing at once, an AI-read deadline already gone leaves
a plain to-do, a malformed snooze button doesn't lose the reminder, and an
unreadable stored time doesn't break the task list."""

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

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import storage, tasks  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
