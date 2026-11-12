"""Fixes from the full project review: the primary admin, the removed
password login route, blocked users, per-student names, HTML escaping,
students who block the bot, the held-alert digest, the folder ZIP, buttons
that survive a restart, background tasks, the process time zone and async
file I/O."""

import asyncio
import io
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_handler, use_temp_db  # noqa: E402
use_temp_db()

from telegram.error import BadRequest, Forbidden  # noqa: E402

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import auth, course_registry, db, preferences, state, storage  # noqa: E402


class PrimaryAdminTests(unittest.TestCase):
    def test_primary_admin_keeps_the_environment_order(self):
        from unibot.config import _first_user_id, _parse_allowed_user_ids
        env = "8312553602,7192285920"
        self.assertEqual(_first_user_id("", env), 8312553602)
        # a set does not keep that order, which is why it is never used for this
        self.assertEqual(_parse_allowed_user_ids(env), {8312553602, 7192285920})

    def test_message_poll_follows_the_watcher_sessions_owner(self):
        from unibot import monitoring
        sent_to = []

        async def check(bot, chat_id, silent=False):
            sent_to.append(chat_id)
            raise asyncio.CancelledError  # one round is enough

        watcher = mock.MagicMock(user_id=8312553602)
        with mock.patch.object(monitoring, "MESSAGE_POLL_SECONDS", 0), \
                mock.patch.object(monitoring, "PRIMARY_ADMIN_ID", 7192285920), \
                mock.patch.object(monitoring, "has_stored_login", lambda s: True), \
                mock.patch.object(monitoring, "lifecycle_state", lambda uid: auth.ACTIVE), \
                mock.patch.object(monitoring, "check_new_messages", check), \
                mock.patch.object(state, "global_session", watcher), \
                mock.patch.object(state, "global_watcher_paused", False), \
                mock.patch.object(state, "user_sessions", {}):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(monitoring.message_poll_loop(mock.MagicMock()))
        self.assertEqual(sent_to, [8312553602])  # the inbox's own student, not the set's first id


class LoginRouteTests(unittest.TestCase):
    def test_password_api_login_is_gone(self):
        import studip_login_server
        self.assertFalse(hasattr(studip_login_server.LoginServer, "_api_login"))
        server = studip_login_server.LoginServer(bot_token="1:x", host="127.0.0.1")
        self.assertFalse(hasattr(server, "on_login"))
        self.assertFalse(hasattr(auth, "_perform_web_login"))


class BlockedUserTests(unittest.TestCase):
    def test_an_invite_never_lifts_a_block(self):
        uid = 9101
        db.upsert_user(telegram_id=uid)
        db.set_user_active(uid, False)
        code = db.create_invite_token(created_by=1, max_uses=3)
        ok, msg = db.redeem_invite_token(code, uid)
        self.assertFalse(ok)
        self.assertIn("blocked", msg)
        self.assertFalse(db.get_user(uid)["is_active"])
        token = next(t for t in db.list_invite_tokens() if t["code"] == code)
        self.assertEqual(token["use_count"], 0)

    def test_profile_sync_keeps_a_block(self):
        uid = 9102
        db.upsert_user(telegram_id=uid)
        db.set_user_active(uid, False)
        db.upsert_user(telegram_id=uid, studip_username="ada")  # the sign-in hook
        self.assertFalse(db.get_user(uid)["is_active"])
        self.assertEqual(db.get_user(uid)["studip_username"], "ada")

    def test_existing_member_uses_no_quota(self):
        uid = 9103
        db.upsert_user(telegram_id=uid)
        code = db.create_invite_token(created_by=1, max_uses=1)
        self.assertEqual(db.redeem_invite_token(code, uid), (True, "You already have access."))
        self.assertTrue(db.redeem_invite_token(code, 9104)[0])  # the slot is still there


class StudentNameTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(storage, "PRIMARY_ADMIN_ID", 9200)
        patcher.start()
        self.addCleanup(patcher.stop)
        for uid in (9200, 9201, 9202, 9203):
            db.get_user_scope(uid).delete_private()
            storage._names_migrated.discard(uid)
        db.delete(storage.USER_NAMES_CACHE_PATH)
        storage.save_personal_record(9200, storage.WATCHER_NAME_RECORD, "Admin Person")
        storage.save_user_name(9201, "Ada")

    def test_no_student_gets_someone_elses_name(self):
        self.assertEqual(storage.get_user_name(9201), "Ada")
        self.assertIsNone(storage.get_user_name(9202))          # a student without a name
        self.assertEqual(storage.get_user_name(9200), "Admin Person")  # the primary admin

    def test_stud_ip_name_never_overwrites_a_chosen_name(self):
        storage.save_studip_user_name("Ada Lovelace", user_id=9201)
        self.assertEqual(storage.get_user_name(9201), "Ada")
        storage.save_studip_user_name("Grace Hopper", user_id=9203)
        self.assertEqual(storage.get_user_name(9203), "Grace Hopper")
        self.assertEqual(storage.get_primary_user_name(), "Admin Person")  # untouched by a student

    def test_names_leave_the_shared_plain_record_and_die_with_the_student(self):
        db.save(storage.USER_NAMES_CACHE_PATH, {"9202": "Bob", "studip_name": "Admin Old"})
        db.get_user_scope(9200).delete_private()
        self.assertEqual(storage.get_user_name(9202), "Bob")
        self.assertEqual(storage.get_primary_user_name(), "Admin Old")
        self.assertFalse(db.exists(storage.USER_NAMES_CACHE_PATH))  # nothing left in plain text
        db.get_user_scope(9202).delete_private()  # what deleting their data does
        self.assertIsNone(storage.get_user_name(9202))


class HtmlEscapeTests(unittest.TestCase):
    def test_morning_tasks_and_lunch_are_escaped(self):
        from unibot import morning
        tasks = [{"due_time": "2026-11-10T09:00:00+01:00", "text": "Read <Chapter 3> & notes", "course": "R&D"}]
        section = morning._format_tasks_section(tasks)
        self.assertIn("Read &lt;Chapter 3&gt; &amp; notes", section)
        self.assertIn("[R&amp;D]", section)
        self.assertNotIn("<Chapter", section)
        lunch = morning._format_lunch_section({"dish": "Kartoffeln & Quark <neu>", "category": "Hauptgericht"})
        self.assertIn("Kartoffeln &amp; Quark &lt;neu&gt;", lunch)


class BlockedBotTests(unittest.TestCase):
    def test_a_student_who_blocks_the_bot_becomes_inactive(self):
        from unibot.telegram_ui import TelegramSafetyLimiter, mark_blocked_chat
        uid = 9301
        db.upsert_user(telegram_id=uid)
        db.set_lifecycle(uid, "active")

        async def send(**kw):
            raise Forbidden("Forbidden: bot was blocked by the user")

        with self.assertRaises(Forbidden):
            asyncio.run(TelegramSafetyLimiter().process_request(send, (), {}, "sendMessage", {"chat_id": uid}, None))
        self.assertEqual(db.get_lifecycle(uid)["state"], "inactive")
        self.assertFalse(mark_blocked_chat(-100123, Forbidden("bot was blocked by the user")))  # a group
        self.assertFalse(mark_blocked_chat(9399, Forbidden("bot was blocked by the user")))     # unknown user
        db.set_lifecycle(uid, "active")
        self.assertFalse(mark_blocked_chat(uid, Forbidden("bot is not a member of the channel chat")))
        self.assertEqual(db.get_lifecycle(uid)["state"], "active")


