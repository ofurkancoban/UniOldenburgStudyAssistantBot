"""Fixes from the eleventh review (exams, morning, voice): students can't
post into the administrator's WhatsApp group from an exam notice, one
student's failure no longer cancels the morning summary of everyone at
the same time, exam notices store a button action only when sent, a voice
reminder can't be set in the past, and the course-file exam scan is gone."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402

import exam_reminder  # noqa: E402
from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import app as botapp, db, exams, morning, state, storage, telegram_ui, voice  # noqa: E402

STUDENT, ADMIN = 9501, 9502


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


class ExamWhatsAppTests(unittest.TestCase):
    MARKUP = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Register", callback_data="exam_ask|s1")],
        [InlineKeyboardButton("📲 Forward to WA", callback_data="examopen_wa_ask|s1")],
        [InlineKeyboardButton("🔕 Don't remind me again", callback_data="examopen_mute_ask|s1")],
    ])

    def test_button_hidden_from_students_kept_for_admins(self):
        with mock.patch.object(telegram_ui, "is_admin_chat", lambda uid: uid == ADMIN):
            self.assertEqual(_datas(telegram_ui.markup_for(STUDENT, self.MARKUP)),
                             ["exam_ask|s1", "examopen_mute_ask|s1"])
            self.assertIn("examopen_wa_ask|s1", _datas(telegram_ui.markup_for(ADMIN, self.MARKUP)))

    def test_students_cannot_forward_even_with_an_old_button(self):
        send = mock.AsyncMock(return_value=(True, "sent"))
        for handler, data in ((exams.handle_examopen_wa_ask, "examopen_wa_ask|s1"),
                              (exams.handle_examopen_wa_confirm, "examopen_wa_confirm|s1")):
            query = mock.AsyncMock()
            query.data = data
            query.from_user = types.SimpleNamespace(id=STUDENT)
            with mock.patch.object(exams, "is_user_admin", return_value=False), \
                    mock.patch.object(exams, "_send_text_to_whatsapp", send):
                asyncio.run(handler(types.SimpleNamespace(callback_query=query), None))
            query.answer.assert_awaited_once()
            self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
            query.message.reply_text.assert_not_awaited()
        send.assert_not_awaited()


class MorningIsolationTests(unittest.TestCase):
    def test_one_failing_student_leaves_the_others(self):
        async def prepare(uid, today, weather, lunch_at):
            if uid == 1:
                raise RuntimeError("broken timetable")
            return f"text {uid}", None, "menu_nav|2/|new", (None, None)

        with mock.patch.object(state, "global_session", object()), \
                mock.patch.object(morning, "get_weather_snapshot", mock.AsyncMock(return_value=None)), \
                mock.patch.object(morning, "_prepare_user_summary", prepare), \
                mock.patch.object(morning, "_prepare_outfits", mock.AsyncMock(return_value={})):
            data = asyncio.run(morning._prepare_morning_summary(mock.MagicMock(), [1, 2]))
        self.assertEqual(data["texts"], {2: "text 2"})
        self.assertNotIn(1, data["menu_buttons"])


class ExamNoticeButtonTests(unittest.TestCase):
    def _run(self, notified_open):
        exam = {"code": "inf100", "start_date": "2026-11-01", "end_date": None, "unit_id": "u1",
                "title": "Algorithms", "raw": "window"}
        cache = {"notified_open": notified_open}
        actions: dict = {}
        sent = []

        async def broadcast(bot, text, reply_markup=None):
            sent.append(reply_markup)

        with mock.patch.object(exam_reminder, "get_open_exam_registrations", mock.AsyncMock(return_value=[exam])), \
                mock.patch.object(exam_reminder, "load_exam_cache", lambda *a, **k: cache), \
                mock.patch.object(exam_reminder, "save_exam_cache", lambda *a, **k: None):
            asyncio.run(exam_reminder.check_exam_reminders(types.SimpleNamespace(user_id=STUDENT), None,
                                                           broadcast, actions))
        return actions, sent

    def test_no_action_stored_when_nothing_is_sent(self):
        actions, sent = self._run({"inf100:2026-11-01:None": "2026-11-01T10:00"})
        self.assertEqual(sent, [])
        self.assertEqual(actions, {})

    def test_sent_notice_gets_one_working_action(self):
        actions, sent = self._run({})
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(actions), 1)
        sid = next(iter(actions))
        self.assertIn(f"exam_ask|{sid}", _datas(sent[0]))
        self.assertEqual(actions[sid]["user_id"], STUDENT)


class VoiceExamReminderTests(unittest.TestCase):
    def _speak(self, exam_in, before_days):
        exam_dt = (datetime.now(TZ_BERLIN) + exam_in).replace(tzinfo=None)
        message = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=STUDENT))
        context = types.SimpleNamespace(bot=mock.AsyncMock())
        added = []
        with mock.patch.object(voice, "extract_exam_reminder_task", mock.AsyncMock(return_value={
                    "course_query": "Algorithms", "offset_value": before_days, "offset_unit": "days"})), \
                mock.patch.object(voice, "login_studip", mock.AsyncMock()), \
                mock.patch.object(voice, "_find_exam_datetime", mock.AsyncMock(return_value=("Algorithms", exam_dt, True))), \
                mock.patch.object(voice, "add_user_task", lambda uid, task: added.append(task)):
            asyncio.run(voice._voice_intent_create_exam_reminder_task(
                update, context, {}, "remind me", "create_exam_reminder_task", None, [], message))
        return added, message.reply_text.call_args.args[0]

    def test_reminder_in_the_past_is_refused(self):
        added, reply = self._speak(timedelta(days=3), 10)
        self.assertEqual(added, [])
        self.assertIn("already in the past", reply)

    def test_reminder_ahead_is_set(self):
        added, reply = self._speak(timedelta(days=10), 3)
        self.assertEqual(len(added), 1)
        self.assertIn("Reminder set", reply)


class CourseFileExamScanRemovedTests(unittest.TestCase):
    def test_feature_is_gone(self):
        for name in ("scan_course_files_for_exams", "get_file_exam_events", "check_file_exam_reminders",
                     "notify_new_file_exam_events", "handle_exam_files_scan"):
            self.assertFalse(hasattr(exams, name), name)

    def test_no_scan_button_handler(self):
        from telegram.ext import Application, CallbackQueryHandler
        app = Application.builder().token("123:TEST").build()
        botapp.register_handlers(app)
        patterns = [h.pattern.pattern for group in app.handlers.values() for h in group
                    if isinstance(h, CallbackQueryHandler) and h.pattern is not None]
        self.assertFalse(any("exam_files_scan" in p for p in patterns))

    def test_retired_data_is_dropped(self):
        db.save(exams.RETIRED_EXAM_FILE_CACHE_PATH, {"files": {"c1": {}}})
        storage.save_personal_record(STUDENT, exams.RETIRED_STUMS_SITTINGS_RECORD, {"c1": [{"date": "2026-12-03"}]})
        exams.drop_retired_exam_file_data([STUDENT])
        self.assertFalse(db.exists(exams.RETIRED_EXAM_FILE_CACHE_PATH))
        self.assertFalse(storage.personal_record(STUDENT, exams.RETIRED_STUMS_SITTINGS_RECORD))


if __name__ == "__main__":
    unittest.main()
