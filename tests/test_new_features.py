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


def _write_executable(path, text):
    import stat
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


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


class ExamRevisionPlanTests(unittest.TestCase):
    WED = date(2026, 11, 18)

    def _plan(self, exams, rows, hours=8):
        from unibot import schedule, study_plan
        tz = __import__("studip_utils").TZ_BERLIN
        start = datetime(2026, 11, 23, 9, 0, tzinfo=tz)
        end = start + timedelta(hours=hours)
        gaps = [{"start_dt": start, "end_dt": end, "date_key": start.date()}]
        with mock.patch.object(study_plan, "_split_gaps_into_blocks", lambda g: [(start, end)]), \
                mock.patch.object(study_plan, "_unit_label", lambda u: u.get("label", "unit")):
            return schedule.schedule_study_sessions(gaps, rows, exams, date(2026, 11, 23))

    def _row(self, cid="c1", name="Macro", past_paper_minutes=0):
        papers = [{"file": "p.pdf", "label": "Past exam", "date": None,
                   "tasks": [{"key": "pp", "title": "Past exam 2024", "remaining": past_paper_minutes}]}] \
            if past_paper_minutes else []
        return {"cid": cid, "name": name, "backlog": [], "exam_practice": papers, "upcoming": [], "dated": False,
                "backlog_minutes": 0}

    def test_the_closer_the_exam_the_more_revision(self):
        from unibot import schedule
        self.assertEqual([schedule.exam_revision_minutes(d) for d in (0, 3, 4, 7, 10, 14, 21, 22, -1)],
                         [240, 240, 180, 180, 120, 120, 60, 0, 0])

    def test_a_course_with_an_exam_and_no_past_papers_still_gets_revision(self):
        plan = self._plan([{"matched_cid": "c1", "days_until": 5, "title": "Macro"}], [self._row()])
        pieces = [p for s in plan["sessions"] for p in s["pieces"]]
        self.assertEqual(sum(p["minutes"] for p in pieces), 180)
        self.assertTrue(all(p["reason"] == "exam prep" for p in pieces))
        self.assertIn("5 days", pieces[0]["title"])

    def test_past_papers_count_towards_the_revision_time(self):
        plan = self._plan([{"matched_cid": "c1", "days_until": 5, "title": "Macro"}],
                          [self._row(past_paper_minutes=120)])
        pieces = [p for s in plan["sessions"] for p in s["pieces"]]
        titles = [p["title"] for p in pieces]
        self.assertEqual(sum(p["minutes"] for p in pieces), 180)  # 120 of papers + 60 of plain revision
        self.assertTrue(any("Past exam" in t for t in titles))
        self.assertTrue(any(t.startswith("Revise for the exam") for t in titles))

    def test_enough_past_papers_need_no_extra_revision(self):
        plan = self._plan([{"matched_cid": "c1", "days_until": 5, "title": "Macro"}],
                          [self._row(past_paper_minutes=240)])
        titles = [p["title"] for s in plan["sessions"] for p in s["pieces"]]
        self.assertFalse(any(t.startswith("Revise for the exam") for t in titles))

    def test_no_exam_no_revision_and_the_nearer_exam_is_scheduled_first(self):
        plan = self._plan([], [self._row()])
        self.assertEqual(plan["sessions"], [])
        rows = [self._row("c1", "Far"), self._row("c2", "Near")]
        exams = [{"matched_cid": "c1", "days_until": 20, "title": "Far"},
                 {"matched_cid": "c2", "days_until": 2, "title": "Near"}]
        plan = self._plan(exams, rows, hours=1)  # one hour: only the more urgent course fits
        first = plan["sessions"][0]["pieces"][0]
        self.assertEqual(first["course"], "Near")
        self.assertGreater(plan["stats"]["needed"]["c2"], plan["stats"]["needed"]["c1"])


