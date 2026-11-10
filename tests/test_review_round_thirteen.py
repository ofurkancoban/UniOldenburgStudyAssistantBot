"""Fixes from the thirteenth review (schedule): a cancelled class gets no
"starting in 30 minutes" reminder, the weather heads-up goes with the first
class that is actually held, two students may fast-enroll in the same
course, one unreadable task time doesn't break the Upcoming view, a course
whose file listing failed keeps its search index, and the progress buttons
say when the student's session expired."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import menus, onboarding, schedule, settings_menu, study_plan  # noqa: E402
from unibot import preferences as prefs  # noqa: E402

UID = 9801
DAY = date(2026, 11, 4)


def _at(hm):
    h, m = map(int, hm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ_BERLIN)


def _class(title, start, cancelled=False):
    s = _at(start)
    return {"title": f"Vorlesung: {title}", "time": f"{start} - {s + timedelta(hours=2):%H:%M}", "location": "A1",
            "date_key": DAY, "start_dt": s, "end_dt": s + timedelta(hours=2), "is_cancelled": cancelled}


class _Frozen(datetime):
    frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


class CancelledClassReminderTests(unittest.TestCase):
    def _run(self, events, at):
        sent = []

        async def send_message(**kwargs):
            sent.append(kwargs["text"])

        bot = mock.MagicMock()
        bot.send_message = send_message
        _Frozen.frozen = _at(at)
        wet = {"is_wet": True, "emoji": "🌧️", "description": "rain", "temp_min": 5, "temp_max": 9,
               "precip_chance": 80, "location_name": "Oldenburg"}
        with mock.patch.object(schedule, "datetime", _Frozen), \
                mock.patch.object(schedule, "get_calendar_events", mock.AsyncMock(return_value=events)), \
                mock.patch.object(schedule, "get_user_session", lambda uid=None: None), \
                mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=wet)), \
                mock.patch.object(schedule, "load_reminders_cache", lambda user_id=None: {}), \
                mock.patch.object(schedule, "save_reminders_cache", lambda data, user_id=None: None):
            asyncio.run(schedule.check_calendar_reminders(bot, UID, silent=True))
        return [t for t in sent if "LECTURE REMINDER" in t]

    def test_no_reminder_for_a_cancelled_class(self):
        self.assertEqual(self._run([_class("Statistics", "10:00", cancelled=True)], "09:15"), [])

    def test_weather_goes_with_the_first_held_class(self):
        events = [_class("Statistics", "08:00", cancelled=True), _class("Econometrics", "10:00")]
        # wet weather widens the window: 40-50 minutes before the first held class
        (reminder,) = self._run(events, "09:15")
        self.assertIn("Econometrics", reminder)
        self.assertIn("umbrella", reminder)



class FastEnrollPerStudentTests(unittest.TestCase):
    def test_two_students_may_schedule_the_same_course(self):
        sem = "a" * 32
        saved = []

        async def run(*args):
            return None

        with mock.patch("fast_enroll.load_pending", return_value={"1": {sem: {}}}), \
                mock.patch.object(schedule, "save_pending_job", lambda *a: saved.append(a[0])), \
                mock.patch("unibot.tasks._run_fastenroll_task", run), \
                mock.patch.dict(schedule.fastenroll_tasks, {}, clear=True):
            reply = asyncio.run(schedule.schedule_fastenroll_job(2, sem, "08:00:00", None, None))
        self.assertIn("scheduled", reply)
        self.assertEqual(saved, [2])


class UpcomingDashboardTests(unittest.TestCase):
    def test_unreadable_and_naive_task_times(self):
        tasks = [{"id": "a", "text": "Broken", "due_time": "nope"},
                 {"id": "b", "text": "Hand in", "due_time": "2030-01-05T15:00:00"},
                 {"id": "c", "text": "Buy pens"}]
        with mock.patch.object(schedule, "user_tasks", lambda uid: tasks), \
                mock.patch.object(schedule, "get_open_exam_registrations", mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, "get_registered_exam_schedule", mock.AsyncMock(return_value=[
                    {"title": "Statistics", "date": "2030-01-10", "start_datetime": None}])):
            text = asyncio.run(schedule.build_upcoming_dashboard(object(), UID))
        self.assertIn("Hand in", text)
        self.assertIn("Statistics", text)
        self.assertIn("• Broken", text)  # kept as an undated to-do
        self.assertIn("• Buy pens", text)
        self.assertLess(text.index("Hand in"), text.index("Statistics"))



class CourseIndexKeptOnFailureTests(unittest.TestCase):
    def test_failed_listing_keeps_the_courses_index(self):
        index = {"files": {"c1|old.pdf": {"course": "A", "file": "old.pdf", "size": "1", "modified_ts": 1, "chunks": []},
                           "c2|gone.pdf": {"course": "B", "file": "gone.pdf", "size": "1", "modified_ts": 1, "chunks": []}}}
        saved = {}

        async def listing(cid, session=None):
            if cid == "c1":
                raise RuntimeError("Stud.IP timeout")
            return []

        with mock.patch.object(study_plan, "GWDG_API_KEY", "k"), \
                mock.patch.object(study_plan, "watched_courses", mock.AsyncMock(return_value=[("A", "c1", object()), ("B", "c2", object())])), \
                mock.patch.object(study_plan, "_list_course_files", listing), \
                mock.patch.object(study_plan, "_load_course_index_async", mock.AsyncMock(return_value=index)), \
                mock.patch.object(study_plan, "save_course_index", lambda data: saved.update(data)):
            stats = asyncio.run(study_plan.index_course_files(None))
        self.assertIn("c1|old.pdf", saved["files"])  # not read this run: kept, not re-embedded tomorrow
        self.assertNotIn("c2|gone.pdf", saved["files"])  # read and really gone
        self.assertEqual(stats["removed"], 1)


class ExpiredSessionButtonTests(unittest.TestCase):
    def test_progress_and_track_buttons_say_so(self):
        for handler, loader in ((study_plan.handle_study_progress_menu, "_load_progress_rows"),
                                (study_plan.handle_study_track_menu, "_load_track_menu_progress")):
            query = mock.AsyncMock()
            query.data = "sp_progress"
            update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=UID))
            with mock.patch.object(study_plan, loader, mock.AsyncMock(side_effect=RuntimeError("Sign in again"))):
                asyncio.run(handler(update, None))
            self.assertIn("Sign in again", query.message.reply_text.call_args.args[0])



class OutfitIsForAdminsTests(unittest.TestCase):
    def test_a_student_cannot_flip_the_outfit_switch(self):
        uid = 9802
        prefs.set_pref(uid, "outfit", True)
        with mock.patch.object(settings_menu, "is_user_admin", return_value=False):
            settings_menu.apply_action(uid, ["settings", "flip", "outfit"])
        with mock.patch.object(onboarding, "is_user_admin", return_value=False), self.assertRaises(ValueError):
            onboarding.apply_answer(uid, "outfit", "off", [])
        self.assertTrue(prefs.get_pref(uid, "outfit"))



class FilesBrowserTests(unittest.TestCase):
    def test_course_button_shows_an_ampersand_as_it_is(self):
        query = mock.AsyncMock()
        asyncio.run(menus._send_courses_menu(query, [("Research & Methods", "c1")]))
        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "📘 Research & Methods")

    def test_file_card_comes_back_after_a_failed_fresh_link(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        card_markup = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download", callback_data="file|s1")]])
        query = mock.AsyncMock()
        query.message.text_html = "<b>📁 File</b>"
        query.message.reply_markup = card_markup
        with mock.patch.dict(menus.link_cache, {"s1": {"cid": "c1", "name": "slides.pdf"}}), \
                mock.patch.object(menus, "get_user_session", lambda uid=None: object()), \
                mock.patch.object(menus, "get_fresh_file_url", mock.AsyncMock(return_value=None)), \
                mock.patch.object(menus, "_show_typing", mock.AsyncMock()), \
                mock.patch.object(menus, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(menus, "_stop_loading_animation", mock.AsyncMock()):
            asyncio.run(menus._selection_file(None, None, query, ["file", "s1"], UID))
        last = query.edit_message_text.call_args
        self.assertEqual(last.args[0], "<b>📁 File</b>")
        self.assertIs(last.kwargs["reply_markup"], card_markup)


if __name__ == "__main__":
    unittest.main()
