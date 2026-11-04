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

import studip_login_server  # noqa: E402
import studip_oauth  # noqa: E402
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
        import studip_bot
        os.chdir(cwd)  # the module anchors CWD to its folder on import
        cls.bot = studip_bot

    def test_time_module_not_shadowed(self):
        # Regression: `from datetime import time` once replaced the stdlib
        # module and broke every time.time()/time.monotonic() call.
        self.assertTrue(callable(getattr(self.bot.time, "time", None)))
        self.assertTrue(callable(getattr(self.bot.time, "monotonic", None)))

    def test_allowlist_fails_closed(self):
        with mock.patch.object(self.bot, "ALLOWED_USER_IDS", set()):
            self.assertFalse(self.bot.is_user_allowed(123))
        with mock.patch.object(self.bot, "ALLOWED_USER_IDS", {123}):
            self.assertTrue(self.bot.is_user_allowed(123))
            self.assertFalse(self.bot.is_user_allowed(456))
            self.assertFalse(self.bot.is_user_allowed(None))

    def test_escaping_helpers(self):
        self.assertEqual(self.bot._h("Theory & <Practice>"), "Theory &amp; &lt;Practice&gt;")
        self.assertEqual(self.bot._md("Info_1 *x*"), "Info\\_1 \\*x\\*")

    def test_study_windows_regular_day(self):
        day = date(2026, 11, 4)  # Wednesday
        events = [
            _event(day, "12:00", "14:00", "Lecture A"),
            _event(day, "15:30", "17:00", "Lecture B"),
        ]
        windows = self.bot.find_study_windows_for_day(events, day)
        spans = [(w["start_dt"].strftime("%H:%M"), w["end_dt"].strftime("%H:%M")) for w in windows]
        # before (3h), between (1.5h); after 17:00-19:00 is 2h
        self.assertEqual(spans, [("09:00", "12:00"), ("14:00", "15:30"), ("17:00", "19:00")])

    def test_study_windows_free_weekday_and_weekend(self):
        wed = date(2026, 11, 4)
        free = self.bot.find_study_windows_for_day([], wed)
        self.assertEqual(len(free), 1)
        self.assertEqual(free[0]["end_dt"].strftime("%H:%M"), "19:00")
        self.assertEqual(free[0]["duration_minutes"], 600)
        self.assertEqual(self.bot.find_study_windows_for_day([], date(2026, 11, 7)), [])  # Saturday

    def test_study_windows_ignore_cancelled_and_incomplete(self):
        day = date(2026, 11, 4)
        events = [
            _event(day, "10:00", "12:00", is_cancelled=True),
            {"date_key": day, "start_dt": None, "end_dt": None, "title": "broken"},
        ]
        windows = self.bot.find_study_windows_for_day(events, day)
        self.assertEqual(len(windows), 1)  # whole day free

    def test_study_windows_remaining_today(self):
        day = date(2026, 11, 4)
        events = [_event(day, "12:00", "14:00")]
        now = datetime(2026, 11, 4, 10, 30, tzinfo=TZ)
        windows = self.bot.find_study_windows_for_day(events, day, reference_now=now)
        self.assertTrue(windows[0]["is_remaining"])
        self.assertEqual(windows[0]["duration_minutes"], 90)

    def test_weekly_windows_use_given_week(self):
        monday = date(2026, 11, 2)
        windows = self.bot.find_weekly_study_gaps([], monday)
        self.assertEqual([w["date_key"] for w in windows], [date(2026, 11, d) for d in range(2, 7)])

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
        rooms = self.bot._parse_course_rooms_page(page)
        self.assertEqual(rooms[("recurring", 2, "10:00")], "A14 1-102")
        self.assertEqual(rooms[("date", date(2026, 10, 21), "14:00")], "A14 1-102")
        self.assertEqual(rooms[("recurring", 3, "08:00")], "V03 0-C002")
        # two different rooms: no blind course-wide fallback
        self.assertNotIn(("course_default", None), rooms)

    def test_weekday_short_forms_need_punctuation(self):
        self.assertEqual(self.bot._parse_weekday("Do. 10:00 - 12:00"), 3)
        self.assertIsNone(self.bot._parse_weekday("so we do this at 10:00 - 12:00"))


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


if __name__ == "__main__":
    unittest.main()
