"""Fixes from the ninth review: a callback is answered only once (Telegram
rejects a second answer, so expiry and access alerts never showed), the
admin user actions no longer reassign the frozen CallbackQuery.data,
impossible task dates get a readable message, and a reminder survives a
passing send failure or a due time stored without a zone."""

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
from tests.testenv import activate_student, run_handler, use_temp_db  # noqa: E402
use_temp_db()

from telegram.error import BadRequest, Forbidden, NetworkError  # noqa: E402

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import db, storage, system, tasks, wardrobe  # noqa: E402


class StrictQuery:
    """Behaves like Telegram: one answer per callback, data is read-only."""

    def __init__(self, data, user_id):
        object.__setattr__(self, "_data", data)
        self.from_user = types.SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []
        self.message = mock.AsyncMock()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):
        raise AttributeError("Attribute `data` of class `CallbackQuery` can't be set!")

    async def answer(self, text=None, show_alert=False, **kw):
        if self.answers:
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)

    async def edit_message_reply_markup(self, **kw):
        pass


def _run(handler, query):
    run_handler(handler, types.SimpleNamespace(callback_query=query, effective_user=query.from_user), mock.MagicMock())


class AdminCallbackTests(unittest.TestCase):
    def setUp(self):
        db.upsert_user(6101, role="student", username="stud9", full_name="Student Nine", is_active=1)
        self.admin = mock.patch.object(system, "is_user_admin", side_effect=lambda uid: uid == 9001)
        self.admin.start()
        self.addCleanup(self.admin.stop)

    def test_toggle_answers_once_and_redraws_detail(self):
        query = StrictQuery("admin_toggle_user|6101", 9001)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(db.get_user(6101)["is_active"], 0)
        self.assertEqual(query.answers, [("User blocked.", True)])
        self.assertIn("Student Profile: @stud9", query.edits[-1])
        self.assertIn("Blocked", query.edits[-1])

    def test_role_and_revoke_redraw_detail(self):
        query = StrictQuery("admin_role_user|6101", 9001)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(db.get_user(6101)["role"], "admin")
        self.assertEqual(len(query.answers), 1)
        with mock.patch.object(system, "_perform_logout", mock.AsyncMock()) as logout:
            query = StrictQuery("admin_revoke_user|6101", 9001)
            _run(system.handle_admin_callbacks, query)
        logout.assert_awaited_once_with(user_id=6101)
        self.assertEqual(query.answers, [("Stud.IP session revoked and credentials cleared.", True)])
        self.assertTrue(query.edits)

    def test_detail_view_answers_plainly(self):
        query = StrictQuery("admin_user_detail|6101", 9001)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(query.answers, [(None, False)])

    def test_missing_user_alert_is_the_only_answer(self):
        query = StrictQuery("admin_user_detail|424242", 9001)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(query.answers, [("User not found.", True)])

    def test_non_admin_alert_is_the_only_answer(self):
        query = StrictQuery("admin_menu", 6101)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(query.answers, [("⛔ Access denied. Admin-only feature.", True)])

    def test_other_views_answer_once(self):
        query = StrictQuery("admin_users", 9001)
        _run(system.handle_admin_callbacks, query)
        self.assertEqual(len(query.answers), 1)


class SingleAnswerTests(unittest.TestCase):
    def test_wardrobe_denial_is_the_only_answer(self):
        handlers = (wardrobe.handle_wardrobe_menu, wardrobe.handle_wardrobe_add_item,
                    wardrobe.handle_wardrobe_set_reference, wardrobe.handle_wardrobe_remove,
                    wardrobe.handle_wardrobe_suggest_now)
        with mock.patch.object(wardrobe, "is_user_admin", return_value=False):
            for handler in handlers:
                query = StrictQuery("wardrobe_remove|x", 6101)
                _run(handler, query)
                self.assertEqual(query.answers, [("⛔ This feature is restricted to administrators.", True)], handler.__name__)

    def test_expired_task_buttons_show_their_alert(self):
        cases = ((tasks.handle_task_suggestion_add, "tasksug_add|gone", "This suggestion has expired."),
                 (tasks.handle_announcement_to_task, "ann_to_task|gone", "This announcement has expired."),
                 (tasks.handle_task_reminder_action, "task_reminder_done|gone", "This reminder has expired."))
        with mock.patch.object(tasks, "is_user_allowed", return_value=True):
            for handler, data, alert in cases:
                query = StrictQuery(data, 6101)
                _run(handler, query)
                self.assertEqual(query.answers, [(alert, True)])

    def test_live_reminder_is_answered_once(self):
        storage.remember_user_callback(6101, "task:r1", {"id": "r1", "text": "Read", "due_time": None})
        with mock.patch.object(tasks, "is_user_allowed", return_value=True):
            query = StrictQuery("task_reminder_snooze|r1|15m", 6101)
            _run(tasks.handle_task_reminder_action, query)
        self.assertEqual(query.answers, [(None, False)])
        self.assertEqual([t["id"] for t in storage.user_tasks(6101)], ["r1"])
        storage.save_user_tasks(6101, [])


