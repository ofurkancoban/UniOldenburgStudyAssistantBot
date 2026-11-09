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
from unibot import schedule, state, storage, study_day, study_plan  # noqa: E402

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


    def test_wechloy_menu_comes_from_the_studierendenwerk_widget(self):
        events = [_event("09:00", "11:30", "Biology", location="W02 1-148"),
                  _event("14:00", "16:00", "Ecology", location="W04 1-162")]
        plan = schedule.plan_study_day(events, WED)
        lunch = plan["lunch"]
        self.assertEqual((lunch["mensa"], lunch["menu_path"], lunch["menu_loc_id"]), ("Mensa Wechloy", None, "3"))
        titles = mock.AsyncMock(return_value=["Chili sin carne", "LIMITED-Fried vegetables", "Vegetable soup"])
        schedule._MENU_HIGHLIGHT_CACHE.clear()
        with mock.patch.object(schedule, "berlin_now", lambda: _at(WED, "08:00")), \
                mock.patch.object(schedule, "fetch_menu_titles", titles):
            menu = asyncio.run(schedule._lunch_menu_highlight(lunch, session=None))
        titles.assert_awaited_once_with("3", WED)
        self.assertEqual(menu, "Chili sin carne / Fried vegetables")

    def test_haarentor_falls_back_to_the_public_widget(self):
        events = [_event("09:00", "11:30"), _event("14:00", "16:00")]
        lunch = schedule.plan_study_day(events, WED)["lunch"]
        titles = mock.AsyncMock(return_value=["Tortelloni"])
        schedule._MENU_HIGHLIGHT_CACHE.clear()
        with mock.patch.object(schedule, "berlin_now", lambda: _at(WED, "08:00")), \
                mock.patch.object(schedule, "fetch_todays_menu_items", mock.AsyncMock(side_effect=RuntimeError("down"))), \
                mock.patch.object(schedule, "fetch_menu_titles", titles):
            menu = asyncio.run(schedule._lunch_menu_highlight(lunch, session=object()))
        titles.assert_awaited_once_with("2", WED)
        self.assertEqual(menu, "Tortelloni")

    def test_speiseplan_titles_are_parsed(self):
        from unibot.mensa import parse_speiseplan_titles
        html = ('<div class="splMeal"><span style="font-size:24px">Chili sin carne<sup>a</sup></span></div>'
                '<div class="splMeal"><span style="font-size:24px">Vegetable soup</span></div>'
                '<div class="splMeal"><span>no title</span></div>')
        self.assertEqual(parse_speiseplan_titles(html), ["Chili sin carne", "Vegetable soup"])


