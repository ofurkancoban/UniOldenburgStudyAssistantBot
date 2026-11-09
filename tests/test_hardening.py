"""Regression tests for the download, login, timezone, cache and logging fixes.

Run from the project root:
    .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
# Tests must never touch the real bot database
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

import studip_session  # noqa: E402
import studip_utils  # noqa: E402


class UtilsTests(unittest.TestCase):
    def test_safe_filename_strips_paths(self):
        self.assertEqual(studip_utils.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(studip_utils.safe_filename("/root/.ssh/id_rsa"), "id_rsa")
        self.assertEqual(studip_utils.safe_filename("..\\..\\win.ini"), "win.ini")
        self.assertEqual(studip_utils.safe_filename(".."), "file")
        self.assertEqual(studip_utils.safe_filename("Lecture 1: Intro?.pdf"), "Lecture 1 Intro.pdf")

    def test_safe_zip_path_never_escapes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(studip_utils.safe_zip_path("../../Week 1/..") + "/" + studip_utils.safe_filename("../x.pdf"), b"x")
        names = zipfile.ZipFile(buf).namelist()
        self.assertEqual(names, ["Week 1/x.pdf"])

    def test_redact_url(self):
        self.assertEqual(
            studip_utils.redact_url("https://login.uni-oldenburg.de/nidp/x?code=SECRET&state=abc#frag"),
            "https://login.uni-oldenburg.de/nidp/x",
        )

    def test_berlin_now_ignores_host_timezone(self):
        if not hasattr(time, "tzset"):
            self.skipTest("tzset not available")
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            berlin = studip_utils.berlin_now()
            expected = datetime.now(studip_utils.TZ_BERLIN).replace(tzinfo=None)
            self.assertLess(abs((expected - berlin).total_seconds()), 5)
            self.assertIsNone(berlin.tzinfo)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_atomic_write_json_keeps_old_file_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            studip_utils.atomic_write_json(path, {"a": 1})
            with self.assertRaises(TypeError):
                studip_utils.atomic_write_json(path, {"bad": object()})
            with open(path) as fh:
                self.assertEqual(json.load(fh), {"a": 1})
            self.assertEqual(os.listdir(d), ["c.json"])  # no temp file left behind


class LoginBackoffTests(unittest.TestCase):
    def _session(self):
        s = studip_session.StudIPSession("user", "pw", "JBSWY3DPEHPK3PXP", cookie_file="/nonexistent")
        s.ensure_session = mock.AsyncMock()
        s.session = mock.MagicMock()
        s.save_cookies = mock.AsyncMock()
        s._check_html_logged_in = mock.AsyncMock(return_value=False)
        return s

    def test_failures_back_off_and_success_resets(self):
        s = self._session()
        s._run_sso_flow = mock.AsyncMock(side_effect=RuntimeError("bad password"))
        s.session.get = mock.MagicMock()
        s.session.get.return_value.__aenter__ = mock.AsyncMock()
        s.session.get.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        self.assertFalse(asyncio.run(s.login()))
        self.assertEqual(s.login_failures, 1)
        self.assertEqual(s._run_sso_flow.await_count, 1)

        # Blocked: no second password submission inside the backoff window
        self.assertFalse(asyncio.run(s.login()))
        self.assertEqual(s._run_sso_flow.await_count, 1)

        # force bypasses the backoff (explicit user action)
        s._run_sso_flow = mock.AsyncMock(return_value=("u", "t"))
        self.assertTrue(asyncio.run(s.login(force=True)))
        self.assertEqual(s.login_failures, 0)
        self.assertEqual(s.login_blocked_until, 0.0)

    def test_backoff_grows(self):
        s = self._session()
        for _ in range(5):
            s._record_login_failure()
        self.assertAlmostEqual(s.login_blocked_until - time.time(), 3600, delta=5)

    def test_positive_login_check_is_cached(self):
        s = self._session()
        s._check_html_logged_in = mock.AsyncMock(return_value=True)
        self.assertTrue(asyncio.run(s.is_html_logged_in()))
        self.assertTrue(asyncio.run(s.is_html_logged_in()))
        self.assertEqual(s._check_html_logged_in.await_count, 1)
        self.assertTrue(asyncio.run(s.is_html_logged_in(use_cache=False)))
        self.assertEqual(s._check_html_logged_in.await_count, 2)

    def test_negative_login_check_is_cached_briefly(self):
        # a logged-out answer is kept for 15 s so a burst of handlers does not
        # re-download Stud.IP pages; use_cache=False always asks again
        s = self._session()
        asyncio.run(s.is_html_logged_in())
        asyncio.run(s.is_html_logged_in())
        self.assertEqual(s._check_html_logged_in.await_count, 1)
        asyncio.run(s.is_html_logged_in(use_cache=False))
        self.assertEqual(s._check_html_logged_in.await_count, 2)
        s._logged_out_until = time.time() - 1
        asyncio.run(s.is_html_logged_in())
        self.assertEqual(s._check_html_logged_in.await_count, 3)


class BotStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        from unibot import storage, tasks, telegram_ui
        os.chdir(cwd)
        cls.storage = storage
        cls.tasks = tasks
        cls.ui = telegram_ui

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(self.storage, "GENERAL_CACHE_PATH", os.path.join(self.tmp.name, "general.json")),
            mock.patch.object(self.storage, "TASKS_CACHE_PATH", os.path.join(self.tmp.name, "tasks.json")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_update_general_cache_keeps_other_keys(self):
        self.storage.save_general_cache({"watch_prev": True, "health_state": {}})
        stale = self.storage.load_general_cache()
        # a button handler changes a setting while a background job is awaiting
        fresh = self.storage.load_general_cache()
        fresh["watch_prev"] = False
        self.storage.save_general_cache(fresh)
        stale_update = {"health_state": {"weather": True}}
        self.storage.update_general_cache(stale_update)
        result = self.storage.load_general_cache()
        self.assertFalse(result["watch_prev"])
        self.assertEqual(result["health_state"], {"weather": True})
        self.assertTrue(stale["watch_prev"])  # the stale copy was never written

    def test_task_reminder_keeps_tasks_added_meanwhile(self):
        past = (datetime.now(studip_utils.TZ_BERLIN) - timedelta(minutes=1)).isoformat()
        self.storage.save_user_tasks(1, [{"id": "t1", "user_id": 1, "text": "Read <ch.3> & notes", "due_time": past}])
        sent = []

        class FakeBot:
            async def send_message(inner_self, **kw):
                sent.append(kw["text"])
                # the user adds a task while the reminder is being sent
                self.storage.add_user_task(1, {"id": "t2", "user_id": 1, "text": "new", "due_time": None})

        asyncio.run(self.tasks.check_task_reminders(FakeBot()))
        self.assertEqual([t["id"] for t in self.storage.user_tasks(1)], ["t2"])
        self.storage.save_user_tasks(1, [])
        self.assertIn("Read &lt;ch.3&gt; &amp; notes", sent[0])  # HTML-escaped

    def test_single_flight_serializes(self):
        running = []
        overlaps = []

        @self.storage.single_flight
        async def job():
            if running:
                overlaps.append(True)
            running.append(1)
            await asyncio.sleep(0.01)
            running.pop()

        async def main():
            await asyncio.gather(job(), job(), job())

        asyncio.run(main())
        self.assertEqual(overlaps, [])

    def test_link_cache_persists(self):
        path = os.path.join(self.tmp.name, "links.json")
        cache = self.storage._PersistentLinkCache(path)
        cache["abc"] = {"cid": "c1", "name": "x.pdf", "ts": time.time()}  # no loop: written at once
        reloaded = self.storage._PersistentLinkCache(path)
        self.assertEqual(reloaded["abc"]["name"], "x.pdf")

        async def debounced():
            cache["def"] = {"cid": "c2", "ts": time.time()}
            cache["ghi"] = {"cid": "c3", "ts": time.time()}
            self.assertNotIn("def", self.storage._PersistentLinkCache(path))  # not flushed yet
            cache.flush()

        asyncio.run(debounced())
        self.assertIn("ghi", self.storage._PersistentLinkCache(path))

    def test_send_file_bytes_uses_memory_and_safe_name(self):
        message = mock.MagicMock()
        seen = []
        message.reply_document = mock.AsyncMock(side_effect=lambda **kw: seen.append(kw["document"].getvalue()))
        asyncio.run(self.ui._send_file_bytes(message, b"%PDF", "../../evil.pdf", "sid1"))
        kwargs = message.reply_document.await_args.kwargs
        self.assertEqual(kwargs["filename"], "evil.pdf")
        self.assertEqual(seen, [b"%PDF"])
        self.assertTrue(kwargs["document"].closed)  # the buffer is closed once sent


if __name__ == "__main__":
    unittest.main()
