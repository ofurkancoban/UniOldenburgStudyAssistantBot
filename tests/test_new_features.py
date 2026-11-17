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


class MealFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for uid in (7821, 7822, 7823, 7824):
            activate_student(uid)

    def _press(self, uid, action, delivered=None):
        from unibot import mensa
        query = mock.AsyncMock()
        query.data = f"meal_fb|{action}"
        query.from_user = types.SimpleNamespace(id=uid)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=uid))
        with mock.patch.object(mensa, "is_user_allowed", lambda u: True), \
                mock.patch.object(mensa, "build_food_recommendation",
                                  mock.AsyncMock(return_value={"dish": "Next dish", "category": "COUNTER ONE",
                                                               "reasoning": "r", "loc_id": "2"})), \
                mock.patch.object(mensa, "_deliver_food_recommendation", delivered or mock.AsyncMock()):
            run_handler(mensa.handle_meal_feedback, update, types.SimpleNamespace(user_data={}))
        return query

    def _last_pick(self, uid, dish, description=""):
        from unibot import mensa
        mensa.save_personal_record(uid, mensa.FOOD_LAST_PICK_RECORD,
                                   {"dish": dish, "description": description, "loc_id": "2", "day": "2026-11-17"})

    def test_ingredients_are_found_in_a_dish_without_duplicates(self):
        from unibot import mensa
        found = mensa.dish_ingredients("Sliced chicken with mushrooms", "rice, tomato sauce")
        self.assertEqual(set(found), {"chicken", "mushroom", "rice", "tomato"})
        self.assertEqual(mensa.dish_ingredients("Rosemary potatoes").count("potato"), 1)
        self.assertEqual(mensa.dish_ingredients("Eintopf"), [])  # "ei" inside a word is not an egg

    def test_the_recommendation_comes_with_both_buttons(self):
        from unibot import mensa
        data = [b.callback_data for row in mensa.meal_feedback_keyboard().inline_keyboard for b in row]
        self.assertEqual(data, ["meal_fb|like", "meal_fb|another"])

    def test_like_teaches_the_ingredients_and_removes_the_buttons(self):
        from unibot import mensa
        self._last_pick(7821, "Sliced chicken with mushrooms", "rice")
        query = self._press(7821, "like")
        liked = mensa.load_food_preferences(7821)["like_keywords"]
        self.assertEqual(len(liked), 3)  # at most three words per tap
        self.assertTrue(set(liked) <= {"chicken", "mushroom", "rice"})
        query.edit_message_reply_markup.assert_awaited_with(reply_markup=None)
        self.assertIn("Noted", query.message.reply_text.call_args.args[0])

    def test_like_never_turns_an_avoided_ingredient_into_a_liked_one(self):
        from unibot import mensa
        mensa.add_food_words(7822, "avoid", "mushroom, fish")
        self._last_pick(7822, "Salmon with mushroom sauce", "rice")
        self._press(7822, "like")
        prefs = mensa.load_food_preferences(7822)
        self.assertIn("mushroom", prefs["avoid_keywords"])
        self.assertNotIn("mushroom", prefs["like_keywords"])
        self.assertNotIn("fish", prefs["like_keywords"])
        self.assertIn("rice", prefs["like_keywords"])

    def test_another_asks_again_and_delivers_a_new_pick(self):
        deliver = mock.AsyncMock()
        self._last_pick(7823, "Old dish")
        self._press(7823, "another", delivered=deliver)
        self.assertEqual(deliver.call_args.args[1]["dish"], "Next dish")

    def test_a_stale_button_says_so_instead_of_failing(self):
        from unibot import db
        db.get_user_scope(7824).delete_private("cache." + "food_last_pick")
        query = self._press(7824, "like")
        self.assertIn("too old", query.answer.call_args.args[0])

    def test_the_last_pick_is_remembered_with_its_description(self):
        from unibot import mensa
        mensa._remember_pick(7824, "Tofu Bowl", date(2026, 11, 17), "tofu, broccoli", "3")
        record = mensa.personal_record(7824, mensa.FOOD_LAST_PICK_RECORD)
        self.assertEqual(record["dish"], "Tofu Bowl")
        self.assertEqual(record["loc_id"], "3")


def _async(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