class CampusSwitchTests(unittest.TestCase):
    EVENTS = [_event("08:00", "10:00", "Biology", location="W14 0-001"),
              _event("10:00", "12:00", "Statistics", location="A05 1-161"),
              _event("14:00", "16:00", "Econometrics", location="A05 1-161")]

    def test_back_to_back_classes_on_different_campuses_are_flagged(self):
        switches = study_day.tight_campus_switches(self.EVENTS)
        self.assertEqual(len(switches), 1)
        s = switches[0]
        self.assertEqual((s["from_campus"], s["to_campus"], s["gap_minutes"]), (study_day.WECHLOY, study_day.HAARENTOR, 0))
        line = study_day.switch_warning(s)
        self.assertIn("10:00 W14 → 10:00 A05", line)
        self.assertIn("no time between", line)
        self.assertIn("~20 min", line)

    def test_enough_time_or_same_campus_is_fine(self):
        ok = [_event("08:00", "09:30", location="W14 0-001"), _event("10:00", "12:00", location="A05 1-161"),
              _event("12:00", "13:00", location="A06 0-009"), _event("13:10", "14:00", location="Online")]
        self.assertEqual(study_day.tight_campus_switches(ok), [])
        cancelled = [{**self.EVENTS[0], "is_cancelled": True}, self.EVENTS[1]]
        self.assertEqual(study_day.tight_campus_switches(cancelled), [])
        short = [_event("08:00", "09:50", location="W14 0-001"), _event("10:00", "12:00", location="A05 1-161")]
        self.assertIn("10 min between", study_day.switch_warning(study_day.tight_campus_switches(short)[0]))

    def test_morning_schedule_section_warns(self):
        from unibot import morning
        evs = [{**e, "time": f"{e['start_dt']:%H:%M} - {e['end_dt']:%H:%M}"} for e in self.EVENTS]
        text = morning._format_schedule_section(evs)
        self.assertIn("⚠️ 10:00 W14 → 10:00 A05", text)

    def test_daily_view_warns(self):
        sender = mock.MagicMock()
        sender.edit_message_text = mock.AsyncMock()
        with mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(schedule, "_lunch_menu_highlight", mock.AsyncMock(return_value=None)):
            evs = [{**e, "time": f"{e['start_dt']:%H:%M} - {e['end_dt']:%H:%M}"} for e in self.EVENTS]
            asyncio.run(schedule.send_daily_calendar(sender, evs, WED, WED - timedelta(days=2)))
        text = sender.edit_message_text.await_args.args[0]
        self.assertIn("W14 → 10:00 A05", text)
        self.assertIn("Campus Wechloy to Campus Haarentor", text)


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
        with mock.patch.object(study_plan, "user_tasks", lambda uid: [t for t in tasks if t.get("user_id") == uid]):
            self.assertEqual(study_plan.study_pack_course({}, self.UID, now), ("c2", "Statistics"))
        with mock.patch.object(study_plan, "user_tasks", lambda uid: []):
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
                mock.patch.object(study_plan, "user_tasks", lambda uid: [t for t in tasks if t.get("user_id") == uid]), \
                mock.patch.object(study_plan, "link_cache", cache):
            text, markup = asyncio.run(study_plan.build_study_pack(window, self.UID, now))
        self.assertIn("STUDY BLOCK STARTED", text)
        self.assertIn("<b>Econometrics</b>", text)
        self.assertIn("Deep work", text)
        self.assertIn("Problem Set 2", text)
        self.assertNotIn("someone else", text)
        *file_rows, feedback = markup.inline_keyboard
        buttons = [row[0] for row in file_rows]
        self.assertEqual([b.text for b in buttons], ["📝 Problem_Set_2.pdf", "📊 Lecture_4.pdf"])
        start_ts = int(window["start_dt"].timestamp())
        self.assertEqual([b.callback_data for b in feedback], [f"sp_block|{start_ts}|done", f"sp_block|{start_ts}|skipped"])
        sid = buttons[0].callback_data.split("|", 1)[1]
        self.assertTrue(buttons[0].callback_data.startswith("file|"))
        self.assertEqual((cache[sid]["cid"], cache[sid]["url"], cache[sid]["user_id"]), ("c1", "p2", self.UID))

    def test_pack_without_course_still_describes_the_block(self):
        window = schedule.find_study_windows_for_day([], WED)[0]
        with mock.patch.object(study_plan, "user_tasks", lambda uid: []):
            text, markup = asyncio.run(study_plan.build_study_pack(window, self.UID, window["start_dt"]))
        self.assertIn("Study block", text)
        self.assertEqual(len(markup.inline_keyboard), 1)  # just the Studied / Skipped answer
        self.assertTrue(markup.inline_keyboard[0][0].callback_data.startswith("sp_block|"))


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


    def test_lecture_reminder_warns_about_a_tight_campus_switch(self):
        events = CampusSwitchTests.EVENTS
        texts = self._run_at(_at(WED, "09:30"), events)  # Statistics at 10:00 starts in 30 minutes
        lecture = [t for t in texts if "LECTURE REMINDER" in t]
        self.assertEqual(len(lecture), 1)
        self.assertIn("Statistics", lecture[0])
        self.assertIn("Campus Wechloy to Campus Haarentor", lecture[0])
        later = self._run_at(_at(WED, "13:30"), events)  # Econometrics: no switch before it
        self.assertNotIn("⚠️", "".join(t for t in later if "LECTURE REMINDER" in t))


class TaskPrivacyTests(unittest.TestCase):
    def test_weekly_view_lists_only_the_users_own_tasks(self):
        tasks = [{"user_id": 1, "text": "mine", "due_time": "2026-11-04T10:00:00+01:00"},
                 {"user_id": 2, "text": "theirs", "due_time": "2026-11-04T11:00:00+01:00"}]
        # real per-student scopes: each list is stored and read under its own id
        for uid in (1, 2):
            storage.save_user_tasks(uid, [t for t in tasks if t["user_id"] == uid])
        own = schedule._week_tasks_for_range(date(2026, 11, 2), date(2026, 11, 8), user_id=1)
        from unibot.auth import acting_as
        with acting_as(2):
            current = schedule._week_tasks_for_range(date(2026, 11, 2), date(2026, 11, 8))
        for uid in (1, 2):
            storage.save_user_tasks(uid, [])
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
                    mock.patch.object(morning, "user_tasks", lambda uid: [t for t in tasks if t.get("user_id") == uid]), \
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


