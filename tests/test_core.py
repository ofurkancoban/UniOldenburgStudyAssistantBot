"""Regression tests for the pure-logic parts of the bot.

Run from the project root with the bot's virtualenv:
    .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import date, datetime
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Keep test output out of the bot's live watch_log.txt (importing unibot
# attaches its file handler to the root logger).
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
# Tests must never touch the real bot database
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

import studip_login_server  # noqa: E402
import studip_oauth  # noqa: E402
import studip_utils  # noqa: E402
import studip_vault  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")


def _event(day: date, start: str, end: str, title: str = "Course", **extra) -> dict:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    ev = {
        "date_key": day,
        "start_dt": datetime(day.year, day.month, day.day, sh, sm, tzinfo=TZ),
        "end_dt": datetime(day.year, day.month, day.day, eh, em, tzinfo=TZ),
        "title": title,
    }
    ev.update(extra)
    return ev


class BotModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        # importing the package runs unibot.config, which anchors CWD to the project
        from unibot import auth, schedule
        os.chdir(cwd)
        cls.auth = auth
        cls.schedule = schedule

    def test_time_module_not_shadowed(self):
        # Regression: `from datetime import time` once replaced the stdlib
        # module and broke every time.time()/time.monotonic() call.
        import importlib
        import unibot
        for name in unibot.MODULE_ORDER:
            module = importlib.import_module(f"unibot.{name}")
            t = getattr(module, "time", None)
            if t is not None:
                self.assertTrue(callable(getattr(t, "time", None)), name)
                self.assertTrue(callable(getattr(t, "monotonic", None)), name)

    def test_allowlist_fails_closed(self):
        with mock.patch.object(self.auth, "ALLOWED_USER_IDS", set()):
            self.assertFalse(self.auth.is_user_allowed(123))
        with mock.patch.object(self.auth, "ALLOWED_USER_IDS", {123}):
            self.assertTrue(self.auth.is_user_allowed(123))
            self.assertFalse(self.auth.is_user_allowed(456))
            self.assertFalse(self.auth.is_user_allowed(None))

    def test_escaping_helpers(self):
        self.assertEqual(studip_utils.html_escape("Theory & <Practice>"), "Theory &amp; &lt;Practice&gt;")
        self.assertEqual(studip_utils.md_escape("Info_1 *x*"), "Info\\_1 \\*x\\*")

    def test_study_windows_regular_day(self):
        day = date(2026, 11, 4)  # Wednesday
        events = [
            _event(day, "12:00", "14:00", "Lecture A"),
            _event(day, "15:30", "17:00", "Lecture B"),
        ]
        windows = self.schedule.find_study_windows_for_day(events, day)
        spans = [(w["start_dt"].strftime("%H:%M"), w["end_dt"].strftime("%H:%M")) for w in windows]
        # before (3h), between (1.5h); after 17:00-19:00 is 2h
        self.assertEqual(spans, [("09:00", "12:00"), ("14:00", "15:30"), ("17:00", "19:00")])

    def test_study_windows_free_weekday_and_weekend(self):
        wed = date(2026, 11, 4)
        free = self.schedule.find_study_windows_for_day([], wed)
        self.assertEqual(len(free), 3)
        spans = [(w["start_dt"].strftime("%H:%M"), w["end_dt"].strftime("%H:%M")) for w in free]
        self.assertEqual(spans, [("09:30", "12:30"), ("13:30", "16:30"), ("17:00", "19:00")])
        self.assertEqual([w["duration_minutes"] for w in free], [180, 180, 120])
        self.assertEqual(self.schedule.find_study_windows_for_day([], date(2026, 11, 7)), [])  # Saturday

    def test_study_windows_ignore_cancelled_and_incomplete(self):
        day = date(2026, 11, 4)
        events = [
            _event(day, "10:00", "12:00", is_cancelled=True),
            {"date_key": day, "start_dt": None, "end_dt": None, "title": "broken"},
        ]
        windows = self.schedule.find_study_windows_for_day(events, day)
        self.assertEqual(len(windows), 3)  # whole day free -> 3 sessions

    def test_study_windows_remaining_today(self):
        day = date(2026, 11, 4)
        events = [_event(day, "12:00", "14:00")]
        now = datetime(2026, 11, 4, 10, 30, tzinfo=TZ)
        windows = self.schedule.find_study_windows_for_day(events, day, reference_now=now)
        self.assertTrue(windows[0]["is_remaining"])
        self.assertEqual(windows[0]["duration_minutes"], 90)

    def test_weekly_windows_use_given_week(self):
        monday = date(2026, 11, 2)
        windows = self.schedule.find_weekly_study_gaps([], monday)
        self.assertEqual(len(windows), 15)  # 5 days * 3 sessions
        self.assertEqual(sorted(set(w["date_key"] for w in windows)), [date(2026, 11, d) for d in range(2, 7)])

    def test_study_session_reminder_triggered(self):
        from unittest.mock import AsyncMock, patch

        mock_bot = mock.MagicMock()
        mock_bot.send_message = AsyncMock()

        # Wednesday Nov 4, 2026 at 09:02 Berlin time (28 mins before 09:30 morning study session)
        fixed_now = datetime(2026, 11, 4, 9, 2, tzinfo=TZ)
        cached_reminders = {}

        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        with patch("unibot.schedule.get_calendar_events", new_callable=AsyncMock) as mock_get_events, \
             patch("unibot.schedule.load_reminders_cache", return_value=cached_reminders), \
             patch("unibot.schedule.save_reminders_cache") as mock_save, \
             patch("unibot.schedule.datetime", MockDateTime):

            # 1 dummy event so events is non-empty, but on another day so Wednesday is free
            mock_get_events.return_value = [_event(date(2026, 11, 2), "10:00", "12:00")]

            asyncio.run(self.schedule.check_calendar_reminders(mock_bot, chat_id=999, silent=True))

            mock_bot.send_message.assert_called_once()
            kwargs = mock_bot.send_message.call_args[1]
            self.assertEqual(kwargs["chat_id"], 999)
            self.assertIn("STUDY TIME REMINDER", kwargs["text"])
            self.assertIn("Morning focus session", kwargs["text"])
            self.assertTrue(mock_save.called)

    def test_multi_user_admin_and_invites(self):
        from unibot import db, auth

        admin_id = 9990001
        db.bootstrap_admins({admin_id})
        self.assertTrue(auth.is_user_admin(admin_id))
        self.assertTrue(auth.is_user_allowed(admin_id))

        # Create invite token
        code = db.create_invite_token(created_by=admin_id, label="Student Anna")
        self.assertTrue(code.startswith("UNI-"))

        # Redeem token
        new_user_id = 9990002
        ok, msg = db.redeem_invite_token(code, telegram_id=new_user_id, full_name="Anna Schmidt", username="anna_s")
        self.assertTrue(ok)
        self.assertTrue(auth.is_user_allowed(new_user_id))
        self.assertFalse(auth.is_user_admin(new_user_id))

        # Token cannot be re-used
        ok2, msg2 = db.redeem_invite_token(code, telegram_id=9990003)
        self.assertFalse(ok2)

        # Save and load encrypted credentials in SQLite
        fake_cookies = {"PHPSESSID": "secret123456", "session_user": "anna"}
        db.save_user_credential(new_user_id, "cookies", fake_cookies)
        loaded = db.load_user_credential(new_user_id, "cookies")
        self.assertEqual(loaded, fake_cookies)

        # Analytics
        analytics = db.get_user_analytics()
        self.assertGreaterEqual(analytics["total_users"], 2)
        self.assertGreaterEqual(analytics["active_users"], 2)

    def test_rooms_parser(self):
        page = """
        <article><h1>Rooms and times</h1>
          <h4><a>A14 1-102</a></h4>
          <ul>
            <li>Wednesday 10:00 - 12:00, weekly from 14.10.2026</li>
            <li>Di., 21.10.2026 14:00 - 16:00</li>
          </ul>
          <h4><a>V03 0-C002</a></h4>
          <ul><li>Do. 08:00 - 10:00 so we meet here</li></ul>
        </article>
        """
        rooms = self.schedule._parse_course_rooms_page(page)
        self.assertEqual(rooms[("recurring", 2, "10:00")], "A14 1-102")
        self.assertEqual(rooms[("date", date(2026, 10, 21), "14:00")], "A14 1-102")
        self.assertEqual(rooms[("recurring", 3, "08:00")], "V03 0-C002")
        # two different rooms: no blind course-wide fallback
        self.assertNotIn(("course_default", None), rooms)

    def test_weekday_short_forms_need_punctuation(self):
        self.assertEqual(self.schedule._parse_weekday("Do. 10:00 - 12:00"), 3)
        self.assertIsNone(self.schedule._parse_weekday("so we do this at 10:00 - 12:00"))


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(studip_oauth, "TOKEN_FILE", os.path.join(self.tmp.name, "tokens.json")),
            mock.patch.object(studip_vault, "KEY_FILE", os.path.join(self.tmp.name, ".vault_key")),
            mock.patch.dict(os.environ, {"SESSION_ENCRYPTION_KEY": ""}),
        ]
        for p in self.patches:
            p.start()
        studip_oauth._refresh_blocked_until = 0.0

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_restoring_backup_keeps_real_expiry(self):
        past = time.time() - 7200
        studip_oauth.save_tokens({"access_token": "a", "expires_in": 3600, "expires_at": past}, fresh=False)
        os.remove(studip_oauth.TOKEN_FILE)  # only the .bak is left
        tokens = studip_oauth.load_tokens()
        self.assertAlmostEqual(tokens["expires_at"], past, delta=1)
        self.assertTrue(studip_oauth.is_expired(tokens))

    def test_fresh_tokens_get_new_expiry(self):
        studip_oauth.save_tokens({"access_token": "a", "expires_in": 3600})
        self.assertFalse(studip_oauth.is_expired(studip_oauth.load_tokens()))

    def test_rejected_refresh_token_is_cleared(self):
        studip_oauth.save_tokens({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                  "expires_at": time.time() - 10}, fresh=False)

        async def rejected(_tokens=None):
            raise studip_oauth.RefreshRejected("invalid_grant")

        with mock.patch.object(studip_oauth, "refresh", rejected):
            self.assertIsNone(asyncio.run(studip_oauth.get_access_token()))
        self.assertIsNone(studip_oauth.load_tokens())

    def test_transient_refresh_failure_backs_off(self):
        studip_oauth.save_tokens({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                  "expires_at": time.time() - 10}, fresh=False)
        calls = []

        async def failing(_tokens=None):
            calls.append(1)
            raise RuntimeError("network down")

        with mock.patch.object(studip_oauth, "refresh", failing):
            asyncio.run(studip_oauth.get_access_token())
            asyncio.run(studip_oauth.get_access_token())
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(studip_oauth.load_tokens())  # kept for a later retry


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_patch = mock.patch.object(studip_vault, "KEY_FILE", os.path.join(self.tmp.name, ".vault_key"))
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.tmp.cleanup()

    def test_legacy_telegram_token_file_is_migrated(self):
        from cryptography.fernet import Fernet

        path = os.path.join(self.tmp.name, "cookies.json")
        legacy_key = studip_vault._key_from_secret("123:legacy-token")
        token = Fernet(legacy_key).encrypt(json.dumps({"c": 1}).encode()).decode()
        with open(path, "w") as fh:
            fh.write(studip_vault.ENCRYPTED_MAGIC + token)

        env = {"TELEGRAM_TOKEN": "123:legacy-token", "SESSION_ENCRYPTION_KEY": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(studip_vault.read_vault_file(path), {"c": 1})
        # now re-encrypted with the key file: readable without the old token
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "", "SESSION_ENCRYPTION_KEY": ""}):
            self.assertEqual(studip_vault.read_vault_file(path), {"c": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(studip_vault.KEY_FILE).st_mode & 0o777, 0o600)


class LoginServerTests(unittest.TestCase):
    BOT_TOKEN = "123456:TEST"

    def _server(self, allowed):
        return studip_login_server.LoginServer(
            on_login=None, bot_token=self.BOT_TOKEN, allowed_user_ids=allowed, host="127.0.0.1")

    def _init_data(self, user_id: int, age: int = 0) -> str:
        fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", self.BOT_TOKEN.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    @staticmethod
    def _request(sid: str):
        return types.SimpleNamespace(cookies={studip_login_server.PROXY_COOKIE: sid})

    def test_init_data_validation(self):
        good = studip_login_server.verify_init_data(self._init_data(7), self.BOT_TOKEN)
        self.assertEqual(good["id"], 7)
        self.assertIsNone(studip_login_server.verify_init_data(self._init_data(7), "other:token"))
        self.assertIsNone(studip_login_server.verify_init_data(self._init_data(7, age=7200), self.BOT_TOKEN))

    def test_empty_allowlist_admits_nobody(self):
        self.assertFalse(self._server(set())._is_allowed(7))

    def test_revoke_all_invalidates_cookies(self):
        server = self._server({7})
        sid = server._create_proxy_session(7)
        self.assertIsNotNone(server._check_proxy_session(self._request(sid)))
        server.revoke_all()
        self.assertIsNone(server._check_proxy_session(self._request(sid)))

    def test_no_sliding_expiry(self):
        server = self._server({7})
        sid = server._create_proxy_session(7)
        signed_exp = int(sid.split(".")[1])
        for _ in range(3):  # using the cookie must not push its expiry out
            sess = server._check_proxy_session(self._request(sid))
        self.assertEqual(sess["expires"], float(signed_exp))
        expired = f"7.{int(time.time()) - 1}."
        expired += hmac.new(server._cookie_key(), expired[:-1].encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(server._check_proxy_session(self._request(expired)))

    def test_csrf_token_is_per_session(self):
        server = self._server({7})
        a, b = server._create_proxy_session(7), server._create_proxy_session(7) + "x"
        self.assertNotEqual(server._csrf_token(a), server._csrf_token(b))


class ProxyRoutingTests(unittest.TestCase):
    def test_passthrough_when_unset(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "", "CLOUDFLARE_WORKER_SECRET": ""}):
            url = "https://elearning.uni-oldenburg.de/dispatch.php/start"
            routed, headers = studip_utils.route_proxy_url(url, {"X-Custom": "val"})
            self.assertEqual(routed, url)
            self.assertEqual(headers, {"X-Custom": "val"})
            self.assertEqual(studip_utils.unroute_proxy_url(url), url)

    def test_routes_university_hosts_with_secret(self):
        worker = "https://my-uni-worker.subdomain.workers.dev"
        env = {
            "CLOUDFLARE_WORKER_URL": worker,
            "CLOUDFLARE_WORKER_SECRET": "test-super-secret",
        }
        with mock.patch.dict(os.environ, env):
            url = "https://elearning.uni-oldenburg.de/dispatch.php/start?foo=bar"
            routed, headers = studip_utils.route_proxy_url(url)
            self.assertEqual(
                routed,
                f"{worker}/elearning.uni-oldenburg.de/dispatch.php/start?foo=bar",
            )
            self.assertEqual(headers.get("X-Proxy-Secret"), "test-super-secret")
            self.assertEqual(headers.get("X-Target-Host"), "elearning.uni-oldenburg.de")

            unrouted = studip_utils.unroute_proxy_url(routed)
            self.assertEqual(unrouted, url)

    def test_ignores_non_university_hosts(self):
        worker = "https://my-uni-worker.subdomain.workers.dev"
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": worker}):
            telegram_url = "https://api.telegram.org/bot123/getMe"
            routed, headers = studip_utils.route_proxy_url(telegram_url)
            self.assertEqual(routed, telegram_url)
            self.assertNotIn("X-Proxy-Secret", headers)
            self.assertNotIn("X-Target-Host", headers)


class MultiUserSessionTests(unittest.TestCase):
    def setUp(self):
        from unibot import db, state
        self.db = db
        self.state = state
        self.state.user_sessions.clear()

    def test_get_user_session_isolation_and_db_cookies(self):
        from unibot.auth import get_user_session
        s1 = get_user_session(1001)
        s2 = get_user_session(1002)
        self.assertIsNot(s1, s2)
        self.assertEqual(s1.user_id, 1001)
        self.assertEqual(s2.user_id, 1002)

        # Save cookies for user 1001
        self.db.save_user_credential(1001, "cookies", [{"key": "test_cookie", "value": "val_1001"}])
        self.assertIsNotNone(self.db.load_user_credential(1001, "cookies"))
        self.assertIsNone(self.db.load_user_credential(1002, "cookies"))

    def test_oauth_per_user_isolation(self):
        tok1 = {"access_token": "acc_1", "refresh_token": "ref_1", "expires_in": 3600}
        tok2 = {"access_token": "acc_2", "refresh_token": "ref_2", "expires_in": 3600}
        studip_oauth.save_tokens(tok1, user_id=2001)
        studip_oauth.save_tokens(tok2, user_id=2002)

        loaded1 = studip_oauth.load_tokens(user_id=2001)
        loaded2 = studip_oauth.load_tokens(user_id=2002)
        self.assertEqual(loaded1.get("access_token"), "acc_1")
        self.assertEqual(loaded2.get("access_token"), "acc_2")

        studip_oauth.clear_tokens(user_id=2001)
        self.assertIsNone(studip_oauth.load_tokens(user_id=2001))
        self.assertIsNotNone(studip_oauth.load_tokens(user_id=2002))

    def test_login_server_session_resolution_and_callback(self):
        sessions = {}
        def provider(uid):
            if uid not in sessions:
                sessions[uid] = f"sess_{uid}"
            return sessions[uid]

        def is_allowed(uid):
            return uid == 777

        srv = studip_login_server.LoginServer(
            on_login=None,
            session_provider=provider,
            is_allowed_callback=is_allowed,
        )
        self.assertEqual(srv._resolve_session(123), "sess_123")
        self.assertEqual(srv._resolve_session(456), "sess_456")
        self.assertTrue(srv._is_allowed(777))
        self.assertFalse(srv._is_allowed(888))

    def test_set_user_role_and_user_logout_isolation(self):
        from unibot.auth import _perform_logout, get_user_session
        from studip_session import StudIPSession

        # Test role modification
        self.db.upsert_user(3001, role="student")
        self.db.set_user_role(3001, "admin")
        self.assertEqual(self.db.get_user(3001)["role"], "admin")
        self.db.set_user_role(3001, "student")
        self.assertEqual(self.db.get_user(3001)["role"], "student")

        # Setup mock user sessions and credentials
        sess1 = get_user_session(3001)
        sess2 = get_user_session(3002)
        self.state.user_courses_map[3001] = {"c1": "Math"}
        self.state.user_courses_map[3002] = {"c2": "Physics"}
        self.db.save_user_credential(3001, "cookies", [{"key": "c1", "value": "v1"}])
        self.db.save_user_credential(3002, "cookies", [{"key": "c2", "value": "v2"}])

        # Logout user 3001
        asyncio.run(_perform_logout(user_id=3001))

        # User 3001 is cleaned up
        self.assertNotIn(3001, self.state.user_sessions)
        self.assertNotIn(3001, self.state.user_courses_map)
        self.assertIsNone(self.db.load_user_credential(3001, "cookies"))

        # User 3002 is completely untouched
        self.assertIn(3002, self.state.user_sessions)
        self.assertIn(3002, self.state.user_courses_map)
        self.assertIsNotNone(self.db.load_user_credential(3002, "cookies"))

    def test_list_courses_records_in_user_courses_map(self):
        from unibot.studip_api import list_courses

        with mock.patch("unibot.studip_api._list_courses_api", new=mock.AsyncMock(return_value=[("Distributed Systems", "cid_dist_sys")])):
            courses = asyncio.run(list_courses(user_id=4001))
            self.assertEqual(courses, [("Distributed Systems", "cid_dist_sys")])
            self.assertIn(4001, self.state.user_courses_map)
            self.assertEqual(self.state.user_courses_map[4001]["cid_dist_sys"], "Distributed Systems")

    def test_status_command_student_and_admin(self):
        from unibot.system import status_command

        student_update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=5001),
            message=mock.AsyncMock(),
        )
        with mock.patch("unibot.system.is_user_admin", return_value=False), \
             mock.patch("unibot.auth.is_user_admin", return_value=False):
            asyncio.run(status_command(student_update, None))
            sent_text = student_update.message.reply_text.call_args[0][0]
            self.assertIn("Student Account Status", sent_text)
            self.assertNotIn("Bot Memory", sent_text)
            self.assertNotIn("WA Group", sent_text)

        admin_update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=9999),
            message=mock.AsyncMock(),
        )
        with mock.patch("unibot.system.is_user_admin", return_value=True), \
             mock.patch("unibot.auth.is_user_admin", return_value=True):
            asyncio.run(status_command(admin_update, None))
            sent_text = admin_update.message.reply_text.call_args[0][0]
            self.assertIn("Bot Status", sent_text)
            self.assertIn("Bot Memory", sent_text)
            self.assertIn("Unified Watcher", sent_text)

    def test_admin_drill_down_callbacks(self):
        from unibot.system import handle_admin_callbacks

        self.db.upsert_user(6001, role="student", username="stud1", full_name="Student One", is_active=1)

        query = mock.AsyncMock()
        query.data = "admin_user_detail|6001"
        query.from_user = types.SimpleNamespace(id=9999)
        update = types.SimpleNamespace(callback_query=query)
        with mock.patch("unibot.system.is_user_admin", return_value=True):
            asyncio.run(handle_admin_callbacks(update, None))
            detail_text = query.edit_message_text.call_args[0][0]
            self.assertIn("Student Profile: @stud1", detail_text)
            self.assertIn("Student", detail_text)

            query.data = "admin_toggle_user|6001"
            asyncio.run(handle_admin_callbacks(update, None))
            self.assertEqual(self.db.get_user(6001)["is_active"], 0)

            query.data = "admin_role_user|6001"
            asyncio.run(handle_admin_callbacks(update, None))
            self.assertEqual(self.db.get_user(6001)["role"], "admin")

            self.state.user_sessions[6001] = mock.AsyncMock()
            query.data = "admin_revoke_user|6001"
            asyncio.run(handle_admin_callbacks(update, None))
            self.assertNotIn(6001, self.state.user_sessions)


if __name__ == "__main__":
    unittest.main()