class CanaryTests(unittest.TestCase):
    def setUp(self):
        from unibot import canary
        self.canary = canary
        db.delete(canary.CANARY_STATUS_PATH)
        self.addCleanup(db.delete, canary.CANARY_STATUS_PATH)

    # ── public pages ───────────────────────────────────────────────────────
    def test_a_page_with_every_marker_passes(self):
        spec = self.canary.PUBLIC_PAGE_CHECKS["StuMS start page"]
        ok, detail = self.canary.evaluate_public_page(spec, 200, "", '<form id="startPage"> javax.faces.ViewState')
        self.assertTrue(ok)

    def test_a_changed_page_names_what_is_missing(self):
        spec = self.canary.PUBLIC_PAGE_CHECKS["StuMS start page"]
        ok, detail = self.canary.evaluate_public_page(spec, 200, "", "<html>a new design</html>")
        self.assertFalse(ok)
        self.assertIn('id="startPage"', detail)
        self.assertIn("javax.faces.ViewState", detail)

    def test_a_wrong_status_is_reported(self):
        spec = self.canary.PUBLIC_PAGE_CHECKS["Stud.IP JSON API"]
        self.assertTrue(self.canary.evaluate_public_page(spec, 401, "", "")[0])
        ok, detail = self.canary.evaluate_public_page(spec, 404, "", "")
        self.assertFalse(ok)
        self.assertIn("404", detail)

    def test_the_sign_in_entry_must_still_redirect_to_the_netiq_authorisation(self):
        spec = self.canary.PUBLIC_PAGE_CHECKS["StuMS sign-in entry"]
        good = "https://login.uni-oldenburg.de/nidp/oauth/nam/authz?scope=openid"
        self.assertTrue(self.canary.evaluate_public_page(spec, 302, good, "")[0])
        ok, detail = self.canary.evaluate_public_page(spec, 302, "https://elsewhere.example/login", "")
        self.assertFalse(ok)
        self.assertIn("elsewhere.example", detail)
        self.assertFalse(self.canary.evaluate_public_page(spec, 200, "", "")[0])

    def test_unreachable_pages_and_a_changed_menu_widget_are_reported_not_raised(self):
        class Resp:
            def __init__(self, status=200, body="", headers=None):
                self.status, self._body, self.headers = status, body, headers or {}

            async def text(self, errors="strict"):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Http:
            def get(self, url, **kw):
                if "dispatch.php" in url:
                    raise OSError("proxy down")
                return Resp(200, 'nidp getToContent id="startPage" javax.faces.ViewState sso=oidc name="login_ticket"')

            def post(self, url, **kw):
                return Resp(200, "<html>nothing like the old widget</html>")

        results = asyncio.run(self.canary.check_public_pages(Http()))
        self.assertEqual(results["Stud.IP login page"], (False, "unreachable (OSError)"))
        self.assertFalse(results["Mensa menu widget"][0])
        self.assertIn("gruppenkopf", results["Mensa menu widget"][1])
        self.assertTrue(results["NetIQ login page"][0])

    # ── the account ────────────────────────────────────────────────────────
    def test_the_verifiers_accept_good_pages_and_reject_changed_ones(self):
        c = self.canary
        self.assertIsNone(c._verify_grades('<form id="examsReadonly"></form>'))
        self.assertIn("examsReadonly", c._verify_grades("<form id='other'></form>"))
        self.assertIsNone(c._verify_enrolments('<table class="belegungen"></table>'))
        self.assertIsNone(c._verify_enrolments('<form action="x?_flowId=searchOwnEnrollmentInfo-flow"></form>'))
        self.assertIn("changed", c._verify_enrolments("<html>other</html>"))
        self.assertIsNone(c._verify_study_service('<a href="start.xhtml?_flowId=studyservice-flow">'))
        self.assertIn("changed", c._verify_study_service("<html>other</html>"))

    def test_the_account_check_reports_each_page_and_a_failed_sign_in(self):
        import stums_client
        pages = {stums_client.STUDY_PLANNER_URL: "planner", stums_client.GRADES_URL: '<form id="examsReadonly"></form>',
                 stums_client.ENROLLMENT_INFO_URL: "<html>other</html>",
                 stums_client.STUDY_SERVICE_URL: "studyservice-flow"}

        async def read(session, url, referer):
            return pages[url], url

        session = mock.MagicMock()
        with mock.patch.object(stums_client, "_establish_stums_session", mock.AsyncMock(return_value="ref")), \
                mock.patch.object(stums_client, "_read_stums_page", read), \
                mock.patch.object(self.canary, "_verify_planner", lambda text: None), \
                mock.patch("unibot.auth.get_user_session", lambda uid: session):
            results = asyncio.run(self.canary.check_account(7851))
        self.assertTrue(results["StuMS sign-in"][0])
        self.assertTrue(results["Study planner"][0] and results["Results page"][0] and results["Study service page"][0])
        self.assertFalse(results["Enrolments page"][0])
        with mock.patch.object(stums_client, "_establish_stums_session",
                               mock.AsyncMock(side_effect=RuntimeError("StuMS did not sign this account in"))), \
                mock.patch("unibot.auth.get_user_session", lambda uid: session):
            failed = asyncio.run(self.canary.check_account(7851))
        self.assertEqual(list(failed), ["StuMS sign-in"])
        self.assertIn("did not sign", failed["StuMS sign-in"][1])

    def test_a_planner_page_that_no_longer_parses_is_caught(self):
        self.assertIn("changed", self.canary._verify_planner("<html>nothing</html>"))

    # ── the nightly run ────────────────────────────────────────────────────
    def _run(self, public, account, admin=7852, now=None):
        public_check = mock.AsyncMock(return_value=public)
        account_check = mock.AsyncMock(return_value=account)
        with mock.patch.object(self.canary, "check_public_pages", public_check), \
                mock.patch.object(self.canary, "check_account", account_check), \
                mock.patch.object(self.canary, "_admin_with_login", lambda: admin):
            status = asyncio.run(self.canary.run_nightly_canary(now))
        return status, account_check

    def test_a_good_night_is_stored_as_ok(self):
        status, _ = self._run({"Page": (True, "OK")}, {"StuMS sign-in": (True, "OK")})
        self.assertTrue(status["ok"])
        self.assertEqual(self.canary.canary_status()["results"]["Page"], [True, "OK"])
        self.assertEqual(self.canary.canary_health(), (True, "2 checks passed"))

    def test_a_failure_is_stored_logged_and_named_in_the_health_check(self):
        with self.assertLogs(level="WARNING") as logs:
            status, _ = self._run({"Page": (True, "OK")}, {"Results page": (False, "no form examsReadonly")})
        self.assertFalse(status["ok"])
        self.assertTrue(any("Results page" in line for line in logs.output))
        ok, detail = self.canary.canary_health()
        self.assertFalse(ok)
        self.assertIn("Results page (no form examsReadonly)", detail)

    def test_without_a_signed_in_administrator_only_the_public_pages_are_checked(self):
        status, account = self._run({"Page": (True, "OK")}, {}, admin=None)
        account.assert_not_awaited()
        self.assertTrue(status["ok"])
        self.assertIn("skipped", status["account"])
        self.assertIn("skipped", self.canary.canary_health()[1])

    def test_the_health_check_is_silent_until_the_first_run_and_flags_a_stale_one(self):
        self.assertIsNone(self.canary.canary_health())
        self._run({"Page": (True, "OK")}, {})
        self.assertTrue(self.canary.canary_health()[0])
        ok, detail = self.canary.canary_health(now_ts=time.time() + 40 * 3600)
        self.assertFalse(ok)
        self.assertIn("has not run for", detail)

    def test_it_runs_once_a_day_after_four_o_clock(self):
        tz = __import__("studip_utils").TZ_BERLIN
        ran = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(self.canary, "run_nightly_canary", ran):
            self.assertIsNone(asyncio.run(self.canary.run_canary_if_due(datetime(2026, 11, 18, 3, 30, tzinfo=tz))))
            ran.assert_not_awaited()  # too early
            asyncio.run(self.canary.run_canary_if_due(datetime(2026, 11, 18, 4, 5, tzinfo=tz)))
            self.assertEqual(ran.await_count, 1)
        db.save(self.canary.CANARY_STATUS_PATH, {"day": "2026-11-18", "at": time.time(), "results": {}, "ok": True})
        with mock.patch.object(self.canary, "run_nightly_canary", ran):
            asyncio.run(self.canary.run_canary_if_due(datetime(2026, 11, 18, 19, 0, tzinfo=tz)))
            self.assertEqual(ran.await_count, 1)  # already ran today
            asyncio.run(self.canary.run_canary_if_due(datetime(2026, 11, 19, 4, 5, tzinfo=tz)))
            self.assertEqual(ran.await_count, 2)  # the next day

    def test_an_administrator_with_a_login_is_chosen(self):
        users = [{"telegram_id": 1, "role": "user", "is_active": 1}, {"telegram_id": 2, "role": "admin", "is_active": 0},
                 {"telegram_id": 3, "role": "admin", "is_active": 1}]
        with mock.patch.object(db, "get_all_users", return_value=users), \
                mock.patch("unibot.auth.get_user_session", lambda uid: uid), \
                mock.patch("unibot.auth.has_stored_login", lambda session: session == 3):
            self.assertEqual(self.canary._admin_with_login(), 3)
        with mock.patch.object(db, "get_all_users", return_value=users), \
                mock.patch("unibot.auth.get_user_session", lambda uid: uid), \
                mock.patch("unibot.auth.has_stored_login", lambda session: False):
            self.assertIsNone(self.canary._admin_with_login())

    def test_the_bots_health_check_includes_it_only_once_it_ran(self):
        quiet = mock.AsyncMock(return_value=(True, "OK"))
        with mock.patch.object(system, "check_studip_health", quiet), \
                mock.patch.object(system, "check_whatsapp_health", quiet), \
                mock.patch.object(system, "check_weather_health", quiet), \
                mock.patch.object(system, "check_openrouter_health", quiet), \
                mock.patch.object(system, "check_gwdg_health", quiet):
            self.assertNotIn("canary", asyncio.run(system.run_health_checks()))
            self._run({"Page": (False, "changed")}, {})
            results = asyncio.run(system.run_health_checks())
        self.assertFalse(results["canary"][0])
        self.assertIn("canary", system.HEALTH_CHECK_LABELS)