SPEISEPLAN_HTML = (
    '<div class="row splMeal"><span style="font-size:24px">Chili sin carne<div class="kennz">(2, Kn, Sl, So)</div></span>'
    '<div class="preise"><span>2,75 / 4,65 / 4,85 €</span></div><img class="iconLarge" title="vegan" src="V+.png"/></div>'
    '<div class="row splMeal"><span style="font-size:24px">Limited - Pork schnitzel<div class="kennz">(S, Gl)</div></span>'
    '<div class="preise"><span>3,90 / 5,80 / 6,10 €</span></div></div>'
)


class WechloyMenuTests(unittest.TestCase):
    def test_meals_are_parsed_with_codes_price_and_labels(self):
        from unibot.mensa import parse_speiseplan_meals
        chili, schnitzel = parse_speiseplan_meals(SPEISEPLAN_HTML)
        self.assertEqual((chili["name"], chili["allergens_text"], chili["price"], chili["labels"]),
                         ("Chili sin carne", "2, Kn, Sl, So", "2,75 €", ["vegan"]))
        self.assertEqual((schnitzel["name"], schnitzel["is_limited"]), ("Pork schnitzel", True))

    def test_menu_applies_food_preferences(self):
        from unibot import mensa
        with mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value=SPEISEPLAN_HTML)):
            text = asyncio.run(mensa.build_speiseplan_menu("3", WED, avoid_codes=["S"]))
        self.assertIn("Mensa Wechloy", text)
        self.assertIn("Chili sin carne", text)
        self.assertIn("vegan", text)
        self.assertNotIn("schnitzel", text.lower())
        self.assertIn("1 dish(es) hidden", text)

    def test_closed_or_unreachable(self):
        from unibot import mensa
        with mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value=None)):
            self.assertIn("could not be loaded", asyncio.run(mensa.build_speiseplan_menu("3", WED)))
        with mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value="<html></html>")):
            self.assertIn("No dishes found", asyncio.run(mensa.build_speiseplan_menu("3", WED)))

    def test_navigation_and_cross_links(self):
        from unibot import mensa
        rows = mensa.speiseplan_menu_keyboard("3", WED).inline_keyboard
        self.assertEqual([b.callback_data for b in rows[0]], ["mensa_loc|3|2026-11-03", "mensa_loc|3|today", "mensa_loc|3|2026-11-05"])
        callbacks = [b.callback_data for row in rows[1:] for b in row]
        self.assertEqual(callbacks, ["mensa_loc|3|2026-11-04|photos", "menu_prefs", "menu_recommend|3", "menu_nav|2/"])
        studip_rows = mensa.get_menu_navigation_keyboard("p", "n").inline_keyboard
        self.assertIn("mensa_loc|3|today", [b.callback_data for row in studip_rows for b in row])

    def test_morning_menu_button_follows_the_campus(self):
        from unibot import morning
        wechloy = [_event("09:00", "11:30", location="W02 1-148"), _event("14:00", "16:00", location="W04 1-162")]
        haarentor = [_event("09:00", "11:30"), _event("14:00", "16:00")]
        self.assertEqual(morning._menu_button_for(schedule.plan_study_day(wechloy, WED), wechloy), "mensa_loc|3|today|new")
        self.assertEqual(morning._menu_button_for(schedule.plan_study_day(haarentor, WED), haarentor), "menu_nav|2/|new")
        self.assertEqual(morning._menu_button_for({"lunch": None}, []), "menu_nav|2/|new")


