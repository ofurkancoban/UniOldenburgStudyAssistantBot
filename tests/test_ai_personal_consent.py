"""Permission for the AI service: a student's timetable, tasks, exams, name
and weather place go to it (spoken morning summary, 🔊 Listen, spoken
questions) only after they allow it on the privacy screen. It is off by
default and kept per student."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import morning, privacy, storage, voice  # noqa: E402

ON, OFF = 9601, 9602
DAY = date(2026, 11, 11)
EVENT = {"title": "Vorlesung: Statistics", "time": "10:00 - 12:00", "location": "A1", "date_key": DAY,
         "start_dt": datetime(2026, 11, 11, 10, tzinfo=TZ_BERLIN), "end_dt": datetime(2026, 11, 11, 12, tzinfo=TZ_BERLIN)}


class ConsentStorageTests(unittest.TestCase):
    def test_off_by_default_and_per_student(self):
        storage.set_ai_personal_enabled(ON, True)
        self.addCleanup(storage.set_ai_personal_enabled, ON, False)
        self.assertTrue(storage.get_ai_personal_enabled(ON))
        self.assertFalse(storage.get_ai_personal_enabled(OFF))
        self.assertFalse(storage.get_ai_personal_enabled(None))

    def test_privacy_button_toggles_it(self):
        self.addCleanup(storage.set_ai_personal_enabled, OFF, False)
        query = mock.AsyncMock()
        query.data = "privacy|ai_personal"
        query.from_user = types.SimpleNamespace(id=OFF)
        with mock.patch.object(privacy, "is_user_allowed", return_value=True):
            asyncio.run(privacy.handle_privacy(types.SimpleNamespace(callback_query=query), None))
        self.assertTrue(storage.get_ai_personal_enabled(OFF))
        text = query.edit_message_text.call_args.args[0]
        self.assertIn("timetable, tasks and exams: ✅ Yes", text)


class ListenTests(unittest.TestCase):
    def _listen(self, uid, weekly=False):
        sender = mock.AsyncMock()
        ai = mock.AsyncMock(return_value="narration")
        tts = mock.AsyncMock(return_value=True)
        with mock.patch.object(morning, "build_ai_daily_plan_narration", ai), \
                mock.patch.object(morning, "build_ai_weekly_plan_narration", ai), \
                mock.patch.object(morning, "_try_send_voice", tts), \
                mock.patch.object(morning, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(morning, "get_ai_personal_enabled", lambda u: u == ON):
            if weekly:
                asyncio.run(morning.send_weekly_plan_voice(sender, [EVENT], DAY, user_id=uid))
            else:
                asyncio.run(morning.send_daily_plan_voice(sender, [EVENT], DAY, user_id=uid))
        return sender, ai, tts

    def test_without_permission_nothing_goes_out(self):
        for weekly in (False, True):
            sender, ai, tts = self._listen(OFF, weekly)
            ai.assert_not_awaited()
            tts.assert_not_awaited()
            text = sender.reply_text.call_args.args[0]
            self.assertIn("Statistics", text)  # the plain schedule, built locally
            self.assertIn(storage.AI_PERSONAL_OFF_NOTE, text)

    def test_with_permission_it_is_spoken(self):
        sender, ai, tts = self._listen(ON)
        ai.assert_awaited_once()
        tts.assert_awaited_once()


class MorningVoiceTests(unittest.TestCase):
    def test_spoken_summary_only_with_permission(self):
        made = []

        async def speak(*args, **kwargs):
            made.append(kwargs.get("name"))
            return "/tmp/voice.ogg"

        async def lunch_at(loc_id, uid=None):
            return None, None, None

        for uid, expected in ((ON, "/tmp/voice.ogg"), (OFF, None)):
            with mock.patch.object(morning, "get_user_session", return_value=object()), \
                    mock.patch.object(morning, "has_stored_login", return_value=False), \
                    mock.patch.object(morning, "_lunch_menu_highlight", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(morning, "_prepare_morning_voice", speak), \
                    mock.patch.object(morning, "get_ai_personal_enabled", lambda u: u == ON):
                _text, voice_path, _menu, _photo = asyncio.run(morning._prepare_user_summary(uid, DAY, None, lunch_at))
            self.assertEqual(voice_path, expected)
        self.assertEqual(len(made), 1)


class SpokenQuestionTests(unittest.TestCase):
    def _context(self, allowed):
        calendar = mock.AsyncMock(return_value=[EVENT])
        exams = mock.AsyncMock(return_value=[{"title": "Statistics exam", "date": "2026-12-01"}])
        login = mock.AsyncMock(return_value=object())
        with mock.patch.object(voice, "get_ai_personal_enabled", lambda u: allowed), \
                mock.patch.object(voice, "get_ai_grades_enabled", lambda u: False), \
                mock.patch.object(voice, "login_studip", login), \
                mock.patch.object(voice, "get_calendar_events", calendar), \
                mock.patch.object(voice, "get_registered_exam_schedule", exams), \
                mock.patch.object(voice, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(voice, "user_tasks", lambda u: [{"text": "Lab report"}]):
            return asyncio.run(voice.build_qa_context(user_id=OFF)), login, calendar

    def test_nothing_personal_without_permission(self):
        context, login, calendar = self._context(False)
        login.assert_not_awaited()
        calendar.assert_not_awaited()
        for private in ("Statistics", "Lab report"):
            self.assertNotIn(private, context)
        self.assertIn("aren't shared with the assistant", context)

    def test_everything_with_permission(self):
        context, _login, _calendar = self._context(True)
        for shared in ("Statistics", "Statistics exam", "Lab report"):
            self.assertIn(shared, context)

    def test_name_only_with_permission(self):
        for allowed, expected in ((False, None), (True, "Ada")):
            answer = mock.AsyncMock(return_value="ok")
            update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=OFF))
            message = mock.AsyncMock()
            with mock.patch.object(voice, "build_qa_context", mock.AsyncMock(return_value="data")), \
                    mock.patch.object(voice, "search_course_material", mock.AsyncMock(return_value=[])), \
                    mock.patch.object(voice, "get_user_name", lambda u: "Ada"), \
                    mock.patch.object(voice, "get_ai_personal_enabled", lambda u, allowed=allowed: allowed), \
                    mock.patch.object(voice, "answer_question_with_context", answer), \
                    mock.patch.object(voice, "send_text_and_voice", mock.AsyncMock()):
                asyncio.run(voice._voice_intent_ask_question(
                    update, types.SimpleNamespace(bot=mock.AsyncMock()), {}, "q?", "ask_question", None, [], message))
            self.assertEqual(answer.call_args.kwargs["name"], expected)


if __name__ == "__main__":
    unittest.main()
