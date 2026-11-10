"""Fixes from an outside review: null values from Open-Meteo, a repeated
tap on the privacy check, model answers with an example object before the
real one, and a longer SQLite busy timeout."""

import asyncio
import contextlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from telegram.error import BadRequest  # noqa: E402

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import ai, db, privacy, weather  # noqa: E402


class _Response:
    status = 200

    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class WeatherNullTests(unittest.TestCase):
    def _fetch(self, codes, precip):
        day = datetime.now(TZ_BERLIN).date().isoformat()
        data = {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(8, 12)],
                           "weathercode": codes, "precipitation_probability": precip},
                "daily": {"time": [day], "temperature_2m_min": [3], "temperature_2m_max": [9]}}
        session = mock.MagicMock()
        session.get = mock.MagicMock(return_value=_Response(data))

        @contextlib.asynccontextmanager
        async def shared_http():
            yield session

        weather._SNAPSHOTS.clear()
        weather._FETCHED_AT.clear()
        self.addCleanup(weather._SNAPSHOTS.clear)
        with mock.patch.object(weather, "shared_http", shared_http), \
                mock.patch.object(weather, "load_weather_location", return_value=None):
            return asyncio.run(weather.get_weather_snapshot())

    def test_null_hours_are_skipped(self):
        snap = self._fetch([3, None, 61, 2], [10, None, 20, None])
        self.assertEqual(snap["precip_chance"], 20)
        self.assertTrue(snap["is_wet"])

    def test_all_null_precipitation_reads_as_zero(self):
        snap = self._fetch([1, 2, 3, 1], [None, None, None, None])
        self.assertEqual(snap["precip_chance"], 0)

    def test_all_null_codes_give_no_snapshot_instead_of_crashing(self):
        self.assertIsNone(self._fetch([None] * 4, [None] * 4))


class PrivacyRepeatTapTests(unittest.TestCase):
    def _tap(self, error):
        query = mock.MagicMock()
        query.from_user.id = 4401
        query.data = "privacy|check"
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock(side_effect=error)
        with mock.patch.object(privacy, "is_user_allowed", return_value=True), \
                mock.patch.object(privacy, "run_manual_check", mock.AsyncMock(return_value="⏳ wait")), \
                mock.patch.object(privacy, "privacy_view", return_value=("same", None)):
            asyncio.run(privacy.handle_privacy(mock.MagicMock(callback_query=query), mock.MagicMock()))

    def test_unchanged_view_is_ignored(self):
        self._tap(BadRequest("Message is not modified: specified new message content and reply "
                             "markup are exactly the same"))

    def test_other_errors_still_surface(self):
        with self.assertRaises(BadRequest):
            self._tap(BadRequest("Message to edit not found"))


class JsonExtractionTests(unittest.TestCase):
    def test_example_object_before_the_answer(self):
        raw = 'Example: {"type": "test"} - Output: {"task": "HW1"}'
        self.assertEqual(ai._extract_json_object(raw), {"type": "test"})

    def test_fenced_block_wins(self):
        raw = 'Example: {"type": "test"}\n```json\n{"task": "HW1", "due": {"d": 1}}\n```\nDone {x}'
        self.assertEqual(ai._extract_json_object(raw), {"task": "HW1", "due": {"d": 1}})

    def test_braces_inside_strings_and_nesting(self):
        raw = 'Sure! {"title": "a } b", "items": [{"n": 1}]} hope that helps {'
        self.assertEqual(ai._extract_json_object(raw), {"title": "a } b", "items": [{"n": 1}]})

    def test_garbage_and_non_objects(self):
        self.assertIsNone(ai._extract_json_object("no json {here"))
        self.assertIsNone(ai._extract_json_object("[1, 2]"))
        self.assertIsNone(ai._extract_json_object(None))


class BusyTimeoutTests(unittest.TestCase):
    def test_connection_waits_thirty_seconds_for_a_lock(self):
        timeout = db._connection().execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 30000)