class TravelModeTests(unittest.TestCase):
    UID = 5151

    def setUp(self):
        from unibot import weather
        weather._SNAPSHOTS.pop(WED, None)
        self.addCleanup(weather._SNAPSHOTS.pop, WED, None)

    def test_bike_bus_and_rain(self):
        self.assertEqual(study_day.travel_for("bike"), {"minutes": 15, "mode": "bike", "reason": None})
        self.assertEqual(study_day.travel_for("bus"), {"minutes": 20, "mode": "bus", "reason": None})
        self.assertEqual(study_day.travel_for("bike", wet=True), {"minutes": 20, "mode": "bus", "reason": "rain"})

    def test_the_students_setting_and_the_forecast_set_the_buffer(self):
        from unibot import storage, weather
        events = [_event("09:00", "10:00", location="W14 0-001"), _event("12:00", "14:00", location="A05 1-161")]
        storage.set_travel_mode(self.UID, "bike")
        bike = schedule.plan_study_day(events, WED, user_id=self.UID)["windows"][0]
        self.assertEqual((bike["start_dt"].strftime("%H:%M"), bike["travel_mode"]), ("10:15", "bike"))
        self.assertIn("🚲 15 min to Campus Haarentor first", study_day.travel_note(bike))
        weather._SNAPSHOTS[WED] = {"is_wet": True}
        rain = schedule.plan_study_day(events, WED, user_id=self.UID)["windows"][0]
        self.assertEqual(rain["start_dt"].strftime("%H:%M"), "10:20")
        self.assertEqual(study_day.travel_note(rain), "🚌 20 min to Campus Haarentor first (rain)")
        storage.set_travel_mode(self.UID, "bus")
        weather._SNAPSHOTS.pop(WED)
        self.assertEqual(schedule.user_travel(WED, self.UID)["minutes"], 20)

    def test_a_bike_makes_a_short_switch_possible_unless_it_rains(self):
        from unibot import weather
        events = [_event("08:00", "09:42", location="W14 0-001"), _event("10:00", "12:00", location="A05 1-161")]
        self.assertEqual(study_day.tight_campus_switches(events, study_day.travel_for("bike")), [])
        weather._SNAPSHOTS[WED] = {"is_wet": True}
        (switch,) = study_day.tight_campus_switches(events, study_day.travel_for("bike", wet=True))
        self.assertIn("~20 min by bus (rain)", study_day.switch_warning(switch))

    def test_settings_toggle(self):
        from unibot import menus, storage
        storage.set_travel_mode(self.UID, "bike")
        text, markup = menus._status_group_view("semester", self.UID)
        self.assertIn("Bike (15 min, bus when it rains)", text)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("status_group_toggle|semester|travel", callbacks)
        self.assertIn("cal_sync", callbacks)
        self.assertIn("buddy", callbacks)
        query = mock.MagicMock()
        query.data = "status_group_toggle|semester|travel"
        query.from_user.id = self.UID
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        with mock.patch.object(menus, "is_user_allowed", lambda uid: True):
            asyncio.run(menus.handle_status_group(update, None))
        self.assertEqual(storage.get_travel_mode(self.UID), "bus")
        self.assertIn("Bus (20 min)", query.edit_message_text.await_args.args[0])
        with self.assertRaises(ValueError):
            storage.set_travel_mode(self.UID, "car")


class CatchUpTests(unittest.TestCase):
    def test_lecture_reminder_after_a_tight_switch_offers_recording_and_slides(self):
        files = mock.AsyncMock(return_value=[
            ("Recordings", {"name": "Lecture_5.webm", "url": "v5", "modified_iso": "2026-11-03T10:00:00+01:00"}),
            (None, {"name": "Lecture_5.pdf", "url": "s5", "modified_iso": "2026-11-03T09:00:00+01:00"}),
            ("Problem Sets", {"name": "Problem_Set_3.pdf", "url": "p3", "modified_iso": "2026-11-03T08:00:00+01:00"}),
        ])
        sent = []

        async def send_message(**kwargs):
            sent.append(kwargs)
        bot = mock.MagicMock()
        bot.send_message = send_message
        _FrozenDateTime.frozen = _at(WED, "09:30")
        cache: dict = {}
        with mock.patch.object(schedule, "datetime", _FrozenDateTime), \
                mock.patch.object(schedule, "get_calendar_events", mock.AsyncMock(return_value=CampusSwitchTests.EVENTS)), \
                mock.patch.object(schedule, "get_user_session", lambda uid=None: None), \
                mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(schedule, "load_reminders_cache", lambda user_id=None: {}), \
                mock.patch.object(schedule, "save_reminders_cache", lambda data, user_id=None: None), \
                mock.patch.object(study_plan, "_list_course_files_deep", files), \
                mock.patch.object(study_plan, "link_cache", cache):
            asyncio.run(schedule.check_calendar_reminders(bot, 4242, silent=True))
        (lecture,) = [m for m in sent if "LECTURE REMINDER" in m["text"]]
        self.assertIn("Catch up on what you miss", lecture["text"])
        labels = [row[0].text for row in lecture["reply_markup"].inline_keyboard]
        self.assertEqual(labels, ["🎥 Lecture_5.webm", "📊 Lecture_5.pdf"])
        self.assertEqual({e["user_id"] for e in cache.values()}, {4242})
        files.assert_awaited_once_with("c1")

    def test_no_files_no_buttons(self):
        with mock.patch.object(study_plan, "_list_course_files_deep", mock.AsyncMock(side_effect=RuntimeError("down"))):
            self.assertIsNone(asyncio.run(study_plan.build_catch_up_buttons({"course_id": "c1"}, 1)))
        self.assertIsNone(asyncio.run(study_plan.build_catch_up_buttons({}, 1)))


