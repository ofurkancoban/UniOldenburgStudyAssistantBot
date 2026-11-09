"""Study day planning: campus and travel buffer, the lunch break, the kind
of work per window, the study pack and the per-user task lists."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import schedule, state, study_day, study_plan  # noqa: E402

WED = date(2026, 11, 4)


def _at(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ_BERLIN)


def _event(start, end, title="Lecture", location="A05 1-161", cid="c1", day=WED):
    return {"date_key": day, "start_dt": _at(day, start), "end_dt": _at(day, end), "title": title,
            "location": location, "course_id": cid, "is_cancelled": False}


def _spans(entries):
    return [(e["start_dt"].strftime("%H:%M"), e["end_dt"].strftime("%H:%M")) for e in entries]


class CampusTests(unittest.TestCase):
    def test_rooms_map_to_campuses(self):
        self.assertEqual(study_day.campus_of("A05 1-161 CIP"), study_day.HAARENTOR)
        self.assertEqual(study_day.campus_of("A11 1-101 (Hörsaal B)"), study_day.HAARENTOR)
        self.assertEqual(study_day.campus_of("V03 0-C002"), study_day.HAARENTOR)
        self.assertEqual(study_day.campus_of("W14 0-001"), study_day.WECHLOY)
        for unknown in ("", None, "Online", "BigBlueButton", "Raum 12"):
            self.assertIsNone(study_day.campus_of(unknown), unknown)

    def test_work_type_by_length(self):
        kinds = {m: study_day.study_type(m)["key"] for m in (30, 59, 60, 89, 90, 180)}
        self.assertEqual(kinds, {30: "micro", 59: "micro", 60: "focus", 89: "focus", 90: "deep", 180: "deep"})
        self.assertIn("flashcards", study_day.study_type(45)["hint"].lower())
        self.assertIn("problem sets", study_day.study_type(120)["hint"].lower())


class LunchBreakTests(unittest.TestCase):
    def test_long_midday_gap_is_split_around_lunch(self):
        events = [_event("09:00", "11:30", "Statistics"), _event("14:00", "16:00", "Econometrics")]
        plan = schedule.plan_study_day(events, WED)
        lunch = plan["lunch"]
        self.assertEqual(_spans([lunch]), [("12:00", "12:45")])
        self.assertEqual(lunch["mensa"], "Mensa Uhlhornsweg")
        self.assertEqual(_spans(plan["windows"]), [("11:30", "12:00"), ("12:45", "14:00"), ("16:00", "19:00")])
        after_lunch = plan["windows"][1]
        self.assertEqual(after_lunch["duration_minutes"], 75)
        self.assertEqual(after_lunch["study_type"]["key"], "focus")
        self.assertEqual(after_lunch["study_spot"], "BIS library, 2nd floor quiet room")
        self.assertIn("after lunch", after_lunch["description"])
        self.assertEqual(after_lunch["campus"], study_day.HAARENTOR)

    def test_piece_after_lunch_keeps_the_campus_of_the_last_class(self):
        # real Wednesday: W14 08-10, A05 10-12, free afternoon -> lunch and study at Haarentor
        events = [_event("08:00", "10:00", "Biology", "W14 0-001"), _event("10:00", "12:00", "Statistics", "A05 1-161")]
        plan = schedule.plan_study_day(events, WED)
        self.assertEqual(_spans([plan["lunch"]]), [("12:00", "12:45")])
        self.assertEqual(plan["lunch"]["mensa"], "Mensa Uhlhornsweg")
        self.assertEqual([(w["campus"], w["study_spot"]) for w in plan["windows"]],
                         [(study_day.HAARENTOR, "BIS library, 2nd floor quiet room")])
        # find_study_windows_for_day is the same plan without the lunch entry
        self.assertEqual(_spans(schedule.find_study_windows_for_day(events, WED)), _spans(plan["windows"]))

    def test_short_gap_at_noon_is_used_without_cutting_a_window(self):
        # 12:00-12:50 is too short for a study window but fits the lunch break
        events = [_event("10:00", "12:00"), _event("12:50", "14:00"), _event("16:00", "17:00")]
        plan = schedule.plan_study_day(events, WED)
        self.assertEqual(_spans([plan["lunch"]]), [("12:00", "12:45")])
        self.assertEqual(_spans(plan["windows"]), [("09:00", "10:00"), ("14:00", "16:00"), ("17:00", "19:00")])

    def test_free_day_lunch_goes_into_the_midday_break(self):
        plan = schedule.plan_study_day([], WED)
        self.assertEqual(_spans([plan["lunch"]]), [("12:30", "13:15")])
        self.assertEqual(_spans(plan["windows"]), [("09:30", "12:30"), ("13:30", "16:30"), ("17:00", "19:00")])
        self.assertIsNone(plan["windows"][0]["study_spot"])  # no class: campus unknown

    def test_no_lunch_when_no_45_minutes_are_free_at_midday(self):
        events = [_event("11:00", "12:30"), _event("13:00", "15:00")]
        self.assertIsNone(schedule.plan_study_day(events, WED)["lunch"])

    def test_no_lunch_on_weekends(self):
        saturday = date(2026, 11, 7)
        events = [_event("09:00", "10:00", day=saturday)]
        self.assertIsNone(schedule.plan_study_day(events, saturday)["lunch"])

    def test_lunch_already_over_is_dropped_today(self):
        events = [_event("09:00", "11:30"), _event("14:00", "16:00")]
        now = _at(WED, "13:00")
        plan = schedule.plan_study_day(events, WED, reference_now=now)
        self.assertIsNone(plan["lunch"])
        self.assertEqual(_spans(plan["windows"]), [("13:00", "14:00"), ("16:00", "19:00")])
        self.assertTrue(plan["windows"][0]["is_remaining"])


class CampusTravelTests(unittest.TestCase):
    def test_switching_campus_takes_the_travel_time_off(self):
        events = [_event("09:00", "10:00", "Statistics", "A05 1-161"),
                  _event("13:00", "14:30", "Biology", "W14 0-001"), _event("15:00", "17:00", "Lab", "W14 0-001")]
        windows = schedule.find_study_windows_for_day(events, WED)
        gap = windows[0]
        self.assertEqual(_spans([gap]), [("10:20", "12:00")])  # then lunch 12:00-12:45
        self.assertEqual(gap["travel_minutes"], 20)
        self.assertEqual(gap["campus"], study_day.WECHLOY)
        self.assertEqual(gap["study_spot"], "BIS Wechloy branch library or the W3 tables")
        plan = schedule.plan_study_day(events, WED)
        lunch = plan["lunch"]
        self.assertEqual(_spans([lunch]), [("12:00", "12:45")])  # always 45 minutes
        self.assertEqual((lunch["mensa"], lunch["campus"]), ("Mensa Wechloy", study_day.WECHLOY))
        # the piece before lunch is already on Wechloy, where the student travelled to
        self.assertEqual([w["campus"] for w in plan["windows"]], [study_day.WECHLOY, study_day.WECHLOY])

    def test_travel_note_moves_to_the_piece_after_lunch(self):
        events = [_event("10:00", "12:00", "Statistics", "A05 1-161"), _event("14:30", "16:00", "Biology", "W14 0-001")]
        plan = schedule.plan_study_day(events, WED)
        self.assertEqual(_spans([plan["lunch"]]), [("12:20", "13:05")])  # right after the ride
        piece = [w for w in plan["windows"] if w["start_dt"] == _at(WED, "13:05")][0]
        self.assertEqual(piece["travel_minutes"], 20)

    def test_same_campus_has_no_travel(self):
        events = [_event("09:00", "10:00", location="A05 1-161"), _event("11:30", "12:00", location="A14 1-102")]
        gap = schedule.find_study_windows_for_day(events, WED)[0]
        self.assertNotIn("travel_minutes", gap)
        self.assertEqual(_spans([gap]), [("10:00", "11:30")])

    def test_gap_too_short_after_travel_is_dropped(self):
        # 60 minutes minus 20 minutes of travel leaves 40: still a micro block
        events = [_event("09:00", "10:00", location="A05 1-161"), _event("11:00", "12:00", location="W02 1-148")]
        gap = schedule.find_study_windows_for_day(events, WED)[0]
        self.assertEqual((gap["duration_minutes"], gap["study_type"]["key"]), (40, "micro"))


class StudyPlanWindowTests(unittest.TestCase):
    def test_weekly_plan_windows_skip_lunch_and_travel(self):
        monday = date(2026, 11, 2)
        events = [_event("09:00", "10:00", "Statistics", "A05 1-161", day=monday),
                  _event("15:00", "17:00", "Biology", "W14 0-001", day=monday)]
        windows = [w for w in study_plan.find_free_study_windows(events, monday) if w["date_key"] == monday]
        for w in windows:
            lunch = (_at(monday, "12:00"), _at(monday, "12:45"))
            self.assertFalse(w["start_dt"] < lunch[1] and lunch[0] < w["end_dt"], _spans([w]))
        self.assertEqual(_spans(windows)[0][0], "10:20")  # travel to Wechloy first


class MenuHighlightTests(unittest.TestCase):
    def test_main_dishes_and_dessert(self):
        items = [{"category": "Hauptgericht", "name": "Falafel Bowl"}, {"category": "Beilagen", "name": "Reis"},
                 {"category": "Dessert", "name": "Pudding"}, {"category": "Pasta", "name": "Limited - Penne Arrabiata"}]
        self.assertEqual(study_day.menu_highlight(items), "Falafel Bowl / Penne Arrabiata & dessert")
        self.assertIsNone(study_day.menu_highlight([]))

    def test_plan_lines(self):
        events = [_event("09:00", "11:30", "Statistics"), _event("14:00", "16:00", "Econometrics")]
        plan = schedule.plan_study_day(events, WED)
        lines = schedule._plan_entry_lines(plan["lunch"], menu="Falafel Bowl & dessert")
        self.assertEqual(lines, ["🥗 `12:00–12:45` Lunch at Mensa Uhlhornsweg (Falafel Bowl & dessert)"])
        deep = schedule._plan_entry_lines(plan["windows"][2])
        self.assertIn("Deep work", deep[1])
        self.assertIn("BIS library, 2nd floor quiet room", deep[1])


class StudyPackTests(unittest.TestCase):
    UID = 4242

    def setUp(self):
        self.saved = (dict(state.user_courses_map), dict(state.courses_map))
        state.user_courses_map[self.UID] = {"c1": "Econometrics", "c2": "Statistics"}
        self.addCleanup(self._restore)

    def _restore(self):
        state.user_courses_map.clear()
        state.user_courses_map.update(self.saved[0])
        state.courses_map.clear()
        state.courses_map.update(self.saved[1])

    def test_newest_problem_set_and_slides_are_picked(self):
        files = [
            (None, {"name": "Lecture_3.pdf", "url": "u3", "modified_iso": "2026-10-28T10:00:00+01:00"}),
            (None, {"name": "Lecture_4.pdf", "url": "u4", "modified_iso": "2026-11-03T10:00:00+01:00"}),
            ("Problem Sets", {"name": "Problem_Set_1.pdf", "url": "p1", "modified_iso": "2026-10-20T10:00:00+01:00"}),
            ("Problem Sets", {"name": "Problem_Set_2.pdf", "url": "p2", "modified_iso": "2026-11-02T10:00:00+01:00"}),
            (None, {"name": "data.csv", "url": "d"}),
        ]
        picked = study_plan.pick_study_pack_files(files)
        self.assertEqual([(f["name"], f["kind"]) for f in picked],
                         [("Problem_Set_2.pdf", "problem_set"), ("Lecture_4.pdf", "slides")])

    def test_course_is_the_next_class_then_the_last_then_a_deadline(self):
        now = _at(WED, "10:00")
        self.assertEqual(study_plan.study_pack_course({"before_cid": "c1", "after_cid": "c2"}, self.UID, now),
                         ("c1", "Econometrics"))
        self.assertEqual(study_plan.study_pack_course({"after_cid": "c2"}, self.UID, now), ("c2", "Statistics"))
        tasks = [{"user_id": self.UID, "text": "PS 2", "course": "Statistics",
                  "due_time": (now + timedelta(days=2)).isoformat()}]
        with mock.patch.object(study_plan, "load_tasks", lambda: tasks):
            self.assertEqual(study_plan.study_pack_course({}, self.UID, now), ("c2", "Statistics"))
        with mock.patch.object(study_plan, "load_tasks", lambda: []):
            self.assertIsNone(study_plan.study_pack_course({}, self.UID, now))

    def test_pack_message_with_deadline_and_download_buttons(self):
        events = [_event("09:00", "10:00", "Statistics", cid="c2"), _event("12:00", "14:00", "Econometrics", cid="c1")]
        window = schedule.find_study_windows_for_day(events, WED)[0]  # 10:00-12:00, before Econometrics
        now = window["start_dt"]
        tasks = [
            {"user_id": self.UID, "text": "Problem Set 2", "course": "Econometrics", "due_time": "2026-11-06T23:59:00+01:00"},
            {"user_id": 999, "text": "someone else's", "course": "Econometrics", "due_time": "2026-11-05T12:00:00+01:00"},
        ]

        async def files(cid):
            self.assertEqual(cid, "c1")
            return [("Problem Sets", {"name": "Problem_Set_2.pdf", "url": "p2", "modified_iso": "2026-11-02T10:00:00+01:00"}),
                    (None, {"name": "Lecture_4.pdf", "url": "u4", "modified_iso": "2026-11-03T10:00:00+01:00"})]

        cache: dict = {}
        with mock.patch.object(study_plan, "_list_course_files_deep", files), \
                mock.patch.object(study_plan, "load_tasks", lambda: tasks), \
                mock.patch.object(study_plan, "link_cache", cache):
            text, markup = asyncio.run(study_plan.build_study_pack(window, self.UID, now))
        self.assertIn("STUDY BLOCK STARTED", text)
        self.assertIn("<b>Econometrics</b>", text)
        self.assertIn("Deep work", text)
        self.assertIn("Problem Set 2", text)
        self.assertNotIn("someone else", text)
        buttons = [row[0] for row in markup.inline_keyboard]
        self.assertEqual([b.text for b in buttons], ["📝 Problem_Set_2.pdf", "📊 Lecture_4.pdf"])
        sid = buttons[0].callback_data.split("|", 1)[1]
        self.assertTrue(buttons[0].callback_data.startswith("file|"))
        self.assertEqual((cache[sid]["cid"], cache[sid]["url"], cache[sid]["user_id"]), ("c1", "p2", self.UID))

    def test_pack_without_course_still_describes_the_block(self):
        window = schedule.find_study_windows_for_day([], WED)[0]
        with mock.patch.object(study_plan, "load_tasks", lambda: []):
            text, markup = asyncio.run(study_plan.build_study_pack(window, self.UID, window["start_dt"]))
        self.assertIn("Study block", text)
        self.assertIsNone(markup)


class _FrozenDateTime(datetime):
    frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


class StudyReminderTests(unittest.TestCase):
    def _run_at(self, moment, events):
        sent = []
        bot = mock.MagicMock()

        async def send_message(**kwargs):
            sent.append(kwargs)
        bot.send_message = send_message
        cache: dict = {}
        _FrozenDateTime.frozen = moment

        async def fake_pack(window, user_id, now):
            return f"PACK {window['start_dt']:%H:%M} for {user_id}", None

        with mock.patch.object(schedule, "datetime", _FrozenDateTime), \
                mock.patch.object(schedule, "get_calendar_events", mock.AsyncMock(return_value=events)), \
                mock.patch.object(schedule, "get_user_session", lambda uid=None: None), \
                mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(schedule, "load_reminders_cache", lambda user_id=None: cache), \
                mock.patch.object(schedule, "save_reminders_cache", lambda data, user_id=None: None), \
                mock.patch.object(study_plan, "build_study_pack", fake_pack):
            asyncio.run(schedule.check_calendar_reminders(bot, 4242, silent=True))
            asyncio.run(schedule.check_calendar_reminders(bot, 4242, silent=True))  # the next cycle
        return [m["text"] for m in sent]

    def test_pack_is_sent_once_when_the_block_starts(self):
        events = [_event("09:00", "10:00", "Statistics"), _event("12:00", "14:00", "Econometrics")]
        texts = self._run_at(_at(WED, "10:04"), events)
        self.assertEqual([t for t in texts if t.startswith("PACK")], ["PACK 10:00 for 4242"])

    def test_reminder_before_the_block_shows_type_and_spot(self):
        events = [_event("09:00", "09:30", "Statistics"), _event("12:00", "14:00", "Econometrics")]
        texts = self._run_at(_at(WED, "09:00"), events)  # block 09:30-12:00 starts in 30 minutes
        reminder = [t for t in texts if "STUDY TIME REMINDER" in t]
        self.assertEqual(len(reminder), 1)
        self.assertIn("Deep work", reminder[0])
        self.assertIn("BIS library", reminder[0])


class TaskPrivacyTests(unittest.TestCase):
    def test_weekly_view_lists_only_the_users_own_tasks(self):
        tasks = [{"user_id": 1, "text": "mine", "due_time": "2026-11-04T10:00:00+01:00"},
                 {"user_id": 2, "text": "theirs", "due_time": "2026-11-04T11:00:00+01:00"}]
        with mock.patch.object(schedule, "load_tasks", lambda: tasks):
            own = schedule._week_tasks_for_range(date(2026, 11, 2), date(2026, 11, 8), user_id=1)
            from unibot.auth import acting_as
            with acting_as(2):
                current = schedule._week_tasks_for_range(date(2026, 11, 2), date(2026, 11, 8))
        self.assertEqual([t["text"] for t in own], ["mine"])
        self.assertEqual([t["text"] for t in current], ["theirs"])


if __name__ == "__main__":
    unittest.main()


class PerUserMorningSummaryTests(unittest.TestCase):
    def test_each_student_gets_their_own_summary_and_voice(self):
        from unibot import morning
        sessions = {1: mock.MagicMock(name="s1"), 2: mock.MagicMock(name="s2"), 3: mock.MagicMock(name="s3")}
        logged_in = {1: True, 2: True, 3: False}
        timetable = {1: [{**_event("09:00", "10:00", "Statistics"), "time": "09:00 - 10:00"}],
                     2: [{**_event("11:00", "12:00", "Biology", "W14 0-001"), "time": "11:00 - 12:00"}]}
        tasks = [{"user_id": 1, "text": "PS 1", "due_time": "2026-11-04T18:00:00+01:00"},
                 {"user_id": 2, "text": "Lab report", "due_time": "2026-11-04T18:00:00+01:00"}]
        fetched = []

        async def events(session=None, week_start=None, user_id=None):
            fetched.append(user_id)
            return timetable[user_id]

        async def voice(today_events, today_date, today_tasks, weather, food, name=None):
            return f"/tmp/voice-{[t['text'] for t in today_tasks]}"

        _FrozenDateTime.frozen = _at(WED, "06:30")
        saved = state.global_session
        state.global_session = sessions[1]
        try:
            with mock.patch.object(morning, "datetime", _FrozenDateTime), \
                    mock.patch.object(morning, "get_user_session", lambda uid=None: sessions[uid]), \
                    mock.patch.object(morning, "has_stored_login", lambda s: logged_in[next(k for k, v in sessions.items() if v is s)]), \
                    mock.patch.object(morning, "get_calendar_events", events), \
                    mock.patch.object(morning, "load_tasks", lambda: tasks), \
                    mock.patch.object(morning, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "_fetch_lunch_pick", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "_prepare_lunch_photo", mock.AsyncMock(return_value=(None, None))), \
                    mock.patch.object(morning, "_prepare_outfits", mock.AsyncMock(return_value={})), \
                    mock.patch.object(morning, "_fetch_exam_countdown", mock.AsyncMock(return_value="")), \
                    mock.patch.object(morning, "_lunch_menu_highlight", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "_prepare_morning_voice", voice):
                prepared = asyncio.run(morning._prepare_morning_summary(None, [1, 2, 3]))
        finally:
            state.global_session = saved
        texts = prepared["texts"]
        self.assertEqual(fetched, [1, 2])  # no timetable fetch without the student's own login
        self.assertIn("Statistics", texts[1])
        self.assertNotIn("Biology", texts[1])
        self.assertIn("Biology", texts[2])
        self.assertIn("PS 1", texts[1])
        self.assertNotIn("Lab report", texts[1])
        self.assertIn("No classes today", texts[3])
        self.assertEqual(prepared["voice_paths"][2], "/tmp/voice-['Lab report']")
        self.assertIn("Lunch at", texts[1])  # the study day plan is part of it
