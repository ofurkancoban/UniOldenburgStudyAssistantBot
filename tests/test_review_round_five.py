"""Fixes from an outside review: data deletion keeps the student's tasks,
mypy covers every module, the evening digest waits for the next digest
hour, background jobs run only for ACTIVE students, the database connection
opens under the lock, only our own WhatsApp service is stopped at startup
and the rate limit tables don't grow forever."""

import asyncio
import os
import sys
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import auth, course_registry, db, privacy, storage, watcher  # noqa: E402


class DeletionKeepsTasksTests(unittest.TestCase):
    UID = 9801

    def test_tasks_stay_everything_else_private_goes(self):
        db.upsert_user(telegram_id=self.UID)
        storage.save_user_tasks(self.UID, [{"id": "t1", "text": "Submit sheet 3"}])
        scope = db.get_user_scope(self.UID)
        scope.save_private("exam_cache", {"known_grades": {"M1": "1.3"}})
        storage.save_personal_record(self.UID, "weather_location", {"name": "Bremen", "country": "DE",
                                                                   "latitude": 53.0, "longitude": 8.8})
        with mock.patch.object(privacy, "_perform_logout", mock.AsyncMock()):
            asyncio.run(privacy.delete_personal_data(self.UID))
        self.assertEqual([t["text"] for t in storage.user_tasks(self.UID)], ["Submit sheet 3"])
        self.assertFalse(scope.has_private("exam_cache"))
        self.assertIsNone(storage.personal_record(self.UID, "weather_location"))


class MypyCoverageTests(unittest.TestCase):
    def test_every_bot_module_is_type_checked(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
            files = set(tomllib.load(fh)["tool"]["mypy"]["files"])
        modules = {f"unibot/{n}" for n in os.listdir(os.path.join(ROOT, "unibot")) if n.endswith(".py")}
        modules |= {n for n in os.listdir(ROOT) if n.endswith(".py")}
        self.assertEqual(sorted(modules - files), [])


class DigestTimingTests(unittest.TestCase):
    UID = 9802

    def _due(self, queued: datetime, now: datetime) -> bool:
        item = {"reason": "digest", "ts": queued.timestamp()}
        with mock.patch.object(course_registry, "get_pref", return_value="digest"), \
                mock.patch.object(course_registry, "in_quiet_hours", return_value=False):
            return course_registry._queue_due(self.UID, item, now)

    @staticmethod
    def _at(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 11, day, hour, minute, tzinfo=TZ_BERLIN)

    def test_an_alert_waits_for_the_next_digest_hour(self):
        self.assertFalse(self._due(self._at(10, 15), self._at(10, 17, 59)))
        self.assertTrue(self._due(self._at(10, 15), self._at(10, 18, 5)))

    def test_an_evening_alert_goes_into_tomorrows_digest(self):
        self.assertFalse(self._due(self._at(10, 20), self._at(10, 20, 10)))
        self.assertFalse(self._due(self._at(10, 20), self._at(11, 9)))
        self.assertTrue(self._due(self._at(10, 20), self._at(11, 18)))

    def test_a_missed_digest_follows_later(self):
        # queued before yesterday's 18:00, the bot was down then
        self.assertTrue(self._due(self._at(10, 17), self._at(11, 9)))

    def test_old_items_without_a_time_are_due(self):
        with mock.patch.object(course_registry, "get_pref", return_value="digest"), \
                mock.patch.object(course_registry, "in_quiet_hours", return_value=False):
            self.assertTrue(course_registry._queue_due(self.UID, {"reason": "digest"}, self._at(10, 18)))


class ActiveStudentsTests(unittest.TestCase):
    def test_only_active_students_get_background_jobs(self):
        users = [{"telegram_id": 1, "is_active": 1}, {"telegram_id": 2, "is_active": 1},
                 {"telegram_id": 3, "is_active": 1}, {"telegram_id": 4, "is_active": 0}]
        states = {1: auth.ACTIVE, 2: auth.NEEDS_REAUTH, 3: auth.INACTIVE, 4: auth.ACTIVE}
        with mock.patch.object(watcher.db, "get_all_users", return_value=users), \
                mock.patch.object(watcher, "lifecycle_state", side_effect=states.get):
            self.assertEqual(watcher._get_active_user_ids(), [1])

    def test_nobody_active_means_nobody_not_the_env_list(self):
        users = [{"telegram_id": 2, "is_active": 1}]
        with mock.patch.object(watcher.db, "get_all_users", return_value=users), \
                mock.patch.object(watcher, "lifecycle_state", return_value=auth.NEEDS_REAUTH), \
                mock.patch.object(watcher, "ALLOWED_USER_IDS", {77}):
            self.assertEqual(watcher._get_active_user_ids(), [])
        with mock.patch.object(watcher.db, "get_all_users", return_value=[]), \
                mock.patch.object(watcher, "ALLOWED_USER_IDS", {77}):
            self.assertEqual(watcher._get_active_user_ids(), [77])  # an install without users


class ConnectionLockTests(unittest.TestCase):
    def test_threads_opening_the_connection_share_one(self):
        db._conn_path = None  # the next call reopens
        seen, start = [], threading.Barrier(8)

        def worker():
            start.wait()
            seen.append(db._connection())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(c) for c in seen}), 1)
        self.assertEqual(db._connection().execute("SELECT 1").fetchone()[0], 1)  # still open