class FanOutLengthTests(unittest.TestCase):
    def test_an_oversized_alert_is_cut_to_fit(self):
        from unibot import course_registry, formatting
        sent = []

        async def send_message(**kwargs):
            sent.append(kwargs["text"])

        bot = mock.MagicMock()
        bot.send_message = send_message
        with mock.patch.object(course_registry, "delivery_format", return_value="full"), \
                mock.patch.object(course_registry, "_hold_reason", return_value=None), \
                mock.patch.object(course_registry, "get_pref", return_value="card"), \
                mock.patch.object(course_registry.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(course_registry.fan_out(bot, "c1", "exams", "<b>" + "x &amp; y " * 900 + "</b>",
                                                "t", recipients={5}))
        self.assertLessEqual(len(sent[0]), formatting.TELEGRAM_HTML_LIMIT)
        self.assertTrue(sent[0].endswith("</b>"))


class ErrorHandlerTests(unittest.TestCase):
    def _run(self, error, with_update=True):
        from telegram import CallbackQuery, Update, User
        from unibot import app
        update = Update(1, callback_query=CallbackQuery("1", User(1, "a", False), "x", data="d")) \
            if with_update else None
        context = mock.MagicMock()
        context.error = error
        answer = mock.AsyncMock()
        with mock.patch.object(CallbackQuery, "answer", answer), \
                mock.patch.object(app.logging, "error") as log_error:
            asyncio.run(app._handle_error(update, context))
        return log_error.call_count, answer

    def test_double_tap_is_silent(self):
        errors, answer = self._run(BadRequest("Message is not modified"))
        self.assertEqual(errors, 0)
        answer.assert_not_awaited()

    def test_a_real_bug_is_logged_and_the_student_told(self):
        from unibot import app
        errors, answer = self._run(KeyError("boom"))
        self.assertEqual(errors, 1)
        self.assertEqual(answer.await_args.args[0], app.HANDLER_ERROR_TEXT)

    def test_job_errors_without_an_update_are_only_logged(self):
        errors, answer = self._run(RuntimeError("job"), with_update=False)
        self.assertEqual(errors, 1)
        answer.assert_not_awaited()


class AuthHookTests(unittest.TestCase):
    def test_a_failing_hook_is_not_rerun_without_the_student(self):
        from studip_login_server import LoginServer
        calls = []

        async def hook(*args):
            calls.append(args)
            raise TypeError("bug inside the hook")

        server = LoginServer(port=1, on_session_authenticated=hook)
        asyncio.run(server._run_auth_hook(77))
        self.assertEqual(calls, [(77,)])


class _Stop(BaseException):
    pass


class WatcherWithoutAdminTests(unittest.TestCase):
    def _cycles(self, login, cycles=2, admin_session=None):
        from unibot import state, watcher
        seen = []
        steps = ["_run_scheduled_jobs", "_run_reminder_checks", "_run_exam_date_checks", "_run_health_check",
                 "_run_daily_backup", "_run_daily_digest", "_run_weekly_report", "_run_content_watchers"]

        def step(name):
            async def run(*args, **kwargs):
                seen.append((name, args[1] if name in steps[:3] else None))
            return run

        ends = []

        async def fake_sleep(seconds):
            if seconds == 180:
                ends.append(1)
                if len(ends) >= cycles:
                    raise _Stop

        patches = {name: step(name) for name in steps}
        patches.update(login_studip=login, PRIMARY_ADMIN_ID=1)
        watcher._admin_session_missing = False
        with mock.patch.multiple(watcher, **patches), \
                mock.patch.object(watcher.asyncio, "sleep", fake_sleep), \
                mock.patch.object(state, "global_session", admin_session), \
                mock.patch.object(state, "global_watcher_paused", False), \
                self.assertLogs(level="INFO") as logs:
            with self.assertRaises(_Stop):
                asyncio.run(watcher.unified_watcher_controller(mock.MagicMock()))
        return seen, [r.getMessage() for r in logs.records]

    def test_students_are_served_when_the_admin_login_fails(self):
        seen, logs = self._cycles(mock.AsyncMock(side_effect=RuntimeError("Failed to log in to Stud.IP.")))
        names = [name for name, _session in seen]
        self.assertEqual(names.count("_run_content_watchers"), 2)
        self.assertIn("_run_scheduled_jobs", names)
        self.assertTrue(all(session is None for _name, session in seen))
        self.assertEqual(sum("No admin Stud.IP session" in m for m in logs), 1)  # once, not per cycle
        self.assertFalse(any("Login error" in m for m in logs))

    def test_a_signed_out_admin_no_longer_pauses_everyone(self):
        admin = mock.MagicMock(explicitly_logged_out=True, last_interactive_activity=0.0)
        login = mock.AsyncMock()
        seen, _logs = self._cycles(login, cycles=1, admin_session=admin)
        login.assert_not_awaited()  # their logout is respected: no sign-in attempt
        self.assertIn("_run_content_watchers", [name for name, _session in seen])

    def test_the_single_account_fallback_needs_a_session(self):
        from unibot import watcher
        run = mock.AsyncMock()
        with mock.patch.multiple(watcher, _stums_users=lambda: [], _notify_reauth_needed=mock.AsyncMock(),
                                 _send_calendar_reminders=mock.AsyncMock(),
                                 check_course_session_changes=mock.AsyncMock(),
                                 check_personal_timetable_changes=mock.AsyncMock(),
                                 check_task_reminders=mock.AsyncMock(), _run_stums_checks=run), \
                mock.patch.object(watcher.course_registry, "flush_alert_queues", mock.AsyncMock()):
            asyncio.run(watcher._run_reminder_checks(mock.MagicMock(), None, datetime.now(TZ_BERLIN)))
        run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
