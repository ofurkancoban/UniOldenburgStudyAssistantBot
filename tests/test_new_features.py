"""Features added after the full review: AI limits that survive a restart and are visible to the
administrators, personal data export, meal feedback, announcement summaries, calendar feed with
lectures and exams, exam-aware study plan, transit departures, nightly canary and page-structure
checks, staging tools."""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, use_temp_db  # noqa: E402
use_temp_db()

from unibot import ai, db, system  # noqa: E402


class AiBudgetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: (ai._ai_calls.clear(), setattr(ai, "_ai_calls_loaded", False),
                                 db.delete(ai.AI_CALLS_PATH)))
        ai._ai_calls.clear()
        ai._ai_calls_loaded = False
        db.delete(ai.AI_CALLS_PATH)

    def test_the_counts_survive_a_restart(self):
        activate_student(7801)
        with mock.patch.object(ai, "_budget_user", lambda uid=None: 7801):
            for _ in range(ai.AI_CALLS_SAVE_EVERY):
                self.assertTrue(ai.use_ai_budget(7801))
        ai._ai_calls.clear()  # a new process starts with nothing in memory
        ai._ai_calls_loaded = False
        with mock.patch.object(ai, "_budget_user", lambda uid=None: 7801):
            ai.ai_budget_left(7801)
        self.assertEqual(len(ai._ai_calls[7801]), ai.AI_CALLS_SAVE_EVERY)

    def test_old_calls_are_not_picked_up(self):
        db.save(ai.AI_CALLS_PATH, {"7802": [time.time() - 90000, time.time() - 10]})
        self.assertEqual(ai.ai_usage_last_day(), [(7802, 1)])

    def test_the_limit_can_be_set_in_the_environment(self):
        with mock.patch.dict(os.environ, {"AI_CALLS_PER_DAY": "12", "AI_CALLS_PER_HOUR": "abc"}):
            self.assertEqual(ai._env_limit("AI_CALLS_PER_DAY", 300), 12)
            self.assertEqual(ai._env_limit("AI_CALLS_PER_HOUR", 60), 60)  # junk falls back
            self.assertEqual(ai._env_limit("AI_CALLS_NOT_SET", 7), 7)

    def test_the_admin_dashboard_lists_the_heaviest_users_and_the_system_state(self):
        activate_student(7803)
        db.save(ai.AI_CALLS_PATH, {"7803": [time.time() - 5 - i for i in range(9)]})
        ai._ai_calls_loaded = False
        with mock.patch.object(system, "load_general_cache",
                               return_value={"health_state": {"studip": True, "offsite_backup": False}}), \
                mock.patch.object(system, "is_user_admin", lambda uid: False):
            text = system._admin_health_block()
        self.assertIn("Off-site backup (Google Drive)", text)  # the failing check is named
        self.assertIn(": 9", text)
        self.assertIn("cap per student", text)


if __name__ == "__main__":
    unittest.main()