class WhatsAppCleanupTests(unittest.TestCase):
    def _proc(self, pid, cmdline, cwd):
        proc = mock.MagicMock(pid=pid)
        proc.cmdline.return_value = cmdline
        proc.cwd.return_value = cwd
        return proc

    def test_only_our_node_service_is_stopped(self):
        from unibot import app
        wa_dir = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, wa_dir)
        ours = self._proc(101, ["node", "server.js"], wa_dir)
        other_node = self._proc(102, ["node", "server.js"], "/srv/other-project")
        unrelated = self._proc(103, ["python", "app.py"], wa_dir)
        stopped = []
        with mock.patch.object(app.psutil, "process_iter", return_value=[ours, other_node, unrelated]), \
                mock.patch.object(app, "_stop_process", side_effect=stopped.append), \
                mock.patch.object(app, "WA_SERVICE_PORT", 1):
            app._cleanup_stale_whatsapp_service(wa_dir)
        self.assertEqual(stopped, [ours])

    def test_a_recycled_pid_in_the_lock_file_is_left_alone(self):
        from unibot import app
        wa_dir = tempfile.mkdtemp()
        with open(os.path.join(wa_dir, "whatsapp_service.lock"), "w") as fh:
            fh.write("4242")
        stranger = self._proc(4242, ["postgres"], "/var/lib/postgresql")
        stopped = []
        with mock.patch.object(app.psutil, "pid_exists", return_value=True), \
                mock.patch.object(app.psutil, "Process", return_value=stranger), \
                mock.patch.object(app.psutil, "process_iter", return_value=[]), \
                mock.patch.object(app, "_stop_process", side_effect=stopped.append), \
                mock.patch.object(app, "WA_SERVICE_PORT", 1):
            app._cleanup_stale_whatsapp_service(wa_dir)
        self.assertEqual(stopped, [])
        self.assertFalse(os.path.exists(os.path.join(wa_dir, "whatsapp_service.lock")))
        os.rmdir(wa_dir)


class RateTableTests(unittest.TestCase):
    def setUp(self):
        auth._rate_hits.clear()
        auth._rate_cooldown_until.clear()
        self.addCleanup(auth._rate_hits.clear)
        self.addCleanup(auth._rate_cooldown_until.clear)

    def test_idle_users_are_forgotten_past_the_threshold(self):
        now = 10_000.0
        for uid in range(auth.RATE_TABLE_PRUNE_AT + 10):
            auth._rate_hits[uid] = [now - 120]  # idle for two minutes
        auth._rate_cooldown_until[1] = now - 1
        auth._rate_cooldown_until[2] = now + 30  # still cooling down
        self.assertEqual(auth.rate_limit(999_999, now=now), "ok")
        self.assertEqual(set(auth._rate_hits), {999_999})
        self.assertEqual(set(auth._rate_cooldown_until), {2})
        self.assertEqual(auth.rate_limit(2, now=now), "drop")


if __name__ == "__main__":
    unittest.main()