class StudyBlockFeedbackTests(unittest.TestCase):
    UID = 6161

    def setUp(self):
        from unibot import storage
        cache = storage.load_general_cache()
        (cache.get("study_blocks") or {}).pop(str(self.UID), None)
        storage.save_general_cache(cache)

    def _press(self, data, markup=None, slot=None):
        query = mock.MagicMock()
        query.data = data
        query.from_user.id = self.UID
        query.answer = mock.AsyncMock()
        query.edit_message_reply_markup = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        query.message.reply_markup = markup
        query.message.text_html = "📚 <b>STUDY BLOCK STARTED</b>"
        with mock.patch.object(study_plan, "find_makeup_slot", mock.AsyncMock(return_value=slot)):
            asyncio.run(study_plan.handle_study_block_feedback(mock.MagicMock(callback_query=query), None))
        return query

    def test_answers_are_logged_and_summarised(self):
        from unibot import storage
        now = _at(WED, "18:00")
        for hour, status in ((9, "done"), (11, "skipped"), (14, None), (16, "done")):
            ts = int(_at(WED, f"{hour:02d}:00").timestamp())
            storage.record_study_block(self.UID, ts, "sent", "Econometrics")
            if status:
                self._press(f"sp_block|{ts}|{status}")
        self.assertEqual(study_plan.study_block_summary_line(self.UID, now),
                         "📊 <b>Study blocks last week:</b> 2 of 4 used (1 skipped, 1 unanswered)")
        self.assertEqual(study_plan.study_block_summary_line(999, now), "")
        self.assertEqual(storage.study_block_log(self.UID, 0, now.timestamp())[1]["course"], "Econometrics")

    def test_the_answer_replaces_the_buttons_and_keeps_the_files(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Lecture_4.pdf", callback_data="file|abc")],
                                       [InlineKeyboardButton("✅ Studied", callback_data="sp_block|1|done"),
                                        InlineKeyboardButton("⏭ Skipped", callback_data="sp_block|1|skipped")]])
        query = self._press("sp_block|1|done", markup)
        rows = query.edit_message_reply_markup.await_args.args[0].inline_keyboard
        self.assertEqual([[b.callback_data for b in r] for r in rows], [["file|abc"], ["sp_block|answered"]])
        self._press("sp_block|answered")  # tapping the answer again does nothing

    def test_repeated_skips_are_mentioned_in_the_next_pack(self):
        from unibot import storage
        for hour in (9, 11):
            ts = int(_at(WED, f"{hour:02d}:00").timestamp())
            storage.record_study_block(self.UID, ts, "sent", "Econometrics")
            storage.record_study_block(self.UID, ts, "skipped")
        events = [_event("09:00", "10:00", "Statistics", cid="c2"), _event("16:00", "18:00", "Econometrics", cid="c1")]
        window = schedule.find_study_windows_for_day(events, WED)[-2]
        with mock.patch.object(study_plan, "_list_course_files_deep", mock.AsyncMock(return_value=[])), \
                mock.patch.object(study_plan, "user_tasks", lambda uid: []), \
                mock.patch.object(study_plan.state, "user_courses_map", {self.UID: {"c1": "Econometrics"}}):
            text, _ = asyncio.run(study_plan.build_study_pack(window, self.UID, _at(WED, "14:00")))
        self.assertIn("skipped the last 2 blocks", text)

    def test_reminder_logs_the_sent_pack(self):
        from unibot import storage
        events = [_event("09:00", "10:00", "Statistics"), _event("12:00", "14:00", "Econometrics")]
        StudyReminderTests()._run_at(_at(WED, "10:04"), events)  # uses its own user 4242
        log = storage.study_block_log(4242, 0, _at(WED, "23:00").timestamp())
        self.assertEqual([b["status"] for b in log if b["ts"] == int(_at(WED, "10:00").timestamp())], ["sent"])


    def test_a_skip_offers_a_make_up_slot(self):
        slot = {"start_dt": _at(WED, "16:00"), "end_dt": _at(WED, "18:00"), "duration_minutes": 120,
                "study_spot": "BIS library, 2nd floor quiet room"}
        query = self._press("sp_block|1|skipped", slot=slot)
        text = query.edit_message_text.await_args.args[0]
        self.assertTrue(text.startswith("📚 <b>STUDY BLOCK STARTED</b>\n↪️ <b>Make-up slot:</b> Wed 16:00–18:00"))
        self.assertIn("BIS library", text)
        none = self._press("sp_block|2|skipped", slot=None)
        self.assertIn("weekly plan will make room", none.edit_message_text.await_args.args[0])
        done = self._press("sp_block|3|done", slot=slot)
        done.edit_message_text.assert_not_awaited()  # no make-up for a block that happened

    def test_make_up_slot_is_the_next_window(self):
        events = [_event("09:00", "12:00", "Statistics"), _event("13:00", "15:00", "Econometrics")]
        with mock.patch.object(study_plan, "get_user_session", lambda uid=None: None), \
                mock.patch.object(study_plan, "get_calendar_events", mock.AsyncMock(return_value=events)):
            slot = asyncio.run(study_plan.find_makeup_slot(self.UID, _at(WED, "12:10")))
        self.assertEqual(slot["start_dt"].strftime("%a %H:%M"), "Wed 15:00")
        with mock.patch.object(study_plan, "get_user_session", lambda uid=None: None), \
                mock.patch.object(study_plan, "get_calendar_events", mock.AsyncMock(return_value=events)):
            naive = asyncio.run(study_plan.find_makeup_slot(self.UID, _at(WED, "12:10").replace(tzinfo=None)))
        self.assertEqual(naive["start_dt"].strftime("%a %H:%M"), "Wed 15:00")