class AlertQueueTests(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN)

    def _items(self, n, ts=None, reason="quiet"):
        ts = self.NOW.timestamp() if ts is None else ts
        return [{"kind": "files", "cid": "c", "title": f"📄 <b>File {i}</b> " + "x" * 80, "button": None,
                 "reason": reason, "ts": ts} for i in range(n)]

    def test_digest_is_cut_between_whole_lines(self):
        items = self._items(100)
        batch = course_registry.digest_batch(items)
        self.assertLess(len(batch), 100)
        self.assertLessEqual(len(course_registry.digest_text(batch)), course_registry.DIGEST_TEXT_LIMIT)
        self.assertTrue(course_registry.digest_text(batch).endswith("x"))  # no cut tag

    def test_old_and_surplus_items_are_dropped(self):
        old = self._items(2, ts=self.NOW.timestamp() - course_registry.QUEUE_MAX_AGE_SECONDS - 1)
        fresh = self._items(course_registry.QUEUE_MAX_ITEMS + 5)
        pruned = course_registry._prune_queue(old + fresh, self.NOW)
        self.assertEqual(len(pruned), course_registry.QUEUE_MAX_ITEMS)
        self.assertNotIn(old[0], pruned)

    def _flush(self, uid, queue, send, **kw):
        scope = db.get_user_scope(uid)
        scope.sync_courses({"c"})
        scope.save_private(course_registry.ALERT_QUEUE_KIND, queue)
        bot = mock.MagicMock()
        bot.send_message = send
        with mock.patch.object(course_registry.asyncio, "sleep", mock.AsyncMock()):
            sent = asyncio.run(course_registry.flush_alert_queues(bot, now=kw.pop("now", self.NOW), user_ids=[uid], **kw))
        return sent, scope.private(course_registry.ALERT_QUEUE_KIND)

    def test_rejected_html_goes_out_as_plain_text(self):
        uid = 9401
        db.set_lifecycle(uid, "active")
        preferences.set_pref(uid, "quiet_hours", None)
        calls = []

        async def send(**kw):
            calls.append(kw)
            if kw.get("parse_mode") == "HTML":
                raise BadRequest("Can't parse entities")

        sent, left = self._flush(uid, self._items(2), send)
        self.assertEqual(sent, 1)
        self.assertEqual(left, [])
        self.assertNotIn("<b>", calls[-1]["text"])
        self.assertIsNone(calls[-1].get("parse_mode"))

    def test_paused_students_keep_their_queue(self):
        uid = 9402
        db.set_lifecycle(uid, "inactive", "stopped with /stop")
        send = mock.AsyncMock()
        sent, left = self._flush(uid, self._items(1), send)
        self.assertEqual(sent, 0)
        self.assertEqual(len(left), 1)
        send.assert_not_awaited()

    def test_held_alerts_come_with_the_morning_summary(self):
        uid = 9403
        db.set_lifecycle(uid, "active")
        preferences.set_pref(uid, "quiet_hours", {"start": "23:00", "end": "08:00"})
        preferences.set_pref(uid, "delivery_mode", "digest")
        seven = datetime(2026, 11, 10, 7, 0, tzinfo=TZ_BERLIN)  # still quiet hours
        queue = self._items(1) + self._items(1, reason="digest")
        send = mock.AsyncMock()
        sent, left = self._flush(uid, queue, send, now=seven)
        self.assertEqual(sent, 0)  # quiet hours, no morning summary yet
        sent, left = self._flush(uid, queue, send, now=seven, with_morning=True)
        self.assertEqual(sent, 1)
        self.assertEqual([i["reason"] for i in left], ["digest"])  # the evening digest still waits


class FolderZipTests(unittest.TestCase):
    def test_no_login_no_zip(self):
        from unibot import studip_api
        with mock.patch.object(studip_api, "get_user_session", lambda uid=None: None), \
                mock.patch.object(state, "global_session", mock.MagicMock()):
            buf, total, _, ok, size = asyncio.run(studip_api.create_recursive_zip("c", None, "root", user_id=9501))
        self.assertEqual((total, ok, size), (0, 0, 0))

    def test_big_files_are_skipped_and_the_rest_is_zipped(self):
        from unibot import studip_api

        class Resp:
            def __init__(self, length, body):
                self.status, self.content_length, self._body = 200, length, body
                self.content = self
                self.headers = {}

            async def iter_chunked(self, size):
                yield self._body

            async def read(self):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Sess:
            async def get(self, url):
                return Resp(studip_api.ZIP_RAW_LIMIT_BYTES + 1, b"") if url.endswith("big") else Resp(3, b"abc")

        files = [{"type": "file", "name": "small.txt", "url": "https://elearning.uni-oldenburg.de/small"},
                 {"type": "file", "name": "huge.mp4", "url": "https://elearning.uni-oldenburg.de/big"}]
        with mock.patch.object(studip_api, "list_files", mock.AsyncMock(return_value=files)):
            buf, total, _, ok, size = asyncio.run(studip_api.create_recursive_zip("c", "https://elearning.uni-oldenburg.de/folder", "root", session=Sess()))
        self.assertEqual((total, ok), (2, 1))
        self.assertGreater(size, studip_api.ZIP_RAW_LIMIT_BYTES)  # the caller says "too large"
        self.assertEqual(zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist(), ["root/small.txt"])


