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


class CalendarFeedTests(unittest.TestCase):
    UID = 7831

    @classmethod
    def setUpClass(cls):
        activate_student(cls.UID)

    def _ics(self, entries):
        from unibot import study_week
        raw = study_week.build_ics(entries, self.UID, now=datetime(2026, 11, 17, 7, 0, tzinfo=study_week.TZ_BERLIN))
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in raw.split("\r\n")))  # folded as RFC 5545 asks
        return raw.replace("\r\n ", "")  # unfolded, so a sentence can be searched for

    def _lecture(self, **extra):
        from unibot import study_week
        tz = study_week.TZ_BERLIN
        return {"kind": "lecture", "title": "Applied Econometrics", "location": "A06 0-006",
                "start_dt": datetime(2026, 11, 18, 10, 15, tzinfo=tz), "end_dt": datetime(2026, 11, 18, 11, 45, tzinfo=tz),
                "cancelled": False, "event_id": "ev1", **extra}

    def test_a_lecture_becomes_a_busy_event_with_its_room(self):
        ics = self._ics([self._lecture()])
        self.assertIn("SUMMARY:🎓 Applied Econometrics", ics)
        self.assertIn("LOCATION:A06 0-006", ics)
        self.assertIn("DTSTART:20261118T091500Z", ics)  # 10:15 Berlin in winter time
        self.assertIn("STATUS:CONFIRMED", ics)
        self.assertIn("TRANSP:OPAQUE", ics)

    def test_a_cancelled_lecture_is_marked_and_does_not_block_the_time(self):
        ics = self._ics([self._lecture(cancelled=True)])
        self.assertIn("SUMMARY:❌ Cancelled: Applied Econometrics", ics)
        self.assertIn("STATUS:CANCELLED", ics)
        self.assertIn("TRANSP:TRANSPARENT", ics)

    def test_a_timed_exam_has_a_start_and_a_default_length(self):
        from unibot import study_week
        start = datetime(2026, 12, 8, 14, 0, tzinfo=study_week.TZ_BERLIN)
        ics = self._ics([{"kind": "exam", "title": "Macro; Part 1, final", "start_dt": start,
                          "end_dt": start + timedelta(minutes=90), "all_day": False}])
        self.assertIn("DTSTART:20261208T130000Z", ics)
        self.assertIn("DTEND:20261208T143000Z", ics)
        self.assertIn("SUMMARY:📝 Exam: Macro\\; Part 1\\, final", ics)  # RFC 5545 escaping

    def test_an_exam_without_a_time_is_an_all_day_event(self):
        ics = self._ics([{"kind": "exam", "title": "Statistics", "day": date(2026, 12, 9), "all_day": True}])
        self.assertIn("DTSTART;VALUE=DATE:20261209", ics)
        self.assertIn("DTEND;VALUE=DATE:20261210", ics)
        self.assertIn("The time is not set in StuMS yet", ics)

    def test_the_calendar_has_valid_structure_and_stable_unique_ids(self):
        ics = self._ics([self._lecture(), self._lecture(event_id="ev2", title="Other")])
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), ics.count("END:VEVENT"))
        uids = [line for line in ics.split("\r\n") if line.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(uids, [line for line in self._ics([self._lecture(), self._lecture(event_id="ev2", title="Other")]).split("\r\n")
                                if line.startswith("UID:")])  # the same entries give the same ids

    def test_the_feed_merges_study_blocks_lectures_and_exams_from_today(self):
        from unibot import study_week
        tz = study_week.TZ_BERLIN
        wed = date(2026, 11, 18)

        async def plan(uid, week_start, session=None):
            return [{"kind": "lunch", "mensa": "Mensa", "start_dt": datetime(2026, 11, 18, 12, 0, tzinfo=tz),
                     "end_dt": datetime(2026, 11, 18, 12, 45, tzinfo=tz)}]

        async def lectures(uid, week_start, session=None):
            return [self._lecture()] if week_start == date(2026, 11, 16) else []

        async def exams(uid, session=None):
            return [{"kind": "exam", "title": "Past exam", "day": date(2026, 10, 1), "all_day": True},
                    {"kind": "exam", "title": "Future exam", "day": date(2026, 12, 9), "all_day": True}]

        with mock.patch.object(study_week, "plan_entries", plan), \
                mock.patch.object(study_week, "lecture_entries", lectures), \
                mock.patch.object(study_week, "exam_entries", exams):
            ics = asyncio.run(study_week.calendar_for(self.UID, today=wed))
        self.assertIn("Applied Econometrics", ics)
        self.assertIn("Future exam", ics)
        self.assertNotIn("Past exam", ics)  # already over
        self.assertIn("Lunch at Mensa", ics)

    def test_a_failing_lecture_fetch_still_gives_the_rest(self):
        from unibot import study_week

        async def plan(uid, week_start, session=None):
            return []

        async def broken(uid, week_start, session=None):
            raise RuntimeError("Stud.IP down")

        async def exams(uid, session=None):
            return [{"kind": "exam", "title": "Still shown", "day": date(2026, 12, 9), "all_day": True}]

        with mock.patch.object(study_week, "plan_entries", plan), \
                mock.patch.object(study_week, "lecture_entries", broken), \
                mock.patch.object(study_week, "exam_entries", exams):
            ics = asyncio.run(study_week.calendar_for(self.UID, today=date(2026, 11, 18)))
        self.assertIn("Still shown", ics)

    def test_exam_entries_come_from_stums_are_cached_and_never_raise(self):
        from unibot import study_week
        study_week._exam_cache.pop(self.UID, None)
        stums = mock.AsyncMock(return_value=[
            {"title": "Macro", "start_datetime": "2026-12-08T14:00:00", "date": "2026-12-08"},
            {"title": "Stats", "start_datetime": None, "date": "2026-12-09"},
            {"title": "Broken", "start_datetime": "not a time", "date": None}])
        session = mock.MagicMock()
        with mock.patch.object(study_week, "get_registered_exam_schedule", stums), \
                mock.patch.object(study_week, "has_stored_login", lambda s: True), \
                mock.patch.object(study_week, "is_user_allowed", lambda uid: True):
            first = asyncio.run(study_week.exam_entries(self.UID, session=session))
            second = asyncio.run(study_week.exam_entries(self.UID, session=session))
        self.assertEqual([e["title"] for e in first], ["Macro", "Stats"])  # the broken one is skipped
        self.assertFalse(first[0]["all_day"])
        self.assertTrue(first[1]["all_day"])
        self.assertEqual(stums.await_count, 1)  # the second call came from the cache
        self.assertEqual(first, second)

    def test_a_stums_failure_gives_the_last_list_or_nothing(self):
        from unibot import study_week
        study_week._exam_cache.pop(self.UID, None)
        broken = mock.AsyncMock(side_effect=RuntimeError("StuMS paused"))
        with mock.patch.object(study_week, "get_registered_exam_schedule", broken), \
                mock.patch.object(study_week, "has_stored_login", lambda s: True), \
                mock.patch.object(study_week, "is_user_allowed", lambda uid: True):
            self.assertEqual(asyncio.run(study_week.exam_entries(self.UID, session=mock.MagicMock())), [])

    def test_the_screen_says_what_is_in_the_feed_and_warns_about_the_link(self):
        from unibot import study_week
        text, _markup = study_week.calendar_sync_view(self.UID)
        self.assertIn("lectures", text)
        self.assertIn("exams", text)
        self.assertIn("Keep the link private", text)


class SummaryButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from unibot import monitoring_alerts
        cls.alerts = monitoring_alerts
        for uid in (7841, 7842):
            activate_student(uid)

    def setUp(self):
        from unibot import tasks
        self.tasks = tasks
        tasks._summary_cache.clear()

    def _press(self, uid, data, access=True, ai=True, llm=None):
        query = mock.AsyncMock()
        query.data = data
        query.from_user = types.SimpleNamespace(id=uid)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=uid))
        llm = llm or mock.AsyncMock(return_value="The exam moves to Friday.")
        with mock.patch.object(self.tasks, "is_user_allowed", lambda u: True), \
                mock.patch.object(self.tasks, "has_course_access", lambda u, c: access), \
                mock.patch.object(self.tasks, "get_ai_personal_enabled", lambda u: ai), \
                mock.patch.object(self.tasks, "personal_ai_allowed", lambda u: ai), \
                mock.patch.object(self.tasks, "_call_openrouter", llm):
            run_handler(self.tasks.handle_summary_button, update, types.SimpleNamespace(user_data={}))
        return query, llm

    def _announcement(self, body="x" * 300):
        self.tasks.announcement_task_cache["sid1"] = {"cid": "c1", "course": "Econ", "subject": "Exam", "body": body}

    def test_a_long_announcement_gets_the_button_and_a_short_one_does_not(self):
        from unibot import monitoring_alerts
        long_post = {"cid": "c1", "course": "Econ", "thread": "T", "last_message_plain": "y" * 300}
        short_post = {"cid": "c1", "course": "Econ", "thread": "T", "last_message_plain": "short"}
        def data(p):
            return [b.callback_data for row in monitoring_alerts._forum_alert_markup(p).inline_keyboard for b in row]
        self.assertTrue(any(d.startswith("summary|f|") for d in data(long_post)))
        self.assertFalse(any(d.startswith("summary|") for d in data(short_post)))

    def test_the_summary_is_made_on_the_tap_and_shown_escaped(self):
        self._announcement()
        query, llm = self._press(7841, "summary|a|sid1", llm=mock.AsyncMock(return_value="Move <exam> & more"))
        text = query.message.reply_text.call_args.args[0]
        self.assertIn("🧾 <b>Summary</b>", text)
        self.assertIn("Move &lt;exam&gt; &amp; more", text)
        self.assertEqual(llm.await_count, 1)
        self.assertIn("Course: Econ", llm.call_args.args[1])

    def test_a_second_tap_on_the_same_message_reuses_the_summary(self):
        self._announcement()
        _q, llm1 = self._press(7841, "summary|a|sid1")
        _q, llm2 = self._press(7842, "summary|a|sid1")
        self.assertEqual(llm1.await_count + llm2.await_count, 1)

    def test_without_the_ai_permission_nothing_is_sent_to_the_ai(self):
        self._announcement()
        query, llm = self._press(7841, "summary|a|sid1", ai=False)
        llm.assert_not_awaited()
        self.assertIn("Enable AI use", query.message.reply_text.call_args.args[0])

    def test_a_student_who_does_not_follow_the_course_gets_nothing(self):
        self._announcement()
        query, llm = self._press(7841, "summary|a|sid1", access=False)
        llm.assert_not_awaited()
        self.assertIn("unavailable", query.answer.call_args.args[0])

    def test_an_expired_message_and_a_failed_ai_are_said_plainly(self):
        query, _llm = self._press(7841, "summary|a|missing")
        self.assertIn("expired", query.answer.call_args.args[0])
        self._announcement()
        query, _llm = self._press(7841, "summary|a|sid1", llm=mock.AsyncMock(return_value=None))
        self.assertIn("Couldn't summarize", query.message.reply_text.call_args.args[0])

    def test_junk_callback_data_is_ignored(self):
        query, llm = self._press(7841, "summary|x|sid1")
        llm.assert_not_awaited()
        query.message.reply_text.assert_not_awaited()


def _async(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