class TransientTelegramErrorLogTests(unittest.TestCase):
    def _record(self, exc, name="telegram.ext.Updater", level=40):
        import logging
        try:
            raise exc
        except Exception:
            import sys
            info = sys.exc_info()
        return logging.LogRecord(name, level, __file__, 1, "Exception happened while polling for updates.", None, info)

    def test_polling_network_errors_become_one_line_warnings(self):
        from telegram.error import NetworkError, TimedOut
        from unibot.config import TransientTelegramErrorFilter
        for exc in (NetworkError("httpx.ReadError: "), NetworkError("Bad Gateway"), TimedOut()):
            record = self._record(exc)
            self.assertTrue(TransientTelegramErrorFilter().filter(record))
            self.assertEqual(record.levelname, "WARNING")
            self.assertIsNone(record.exc_info)
            self.assertIn("retrying", record.getMessage())

    def test_other_errors_keep_their_traceback(self):
        from telegram.error import NetworkError
        from unibot.config import TransientTelegramErrorFilter
        for record in (self._record(ValueError("bug")), self._record(NetworkError("x"), name="unibot")):
            TransientTelegramErrorFilter().filter(record)
            self.assertEqual(record.levelname, "ERROR")
            self.assertIsNotNone(record.exc_info)


class CompanionCourseTests(unittest.TestCase):
    COURSES = {"ex": "Applied Economics - Exercise A", "lec": "Applied Economics", "ue1": "Advanced Microeconomics UE1",
               "am": "Advanced Microeconomics", "tut": "Statistics Tutorial 2", "st": "Statistics", "spat": "Spatial Econometrics"}

    def test_exercise_courses_map_to_their_lecture(self):
        self.assertEqual(study_plan.companion_courses("ex", self.COURSES), ["lec"])
        self.assertEqual(study_plan.companion_courses("ue1", self.COURSES), ["am"])
        self.assertEqual(study_plan.companion_courses("tut", self.COURSES), ["st"])
        self.assertEqual(study_plan.companion_courses("lec", self.COURSES), [])
        self.assertEqual(study_plan.companion_courses("spat", self.COURSES), [])

    def test_catch_up_falls_back_to_the_lecture_course(self):
        listings = {"ex": [], "lec": [(None, {"name": "slides_intro.pdf", "url": "s1", "modified_iso": "2026-11-03T09:00:00+01:00"})]}
        cache: dict = {}
        with mock.patch.object(study_plan, "_list_course_files_deep", mock.AsyncMock(side_effect=lambda cid: listings[cid])), \
                mock.patch.object(study_plan, "link_cache", cache), \
                mock.patch.object(study_plan.state, "user_courses_map", {7: self.COURSES}):
            markup = asyncio.run(study_plan.build_catch_up_buttons({"course_id": "ex"}, 7))
        self.assertEqual(markup.inline_keyboard[0][0].text, "📊 slides_intro.pdf")
        self.assertEqual([e["cid"] for e in cache.values()], ["lec"])  # the download goes to the course holding the file



WECHLOY_DAY_HTML = SPEISEPLAN_HTML + (
    '<div class="row splMeal"><span style="font-size:24px">Long grain rice</span>'
    '<div class="preise"><span>0,50 / 0,90 / 1,10 €</span></div></div>'
    '<div class="row splMeal"><span style="font-size:24px">Fried vegetables in vegan cream<div class="kennz">(Kn, Sl)</div></span>'
    '<div class="preise"><span>max. 2,50* / 4,00 / 4,50 €</span></div></div>'
)


