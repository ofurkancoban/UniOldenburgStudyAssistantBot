"""The study plan's inputs: the current semester is the one running today
(Stud.IP lists future terms first), held lectures are counted for the
course each event belongs to, and cancelled classes neither count as held
nor block study time."""

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
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

import fast_enroll  # noqa: E402
from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import monitoring, state, studip_api, study_plan  # noqa: E402

OPTIONS = [("s29", "Sommersemester 2029"), ("w28", "Wintersemester 2028/2029"),
           ("w26", "Wintersemester 2026/2027"), ("s26", "Sommersemester 2026"), ("w25", "Wintersemester 2025/2026")]


class CurrentSemesterTests(unittest.TestCase):
    def test_the_running_term_not_the_newest(self):
        self.assertEqual(fast_enroll.current_semester_id(OPTIONS, date(2026, 11, 10)), "w26")
        self.assertEqual(fast_enroll.current_semester_id(OPTIONS, date(2027, 2, 15)), "w26")
        self.assertEqual(fast_enroll.current_semester_id(OPTIONS, date(2026, 4, 1)), "s26")
        self.assertEqual(fast_enroll.current_semester_id(OPTIONS, date(2026, 9, 30)), "s26")

    def test_unreadable_labels_fall_back_to_the_first(self):
        self.assertEqual(fast_enroll.current_semester_id([("a", "Term A"), ("b", "Term B")], date(2026, 11, 10)), "a")
        self.assertIsNone(fast_enroll.current_semester_id([], date(2026, 11, 10)))

    def test_label_forms(self):
        self.assertEqual(fast_enroll.semester_start("WiSe 2026/27"), date(2026, 10, 1))
        self.assertEqual(fast_enroll.semester_start("SoSe 26"), date(2026, 4, 1))
        self.assertIsNone(fast_enroll.semester_start("Business 2026"))

    def test_english_labels(self):
        self.assertEqual(fast_enroll.semester_start("Summer semester 2026"), date(2026, 4, 1))
        self.assertEqual(fast_enroll.semester_start("Winter semester 2026/27"), date(2026, 10, 1))
        options = [("s27", "Summer semester 2027"), ("w26", "Winter semester 2026/27"),
                   ("s26", "Summer semester 2026"), ("w25", "Winter semester 2025/26")]
        self.assertEqual(fast_enroll.current_semester_id(options, date(2026, 11, 10)), "w26")
        self.assertEqual(fast_enroll.current_semester_id(options, date(2026, 7, 1)), "s26")
        self.assertEqual(fast_enroll.semester_display("Summer semester 2026"), "☀️ Summer semester 2026")
        self.assertEqual(fast_enroll.semester_display("Winter semester 2026/27"), "❄️ Winter semester 2026/27")

    def test_watched_map_uses_the_running_term(self):
        session = mock.MagicMock(user_id=77)
        listed = []

        async def list_courses(semester_id=None, session=None, user_id=None):
            listed.append(semester_id)
            return [("Statistics", "c1")]

        with mock.patch.object(studip_api, "get_semester_options", mock.AsyncMock(return_value=OPTIONS)), \
                mock.patch.object(studip_api, "load_default_semester", return_value={}), \
                mock.patch.object(studip_api, "get_pref", return_value=False), \
                mock.patch.object(studip_api, "list_courses", list_courses), \
                mock.patch.object(studip_api, "current_semester_id", lambda opts: fast_enroll.current_semester_id(opts, date(2026, 11, 10))), \
                mock.patch.object(state, "courses_map", {}):
            asyncio.run(studip_api._ensure_watched_courses_map(session))
        self.assertEqual(listed, ["w26"])

    def test_previous_semester_is_the_one_before_today(self):
        listed = []

        async def list_api(semester_id, session=None, user_id=None):
            listed.append(semester_id)
            return []

        monitoring._prev_semester_courses.pop(78, None)
        with mock.patch.object(monitoring, "get_semester_options", mock.AsyncMock(return_value=OPTIONS)), \
                mock.patch.object(monitoring, "load_default_semester", return_value={}), \
                mock.patch.object(monitoring, "_list_courses_api", list_api), \
                mock.patch.object(monitoring, "current_semester_id", lambda opts: fast_enroll.current_semester_id(opts, date(2026, 11, 10))):
            asyncio.run(monitoring._previous_semester_courses(78, mock.MagicMock()))
        monitoring._prev_semester_courses.pop(78, None)
        self.assertEqual(listed, ["s26"])


def _event(start, hours, title, cid, cancelled=False):
    return {"title": title, "course_id": cid, "start_dt": start, "end_dt": start + timedelta(hours=hours),
            "date_key": start.date(), "is_cancelled": cancelled}


class HeldLectureTests(unittest.TestCase):
    def test_counted_by_course_and_cancelled_skipped(self):
        past = datetime.now(TZ_BERLIN) - timedelta(days=3)
        events = [
            _event(past, 2, "Vorlesung: 2.01.100 Macroeconomics I", "macro1"),
            # the title is closer to course one, but the event belongs to course two
            _event(past - timedelta(days=7), 2, "Vorlesung: 2.01.101 Macroeconomics II", "macro2"),
            _event(past - timedelta(days=14), 2, "Vorlesung: 2.01.100 Macroeconomics I", "macro1", cancelled=True),
            _event(past - timedelta(days=21), 2, "Übung: 2.01.100 Macroeconomics I", "macro1"),
        ]
        candidates = [("Macroeconomics I", "macro1"), ("Macroeconomics II", "macro2")]
        with mock.patch.object(study_plan, "get_calendar_events", mock.AsyncMock(return_value=events)):
            held = asyncio.run(study_plan.count_lectures_held(None, candidates))
        self.assertEqual(held, {"macro1": 1, "macro2": 1})

    def test_unknown_course_falls_back_to_the_title(self):
        past = datetime.now(TZ_BERLIN) - timedelta(days=3)
        events = [_event(past, 2, "Vorlesung: 2.01.100 Statistics", None)]
        with mock.patch.object(study_plan, "get_calendar_events", mock.AsyncMock(return_value=events)):
            held = asyncio.run(study_plan.count_lectures_held(None, [("Statistics", "stat")]))
        self.assertEqual(held, {"stat": 1})


class CancelledClassWindowTests(unittest.TestCase):
    def test_cancelled_class_is_free_time(self):
        monday = date(2026, 11, 16)
        start = datetime(2026, 11, 16, 12, 0, tzinfo=TZ_BERLIN)
        busy = [_event(start, 4, "Vorlesung: Statistics", "stat")]
        free = [_event(start, 4, "Vorlesung: Statistics", "stat", cancelled=True)]

        def minutes(events):
            return sum(w["duration_minutes"] for w in study_plan.find_free_study_windows(events, monday)
                       if w["date_key"] == monday)

        self.assertGreater(minutes(free), minutes(busy))


if __name__ == "__main__":
    unittest.main()