class StagingScriptTests(unittest.TestCase):
    """scripts/staging.sh against a stand-in server: local folders, an ssh that runs the script locally and a
    pm2 that records what it was asked. Nothing leaves the temporary directory (its own origin, its own push)."""

    def setUp(self):
        import shutil
        import subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.origin, self.work, self.prod, self.staging = (os.path.join(t, n) for n in ("origin.git", "work", "prod", "staging"))
        self.home, self.bin = os.path.join(t, "home"), os.path.join(t, "bin")
        for d in (self.home, self.bin, os.path.join(self.home, ".pm2", "logs")):
            os.makedirs(d)
        git = lambda *a, cwd=t: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                                               env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        git("init", "-q", "--bare", "--initial-branch=trunk", self.origin)  # a remote HEAD that names no pushed branch
        git("clone", "-q", self.origin, self.work)
        os.makedirs(os.path.join(self.work, "scripts"))
        shutil.copy(os.path.join(ROOT, "scripts", "staging.sh"), os.path.join(self.work, "scripts", "staging.sh"))
        for name, text in (("requirements.txt", "aiohttp\n"), ("studip_bot.py", "print('bot')\n"),
                           (".env.example", "# TELEGRAM_TOKEN=\n# ALLOWED_USER_IDS=\n")):
            with open(os.path.join(self.work, name), "w") as fh:
                fh.write(text)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "first", cwd=self.work)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)
        git("clone", "-q", "-b", "main", self.origin, self.prod)
        with open(os.path.join(self.prod, ".env"), "w") as fh:
            fh.write("TELEGRAM_TOKEN=111111:production-secret\nUNI_PROXY_URL=socks5://127.0.0.1:40000\n")
        _write_executable(os.path.join(self.bin, "ssh"), '#!/usr/bin/env bash\nshift\nexec env "$@"\n')
        _write_executable(os.path.join(self.bin, "pm2"), f"""#!/usr/bin/env bash
echo "$*" >> "{t}/pm2_calls.txt"
LOG="$HOME/.pm2/logs/UniAssistantStaging-error.log"
case "$1" in
  describe) [ -f "{t}/started" ];;
  start|restart) touch "{t}/started"; echo "x | INFO | Unified watcher controller started successfully" >> "$LOG";;
  jlist) echo '[{{"name": "UniAssistantStaging", "pm2_env": {{"status": "online"}}}}]';;
esac
""")
        self.git = git

    def _run(self, command):
        env = {**os.environ, "HOME": self.home, "PATH": f"{self.bin}:{os.environ['PATH']}", "DEPLOY_HOST": "stand-in",
               "DEPLOY_DIR": self.prod, "STAGING_DIR": self.staging, "NODE_BIN": self.bin, "STAGING_SKIP_PIP": "1",
               "HEALTH_TIMEOUT": "5", "STAGING_SSH": os.path.join(self.bin, "ssh")}
        return self.subprocess.run(["bash", os.path.join(self.work, "scripts", "staging.sh"), command], env=env,
                                   cwd=self.work, capture_output=True, text=True, timeout=120)

    def _set_staging_token(self, token):
        with open(os.path.join(self.staging, ".env"), "a") as fh:
            fh.write(f"TELEGRAM_TOKEN={token}\n")

    def test_setup_makes_a_separate_copy_that_shares_no_secret_with_production(self):
        result = self._run("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.staging, ".env")) as fh:
            env = fh.read()
        for line in ("BOT_DB_FILE=bot_data.sqlite3", "PORT=3939", "LOGIN_PORT=8898", "CLOUDFLARE_TUNNEL=",
                     "UNI_PROXY_URL=socks5://127.0.0.1:40000"):
            self.assertIn(line, env)
        self.assertNotIn("production-secret", env)
        self.assertIn("@BotFather", result.stdout)
        self.assertEqual(oct(os.stat(os.path.join(self.staging, ".env")).st_mode & 0o777), "0o600")

    def test_setup_is_safe_to_run_again(self):
        self._run("setup")
        self._set_staging_token("222222:staging-secret")
        self.assertEqual(self._run("setup").returncode, 0)
        with open(os.path.join(self.staging, ".env")) as fh:
            self.assertIn("222222:staging-secret", fh.read())  # an existing .env is never overwritten

    def test_deploy_without_the_staging_bots_own_token_stops(self):
        self._run("setup")
        result = self._run("deploy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("own TELEGRAM_TOKEN", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pm2_calls.txt")))  # nothing was started

    def test_deploy_with_the_production_token_stops_before_anything_starts(self):
        self._run("setup")
        self._set_staging_token("111111:another-string-same-bot")  # the same bot id as production
        result = self._run("deploy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRODUCTION bot's token", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pm2_calls.txt")))

    def test_deploy_updates_the_copy_starts_it_and_a_second_deploy_restarts_it(self):
        self._run("setup")
        self._set_staging_token("222222:staging-secret")
        first = self._run("deploy")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("the staging bot runs", first.stdout)
        head = self.git("rev-parse", "--short", "HEAD", cwd=self.work).stdout.strip()
        self.assertIn(head, first.stdout)
        self.assertEqual(self.git("rev-parse", "--short", "HEAD", cwd=self.staging).stdout.strip(), head)
        with open(os.path.join(self.work, "studip_bot.py"), "a") as fh:
            fh.write("# a change to try\n")
        self.git("commit", "-q", "-am", "change", cwd=self.work)
        second = self._run("deploy")
        self.assertEqual(second.returncode, 0, second.stderr)
        with open(os.path.join(self.tmp, "pm2_calls.txt")) as fh:
            calls = fh.read()
        self.assertIn("start .venv/bin/python --name UniAssistantStaging", calls)
        self.assertIn("restart UniAssistantStaging", calls)
        self.assertEqual(self.git("rev-parse", "--short", "HEAD", cwd=self.staging).stdout.strip(),
                         self.git("rev-parse", "--short", "HEAD", cwd=self.work).stdout.strip())

    def test_status_shows_the_commit_and_whether_it_runs(self):
        self._run("setup")
        self._set_staging_token("222222:staging-secret")
        self._run("deploy")
        result = self._run("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("commit:", result.stdout)
        self.assertIn("online", result.stdout)

    def test_an_unknown_command_prints_the_usage(self):
        self.assertEqual(self._run("nonsense").returncode, 2)
        self.assertIn("scripts/staging.sh setup", self._run("help").stdout)


def _async(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