class TaskDueTimeTests(unittest.TestCase):
    def test_impossible_dates_get_a_readable_message(self):
        for text in ("31.02 10:00", "31.02.2027 10:00", "tomorrow 25:00", "today 10:61", "29.02 24:00"):
            with self.assertRaises(ValueError) as caught:
                tasks.parse_task_due_time(text)
            self.assertTrue(str(caught.exception).startswith(tasks.TASK_TIME_INVALID), text)

    def test_unknown_text_keeps_its_message(self):
        with self.assertRaises(ValueError) as caught:
            tasks.parse_task_due_time("whenever")
        self.assertTrue(str(caught.exception).startswith(tasks.TASK_TIME_UNKNOWN))

    def test_leap_day_waits_for_a_leap_year(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN)
        due = tasks._parse_task_due_time("29.02 10:00", now)
        self.assertEqual((due.year, due.month, due.day), (2028, 2, 29))

    def test_past_day_moves_to_next_year(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN)
        self.assertEqual(tasks._parse_task_due_time("05.03 09:00", now).year, 2027)
        self.assertEqual(tasks._parse_task_due_time("20.12 09:00", now).year, 2026)


class ReminderDeliveryTests(unittest.TestCase):
    def setUp(self):
        activate_student(6102)
        self.past = (datetime.now(TZ_BERLIN) - timedelta(minutes=1)).isoformat()
        self.addCleanup(storage.save_user_tasks, 6102, [])

    def _fire(self, error=None):
        class Bot:
            async def send_message(self, **kw):
                if error:
                    raise error

        asyncio.run(tasks.check_task_reminders(Bot()))

    def test_network_error_keeps_the_task(self):
        storage.save_user_tasks(6102, [{"id": "n1", "user_id": 6102, "text": "x", "due_time": self.past}])
        self._fire(NetworkError("down"))
        self.assertEqual([t["id"] for t in storage.user_tasks(6102)], ["n1"])
        self.assertIsNotNone(storage.recall_user_callback(6102, "task:n1"))

    def test_blocked_chat_preserves_the_task(self):
        storage.save_user_tasks(6102, [{"id": "b1", "user_id": 6102, "text": "x", "due_time": self.past}])
        self._fire(Forbidden("bot was blocked by the user"))
        self.assertEqual([t["id"] for t in storage.user_tasks(6102)], ["b1"])
        self.assertIsNone(storage.recall_user_callback(6102, "task:b1"))

    def test_sent_reminder_keeps_its_buttons_working(self):
        storage.save_user_tasks(6102, [{"id": "s1", "user_id": 6102, "text": "x", "due_time": self.past}])
        self._fire()
        self.assertEqual(storage.user_tasks(6102), [])
        self.assertIsNotNone(storage.recall_user_callback(6102, "task:s1"))

    def test_zoneless_and_broken_due_times_dont_stop_the_loop(self):
        naive = (datetime.now(TZ_BERLIN) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        storage.save_user_tasks(6102, [
            {"id": "bad", "user_id": 6102, "text": "x", "due_time": "not a date"},
            {"id": "old", "user_id": 6102, "text": "x", "due_time": naive},
        ])
        self._fire()
        self.assertEqual([t["id"] for t in storage.user_tasks(6102)], ["bad"])


if __name__ == "__main__":
    unittest.main()
