"""Features added after the full review: AI limits that survive a restart and are visible to the
administrators, personal data export, meal feedback, announcement summaries, calendar feed with
lectures and exams, exam-aware study plan, transit departures, nightly canary and page-structure
checks, staging tools."""

# ruff: noqa: F401
import asyncio
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_as, run_handler, use_temp_db  # noqa: E402
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
        from unibot import config
        with mock.patch.dict(os.environ, {"AI_CALLS_PER_DAY": "12", "AI_CALLS_PER_HOUR": "abc"}):
            self.assertEqual(config._positive_int("AI_CALLS_PER_DAY", 300), 12)
            self.assertEqual(config._positive_int("AI_CALLS_PER_HOUR", 60), 60)  # junk falls back
            self.assertEqual(config._positive_int("AI_CALLS_NOT_SET", 7), 7)
        with mock.patch.dict(os.environ, {"AI_CALLS_PER_DAY": "-4"}):
            self.assertEqual(config._positive_int("AI_CALLS_PER_DAY", 300), 300)

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


class DataExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from unibot import storage
        cls.storage = storage
        for uid in (7811, 7812):
            activate_student(uid)
        storage.save_user_name(7811, "ExportAnna")
        storage.save_personal_record(7811, "study_blocks", [{"course": "export-canary-course"}])
        storage.save_user_name(7812, "OtherBob")
        storage.save_personal_record(7812, "study_blocks", [{"course": "bobs-secret-course"}])

    def test_the_export_holds_the_students_own_records_readable(self):
        from unibot import privacy
        data = privacy.export_user_data(7811)
        text = json.dumps(data, ensure_ascii=False)
        self.assertIn("ExportAnna", text)
        self.assertIn("export-canary-course", text)
        self.assertIn("study_blocks", data["records"])  # the cache. prefix is dropped
        self.assertEqual(data["account"]["role"], "user")

    def test_nothing_of_another_student_is_in_it(self):
        from unibot import privacy
        text = json.dumps(privacy.export_user_data(7811), ensure_ascii=False)
        self.assertNotIn("OtherBob", text)
        self.assertNotIn("bobs-secret-course", text)

    def test_no_login_secret_is_exported(self):
        from unibot import privacy
        data = privacy.export_user_data(7811)
        text = json.dumps({"account": data["account"], "records": data["records"]}, ensure_ascii=False).lower()
        for word in ("cookie", "password", "refresh_token", "access_token", "vault_key"):
            self.assertNotIn(word, text)

    def test_the_download_button_sends_a_json_file_once_an_hour(self):
        from unibot import privacy
        privacy._export_at.pop(7811, None)
        query = mock.AsyncMock()
        query.data = "privacy|export"
        query.from_user = types.SimpleNamespace(id=7811)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=7811))
        with mock.patch.object(privacy, "is_user_allowed", lambda uid: True):
            run_handler(privacy.handle_privacy, update, types.SimpleNamespace(user_data={}, bot=mock.AsyncMock()))
            sent = query.message.reply_document.call_args.kwargs["document"]
            self.assertTrue(sent.name.endswith(".json"))
            self.assertIn("ExportAnna", sent.getvalue().decode("utf-8"))
            query.message.reply_document.reset_mock()
            run_handler(privacy.handle_privacy, update, types.SimpleNamespace(user_data={}, bot=mock.AsyncMock()))
            query.message.reply_document.assert_not_awaited()  # the second tap within the hour
            self.assertIn("again in", query.answer.call_args.args[0])

    def test_the_privacy_screen_offers_the_download(self):
        from unibot import privacy
        _text, markup = run_as(7811, _async(lambda: privacy.privacy_view(7811)))
        self.assertIn("privacy|export", [b.callback_data for row in markup.inline_keyboard for b in row])


def _async(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