class CampusLunchPickTests(unittest.TestCase):
    def test_wechloy_pick_uses_its_main_dishes_and_the_meat_floor(self):
        from unibot import mensa
        prompts = []

        async def llm(system, user, temperature=0.9):
            prompts.append(user)
            return '{"dish": "Chili sin carne", "reasoning": "Hearty and cheap."}'
        with mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value=WECHLOY_DAY_HTML)), \
                mock.patch.object(mensa, "_call_openrouter", llm), \
                mock.patch.object(mensa, "load_food_preferences", lambda: {"avoid_codes": [], "avoid_keywords": []}), \
                mock.patch.object(mensa, "login_studip", mock.AsyncMock(side_effect=AssertionError("no Stud.IP for Wechloy"))):
            from unibot import preferences
            preferences.set_pref(9901, "diet", "halal")
            preferences.set_pref(9902, "diet", "vegetarian")
            pick = asyncio.run(mensa.build_food_recommendation("3", user_id=9901))
            asyncio.run(mensa.build_food_recommendation("3", user_id=9903))  # default diet: everything
        self.assertEqual((pick["dish"], pick["category"], pick["loc_id"]), ("Chili sin carne", "Mensa Wechloy", "3"))
        self.assertIn("Fried vegetables in vegan cream", prompts[0])   # "max. 2,50*" is a meal price
        self.assertNotIn("rice", prompts[0])                           # a 0,50 side is not
        self.assertNotIn("schnitzel", prompts[0].lower())              # a halal student never gets pork
        self.assertIn("schnitzel", prompts[1].lower())                 # "everything" keeps it

    def test_morning_picks_per_campus_once_each(self):
        from unibot import morning, state
        calls = []

        async def pick(loc_id="2", user_id=None):
            calls.append(loc_id)
            return {"dish": f"dish-{loc_id}", "category": "Mensa Wechloy" if loc_id == "3" else "COUNTER ONE", "loc_id": loc_id}
        wechloy = [_event("09:00", "11:30", "Biology", location="W02 1-148", day=WED),
                   _event("14:00", "16:00", "Ecology", location="W04 1-162", day=WED)]
        haarentor = [_event("09:00", "11:30", "Statistics", day=WED), _event("14:00", "16:00", "Econometrics", day=WED)]
        own = {1: wechloy, 2: haarentor, 3: haarentor}
        sessions = {uid: mock.MagicMock(name=f"s{uid}") for uid in own}

        async def events(session=None, week_start=None, user_id=None):
            return [{**e, "time": "x"} for e in own[user_id]]
        _FrozenDateTime.frozen = _at(WED, "06:30")
        saved = state.global_session
        state.global_session = sessions[1]
        try:
            with mock.patch.object(morning, "datetime", _FrozenDateTime), \
                    mock.patch.object(morning, "get_user_session", lambda uid=None: sessions[uid]), \
                    mock.patch.object(morning, "has_stored_login", lambda s: True), \
                    mock.patch.object(morning, "get_calendar_events", events), \
                    mock.patch.object(morning, "user_tasks", lambda uid: []), \
                    mock.patch.object(morning, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "build_food_recommendation", pick), \
                    mock.patch.object(morning, "fetch_dish_photo_url", mock.AsyncMock(side_effect=lambda d, loc_id="2": f"photo-{loc_id}")), \
                    mock.patch.object(morning, "_prepare_outfits", mock.AsyncMock(return_value={})), \
                    mock.patch.object(morning, "_fetch_exam_countdown", mock.AsyncMock(return_value="")), \
                    mock.patch.object(morning, "_lunch_menu_highlight", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "_prepare_morning_voice", mock.AsyncMock(return_value=None)):
                prepared = asyncio.run(morning._prepare_morning_summary(None, [1, 2, 3]))
        finally:
            state.global_session = saved
        self.assertEqual(sorted(calls), ["2", "3"])  # one pick per mensa, shared by its students
        self.assertIn("dish-3 (Mensa Wechloy)", prepared["texts"][1])
        self.assertIn("dish-2 (Counter 1)", prepared["texts"][2])
        self.assertEqual({uid: p[0] for uid, p in prepared["photos"].items()}, {1: "photo-3", 2: "photo-2", 3: "photo-2"})
        self.assertEqual(prepared["menu_buttons"], {1: "mensa_loc|3|today|new", 2: "menu_nav|2/|new", 3: "menu_nav|2/|new"})

    def test_recommend_button_reads_the_mensa(self):
        from unibot import mensa
        query = mock.MagicMock()
        query.data = "menu_recommend|3"
        query.answer = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        build = mock.AsyncMock(return_value=None)
        with mock.patch.object(mensa, "is_user_allowed", lambda uid: True), \
                mock.patch.object(mensa, "build_food_recommendation", build), \
                mock.patch.object(mensa, "_show_typing", mock.AsyncMock()), \
                mock.patch.object(mensa, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(mensa, "_stop_loading_animation", mock.AsyncMock()):
            asyncio.run(mensa.handle_menu_recommend_button(mock.MagicMock(callback_query=query), None))
            query.data = "menu_recommend"
            asyncio.run(mensa.handle_menu_recommend_button(mock.MagicMock(callback_query=query), None))
        self.assertEqual([c.args for c in build.await_args_list], [("3",), ("2",)])

    def test_wechloy_photos_button(self):
        from unibot import mensa
        query = mock.MagicMock()
        query.data = "mensa_loc|3|2026-11-04|photos"
        query.answer = mock.AsyncMock()
        send = mock.AsyncMock()
        with mock.patch.object(mensa, "is_user_allowed", lambda uid: True), \
                mock.patch.object(mensa, "load_food_preferences", lambda: {"avoid_codes": ["S"], "avoid_keywords": []}), \
                mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value=SPEISEPLAN_HTML)), \
                mock.patch.object(mensa, "_send_menu_items_individually", send):
            asyncio.run(mensa.handle_mensa_location_button(mock.MagicMock(callback_query=query), None))
        items = send.await_args.args[1]
        self.assertEqual([i["name"] for i in items], ["Chili sin carne"])  # the pork dish stays hidden
        self.assertEqual(send.await_args.kwargs, {"skip_unmatched": True, "loc_id": "3"})



