"""Security fixes: Stud.IP message and announcement text cannot add markup
to an alert, a student's Mini App cookie dies with their logout or block,
the proxy refuses cross-site form posts, invite code guessing is capped and
a Stud.IP reply is always sent from the replying student's own account."""

import asyncio
import contextlib
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

import studip_login_server  # noqa: E402
from unibot import auth, monitoring  # noqa: E402

WED_DAY = __import__("datetime").date(2026, 11, 11)
EVIL = '<a href="https://evil.example/login">Re-login here</a> & <b>'


class AlertEscapingTests(unittest.TestCase):
    def test_message_alert_escapes_sender_subject_and_body(self):
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        old = [{"id": "old"}]
        new = {"id": "m1", "title": EVIL, "sender": EVIL, "date": "today", "body": EVIL}
        with mock.patch.object(monitoring, "get_user_session", return_value=object()), \
                mock.patch.object(monitoring, "_message_owner", return_value=5), \
                mock.patch.object(monitoring, "_fetch_messages_api", mock.AsyncMock(return_value=[new, old[0]])), \
                mock.patch.object(monitoring, "stored_messages", return_value=old), \
                mock.patch.object(monitoring, "save_messages"), \
                mock.patch.object(monitoring.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(asyncio.run(monitoring.check_new_messages(bot, 5, silent=True)))
        text = bot.send_message.call_args.kwargs["text"]
        self.assertNotIn("<a href", text)
        self.assertNotIn("& <b>", text)
        self.assertIn("&lt;a href=&quot;https://evil.example/login&quot;&gt;", text)


class ProxyCookieTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {7, 8}
        self.server = studip_login_server.LoginServer(
            bot_token="1:x", host="127.0.0.1", is_allowed_callback=lambda uid: uid in self.allowed)

    def _request(self, sid):
        return types.SimpleNamespace(cookies={studip_login_server.PROXY_COOKIE: sid})

    def test_blocked_student_loses_a_cached_cookie(self):
        sid = self.server._create_proxy_session(7)
        self.assertIsNotNone(self.server._check_proxy_session(self._request(sid)))
        self.allowed.discard(7)
        self.assertIsNone(self.server._check_proxy_session(self._request(sid)))

    def test_revoke_user_kills_only_that_students_cookies_and_links(self):
        sid7 = self.server._create_proxy_session(7)
        sid8 = self.server._create_proxy_session(8)
        link7 = self.server.new_link(7).rsplit("/", 1)[1]
        link8 = self.server.new_link(8).rsplit("/", 1)[1]
        self.server.revoke_user(7)
        # also the signed fallback path (the in-memory table is gone)
        self.server._proxy_sessions.clear()
        self.assertIsNone(self.server._check_proxy_session(self._request(sid7)))
        self.assertIsNotNone(self.server._check_proxy_session(self._request(sid8)))
        self.assertNotIn(link7, self.server._tokens)
        self.assertIn(link8, self.server._tokens)
        # signing in again afterwards works
        fresh = self.server._create_proxy_session(7)
        self.server._proxy_sessions.clear()
        self.assertIsNotNone(self.server._check_proxy_session(self._request(fresh)))

    def test_logout_revokes_the_students_proxy_cookies(self):
        fake = mock.MagicMock()
        with mock.patch.object(auth.state, "login_server", fake), \
                mock.patch.object(auth.state, "user_sessions", {}):
            asyncio.run(auth._perform_logout(user_id=4242))
        fake.revoke_user.assert_called_once_with(4242)
        fake.revoke_all.assert_not_called()

    def test_cross_site_post_through_the_proxy_is_refused(self):
        sid = self.server._create_proxy_session(7)
        request = mock.MagicMock()
        request.cookies = {studip_login_server.PROXY_COOKIE: sid}
        request.method = "POST"
        request.path = "/dispatch.php/messages/write"
        request.headers = {"Sec-Fetch-Site": "cross-site"}
        resolve = mock.MagicMock()
        with mock.patch.object(self.server, "_resolve_session", resolve):
            response = asyncio.run(self.server._proxy(request))
        self.assertEqual(response.status, 403)
        resolve.assert_not_called()


class InviteGuessLimitTests(unittest.TestCase):
    def setUp(self):
        auth._reset_invite_failures()
        auth._gate_replied.clear()

    def tearDown(self):
        auth._reset_invite_failures()
        auth._gate_replied.clear()

    def test_guessing_is_capped_per_account_and_window(self):
        for _ in range(auth.INVITE_MAX_FAILURES):
            self.assertFalse(auth._invite_attempts_exhausted(99))
            auth._record_invite_failure(99)
        self.assertTrue(auth._invite_attempts_exhausted(99))
        self.assertFalse(auth._invite_attempts_exhausted(100))
        later = time.time() + auth.INVITE_FAILURE_WINDOW + 1
        with mock.patch.object(auth.time, "time", return_value=later):
            self.assertFalse(auth._invite_attempts_exhausted(99))

    def test_gate_stops_answering_after_the_cap(self):
        from telegram.ext import ApplicationHandlerStop

        update = mock.MagicMock()
        update.effective_user.id = 555
        update.effective_chat.type = "private"
        update.effective_message.text = "UNI-AAA-BBB"
        update.effective_message.reply_text = mock.AsyncMock()
        update.callback_query = None
        redeem = mock.MagicMock(return_value=(False, "Invalid invite code."))
        with mock.patch.object(auth, "is_user_allowed", return_value=False), \
                mock.patch.object(auth, "redeem_invite_token", redeem):
            for _ in range(auth.INVITE_MAX_FAILURES + 3):
                with self.assertRaises(ApplicationHandlerStop):
                    asyncio.run(auth._authorization_gate(update, mock.MagicMock()))
        self.assertEqual(redeem.call_count, auth.INVITE_MAX_FAILURES)
        self.assertEqual(update.effective_message.reply_text.await_count, auth.INVITE_MAX_FAILURES)


class ReplySenderTests(unittest.TestCase):
    def test_reply_without_a_student_is_never_sent_as_the_watcher(self):
        login = mock.AsyncMock()
        with mock.patch.object(monitoring, "login_studip", login):
            with self.assertRaises(RuntimeError):
                asyncio.run(monitoring.send_studip_message("r1", "Re: x", "hi"))
        login.assert_not_called()


class MoreEscapingTests(unittest.TestCase):
    def test_dashboard_escapes_task_and_exam_text(self):
        from unibot import schedule

        tasks = [{"text": EVIL, "course": EVIL, "due_time": "2099-01-01T10:00:00+01:00"}, {"text": EVIL}]
        exams = [{"title": EVIL, "date": "2099-01-02"}]
        with mock.patch.object(schedule, "get_open_exam_registrations", mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, "get_registered_exam_schedule", mock.AsyncMock(return_value=exams)), \
                mock.patch.object(schedule, "user_tasks", return_value=tasks):
            text = asyncio.run(schedule.build_upcoming_dashboard(object(), 5))
        self.assertNotIn("<a href", text)
        self.assertEqual(text.count("&lt;a href"), 4)

    def test_exam_list_escapes_title_and_room(self):
        from unibot import monitoring_alerts

        lines = monitoring_alerts._format_exam_dates_message(
            "h", [{"title": EVIL, "room": EVIL, "exam_date_display": "x"}], show_status=False)
        self.assertNotIn("<a href", "\n".join(lines))


class VoiceRecordingTests(unittest.TestCase):
    def test_recording_is_deleted_after_a_whisper_transcript(self):
        created = []

        async def download(path):
            with open(path, "wb") as fh:
                fh.write(b"ogg")
            created.append(path)

        update = mock.MagicMock()
        update.effective_user.id = 5
        update.message.voice.file_unique_id = "unit-test-voice"
        update.message.reply_text = mock.AsyncMock(return_value=mock.MagicMock(edit_text=mock.AsyncMock(), delete=mock.AsyncMock()))
        context = mock.MagicMock()
        context.user_data = {}
        context.bot.send_chat_action = mock.AsyncMock()
        context.bot.get_file = mock.AsyncMock(return_value=mock.MagicMock(download_to_drive=download))
        with mock.patch.object(monitoring, "is_user_allowed", return_value=True), \
                mock.patch.object(monitoring, "transcribe_voice_message", mock.AsyncMock(return_value="hello")), \
                mock.patch("unibot.voice.classify_voice_intent", mock.AsyncMock(side_effect=RuntimeError("stop"))):
            with contextlib.suppress(RuntimeError):
                asyncio.run(monitoring.handle_voice_message(update, context))
        self.assertTrue(created)
        self.assertFalse(os.path.exists(created[0]))


class PerStudentSettingTests(unittest.TestCase):
    A, B, OWNER = 9301, 9302, 9300

    def setUp(self):
        from unibot import db, storage, weather
        self.db, self.storage, self.weather = db, storage, weather
        for uid in (self.A, self.B, self.OWNER):
            db.get_user_scope(uid).delete_private()
        for target in (storage, weather):
            patcher = mock.patch.object(target, "PRIMARY_ADMIN_ID", self.OWNER)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_weather_place_is_each_students_own(self):
        self.weather.save_weather_location("Bremen", "DE", 53.08, 8.8, user_id=self.A)
        self.assertEqual(self.weather.load_weather_location(self.A)["name"], "Bremen")
        self.assertIsNone(self.weather.load_weather_location(self.B))
        with auth.acting_as(self.B):
            self.assertIsNone(self.weather.load_weather_location())
        self.assertNotEqual(self.weather.snapshot_key(WED_DAY, self.A), self.weather.snapshot_key(WED_DAY, self.B))

    def test_old_shared_place_goes_to_the_bot_owner_only(self):
        self.db.save(self.weather.WEATHER_LOCATION_CACHE_PATH, {"name": "Kiel", "country": "DE",
                                                               "latitude": 54.3, "longitude": 10.1})
        self.assertIsNone(self.weather.load_weather_location(self.A))
        self.assertEqual(self.weather.load_weather_location(self.OWNER)["name"], "Kiel")
        self.assertFalse(self.db.exists(self.weather.WEATHER_LOCATION_CACHE_PATH))

    def test_default_semester_is_each_students_own(self):
        self.storage.save_default_semester("s1", "WiSe 26/27", user_id=self.A)
        self.assertEqual(self.storage.load_default_semester(self.A)["id"], "s1")
        self.assertEqual(self.storage.load_default_semester(self.B), {})
        self.storage.clear_default_semester(self.A)
        self.assertEqual(self.storage.load_default_semester(self.A), {})

    def test_background_work_uses_the_watcher_owners_semester(self):
        self.db.save(self.storage.DEFAULT_SEMESTER_CACHE_PATH, {"id": "old", "label": "SoSe 26"})
        self.assertEqual(self.storage.load_default_semester(self.B), {})
        self.assertEqual(self.storage.load_default_semester()["id"], "old")  # no acting user: the owner
        self.assertFalse(self.db.exists(self.storage.DEFAULT_SEMESTER_CACHE_PATH))


class AdminPanelTests(unittest.TestCase):
    def test_student_chosen_names_and_typed_actions_are_escaped(self):
        from unibot import system

        evil_user = {"username": None, "full_name": EVIL, "telegram_id": 1, "interactions": 3,
                     "last_active_at": time.time(), "created_at": time.time(), "action": "cmd:/<b>"}
        stats = {"total_users": 1, "active_users": 1, "dau": 1, "wau": 1, "pending_invites": 0,
                 "top_users": [evil_user], "recent_activities": [evil_user]}
        with mock.patch.object(system.db, "get_user_analytics", return_value=stats):
            text, _markup = system._build_admin_overview_view()
        self.assertNotIn("<a href", text)
        self.assertIn("cmd:/&lt;b&gt;", text)

    def test_broadcast_needs_admin_rights_when_sent(self):
        from unibot import menus

        update = mock.MagicMock()
        update.effective_user.id = 77
        update.effective_message.reply_text = mock.AsyncMock()
        context = mock.MagicMock()
        context.user_data = {"pending_step": "admin_broadcast_text"}
        broadcast = mock.AsyncMock()
        with mock.patch.object(menus, "is_user_admin", return_value=False), \
                mock.patch.object(menus, "broadcast", broadcast):
            self.assertTrue(asyncio.run(menus.handle_pending_step(update, context, text_override="hi all")))
        broadcast.assert_not_called()


class EmptyTimetableTests(unittest.TestCase):
    UID = 9401

    def test_one_empty_read_neither_alerts_nor_forgets(self):
        from unibot import schedule

        old = {"s1": {"title": "Math", "weekday": 1, "weekday_name": "Monday", "start": "10:00",
                      "end": "12:00", "location": "A1"}}
        schedule.save_personal_record(self.UID, "user_schedule_slots", old)
        schedule._timetable_checked_at.pop(self.UID, None)
        schedule._empty_timetable_reads.pop(self.UID, None)
        account = types.SimpleNamespace(user_id=self.UID, session=object())
        fan_out = mock.AsyncMock()
        with mock.patch.object(schedule, "signed_in_accounts", return_value=[account]), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[])), \
                mock.patch("unibot.course_registry.fan_out", fan_out):
            for _ in range(schedule.EMPTY_TIMETABLE_CONFIRMATIONS - 1):
                schedule._timetable_checked_at.pop(self.UID, None)
                asyncio.run(schedule.check_personal_timetable_changes(mock.MagicMock()))
            fan_out.assert_not_called()
            self.assertEqual(schedule.personal_record(self.UID, "user_schedule_slots"), old)
            schedule._timetable_checked_at.pop(self.UID, None)
            asyncio.run(schedule.check_personal_timetable_changes(mock.MagicMock()))
        fan_out.assert_not_called()  # an emptied timetable is accepted quietly
        self.assertEqual(schedule.personal_record(self.UID, "user_schedule_slots"), {})


class InviteGatePersistenceTests(unittest.TestCase):
    def setUp(self):
        auth._reset_invite_failures()
        auth._gate_replied.clear()

    def tearDown(self):
        auth._reset_invite_failures()
        auth._gate_replied.clear()

    def test_the_guess_count_survives_a_restart(self):
        for _ in range(auth.INVITE_MAX_FAILURES):
            auth._record_invite_failure(31)
        # a restart keeps only what is in the database
        self.assertTrue(auth._invite_attempts_exhausted(31))

    def test_unknown_accounts_get_the_invite_reply_once_per_interval(self):
        self.assertTrue(auth._gate_reply_due(32))
        self.assertFalse(auth._gate_reply_due(32))
        later = time.time() + auth.GATE_REPLY_INTERVAL + 1
        with mock.patch.object(auth.time, "time", return_value=later):
            self.assertTrue(auth._gate_reply_due(32))


if __name__ == "__main__":
    unittest.main()