class RestartSafeButtonTests(unittest.TestCase):
    def test_shared_cache_survives_a_restart_and_expires(self):
        path = f"callbacks_test_{time.time_ns()}.json"
        cache = storage.persistent_callback_cache(path)
        cache["a"] = {"text": "Problem set 3"}
        cache["old"] = {"text": "stale", "ts": time.time() - storage.CALLBACK_TTL_SECONDS - 5}
        cache.flush()
        reloaded = storage.persistent_callback_cache(path)
        self.assertEqual(reloaded.get("a")["text"], "Problem set 3")
        self.assertNotIn("old", reloaded)

    def test_exam_button_after_a_restart_is_the_owners_only(self):
        from unibot import exams
        exams.exam_action_cache["s1"] = {"unit_id": "u", "action_type": "anmelden", "title": "Stats", "user_id": 9601}
        dict.clear(exams.exam_action_cache)  # the restart
        self.assertIsNone(exams._exam_action("s1", 9602))        # someone else
        self.assertEqual(exams._exam_action("s1", 9601)["unit_id"], "u")
        exams.exam_action_cache.pop("s1", None)                   # used: gone for good
        dict.clear(exams.exam_action_cache)
        self.assertIsNone(exams._exam_action("s1", 9601))

    def test_task_reminder_buttons_after_a_restart(self):
        from unibot import tasks
        storage.remember_user_callback(9701, "task:t1", {"id": "t1", "text": "Essay", "user_id": 9701})
        query = mock.MagicMock()
        query.from_user.id = 9701
        query.data = "task_reminder_done|t1"
        query.answer = mock.AsyncMock()
        query.edit_message_reply_markup = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        with mock.patch.object(tasks, "is_user_allowed", lambda uid: True), \
                mock.patch.object(tasks, "record_task_outcome", lambda *a: None):
            run_handler(tasks.handle_task_reminder_action, update, None)
        query.message.reply_text.assert_awaited_with("✔️ Done: Essay")
        self.assertIsNone(storage.recall_user_callback(9701, "task:t1"))  # one use

    def test_a_suggestion_works_for_every_follower(self):
        from unibot import tasks
        tasks.task_suggestion_cache["sg1"] = {"body": "Hand in PS3", "course": "Stats", "cid": "c1"}
        saved = []
        for uid in (9801, 9802):
            activate_student(uid, {"c1": "Stats"})
            storage.set_ai_personal_enabled(uid, True)
            self.addCleanup(storage.set_ai_personal_enabled, uid, False)
            query = mock.MagicMock()
            query.from_user.id, query.data = uid, "tasksug_add|sg1"
            query.answer, query.edit_message_reply_markup = mock.AsyncMock(), mock.AsyncMock()
            query.message.reply_text = mock.AsyncMock()
            with mock.patch.object(tasks, "is_user_allowed", lambda u: True), \
                    mock.patch.object(tasks, "extract_task_suggestion", mock.AsyncMock(return_value={"text": "Hand in PS3", "due_time": None})), \
                    mock.patch.object(tasks, "_save_task_and_confirmation_text",
                                      lambda u, *a, **kw: saved.append(u) or "ok"):
                run_handler(tasks.handle_task_suggestion_add, mock.MagicMock(callback_query=query), None)
        self.assertEqual(saved, [9801, 9802])


class BackgroundTaskTests(unittest.TestCase):
    def test_spawned_tasks_are_kept_and_failures_logged(self):
        async def boom():
            raise RuntimeError("lost")

        async def run():
            task = state.spawn(boom(), name="boom")
            self.assertIn(task, state._background_tasks)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return task

        with self.assertLogs(level=logging.ERROR) as logs:
            task = asyncio.run(run())
        self.assertNotIn(task, state._background_tasks)
        self.assertTrue(any("boom" in line for line in logs.output))


class TimeZoneAndFileTests(unittest.TestCase):
    def test_process_runs_on_berlin_time_unless_told_otherwise(self):
        env = {k: v for k, v in os.environ.items() if k != "TZ"}
        code = "import unibot.config, time; print(time.tzname[0])"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn(out.stdout.strip(), ("CET", "CEST"))
        env["TZ"] = "UTC"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "UTC")

    def test_async_file_helpers(self):
        from studip_utils import aread_bytes, awrite_bytes
        path = os.path.join(tempfile.mkdtemp(), "x.bin")
        asyncio.run(awrite_bytes(path, b"\x00ok"))
        self.assertEqual(asyncio.run(aread_bytes(path)), b"\x00ok")


if __name__ == "__main__":
    unittest.main()