class ShortSessionTests(unittest.TestCase):
    UID = 7171

    def test_often_skipped_courses(self):
        from unibot import storage
        now = _at(WED, "20:00")
        for hour, course, status in ((9, "Econometrics", "skipped"), (10, "Econometrics", "skipped"),
                                     (11, "Econometrics", "done"), (12, "Statistics", "skipped"),
                                     (13, "Statistics", "done"), (14, "Statistics", "done"), (15, "Biology", "skipped")):
            ts = int(_at(WED, f"{hour:02d}:00").timestamp())
            storage.record_study_block(self.UID, ts, "sent", course)
            storage.record_study_block(self.UID, ts, status)
        # Econometrics 2 of 3 skipped; Statistics 1 of 3; Biology only one answer
        self.assertEqual(study_plan.often_skipped_courses(self.UID, now), {"Econometrics"})

    def test_short_courses_get_capped_blocks(self):
        start = _at(WED, "10:00")
        gaps = [{"start_dt": start, "end_dt": start + timedelta(hours=2), "date_key": WED}]
        unit = {"file": "L1.pdf", "label": "Lecture 1", "date": None,
                "tasks": [{"key": "t1", "title": "Read Lecture 1", "remaining": 120}]}
        rows = [{"cid": "c1", "name": "Econometrics", "backlog": [unit], "exam_practice": [], "upcoming": [],
                 "dated": False, "backlog_minutes": 120}]
        week = WED - timedelta(days=WED.weekday())
        with mock.patch.object(study_plan, "_split_gaps_into_blocks", lambda g: [(start, start + timedelta(hours=2))]), \
                mock.patch.object(study_plan, "_unit_label", lambda u: u["label"]):
            normal = schedule.schedule_study_sessions(gaps, rows, [], week)
            short = schedule.schedule_study_sessions(gaps, rows, [], week, {"Econometrics"})
        self.assertEqual(sum(p["minutes"] for p in normal["sessions"][0]["pieces"]), 120)
        self.assertEqual([p["minutes"] for p in short["sessions"][0]["pieces"]], [45])


class RestartLogTests(unittest.TestCase):
    def setUp(self):
        from unibot import db, diagnostics
        db.delete(diagnostics.RESTART_LOG)
        self.addCleanup(db.delete, diagnostics.RESTART_LOG)

    def test_deploys_and_unexpected_restarts(self):
        from unibot import diagnostics
        now = _at(WED, "09:00").timestamp()
        self.assertEqual(diagnostics.restart_line(now), "🔄 No restarts in the last 24 h")
        diagnostics.record_start(now - 3 * 86400, "aaa")  # older than 24 h: not counted, but the baseline
        diagnostics.record_start(now - 7200, "bbb")
        diagnostics.record_start(now - 3600, "ccc")
        self.assertEqual(diagnostics.restart_line(now), "🔄 Restarts (24 h): 2, all deploys ✅")
        diagnostics.record_start(_at(WED, "08:30").timestamp(), "ccc")
        self.assertEqual(diagnostics.restart_line(now),
                         "🔄 Restarts (24 h): 3, 2 deploys, ⚠️ 1 on the same commit (crash or manual restart at 08:30)")

    def test_current_commit_reads_the_checkout(self):
        import subprocess
        from unibot import diagnostics
        expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertEqual(diagnostics.current_commit(ROOT), expected)
        self.assertIsNone(diagnostics.current_commit(tempfile.mkdtemp()))
