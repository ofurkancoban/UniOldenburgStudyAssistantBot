"""Fixes from an outside review: a digest flush never overwrites an alert
queued while it was sending, a blocked student is recognised on file uploads
too, every use of a multi-use invite code is kept and the WhatsApp cleanup
survives processes whose working directory vanished."""

import asyncio
import io
import json
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

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import course_registry, db, telegram_ui  # noqa: E402


class FlushRaceTests(unittest.TestCase):
    UID = 9901

    def test_an_alert_queued_during_the_send_survives(self):
        db.upsert_user(telegram_id=self.UID)
        scope = db.get_user_scope(self.UID)
        scope.sync_courses({"c1"})
        scope.delete_private(course_registry.ALERT_QUEUE_KIND)
        now = datetime(2026, 11, 10, 9, 0, tzinfo=TZ_BERLIN)
        course_registry.queue_alert(self.UID, "files", "c1", "📄 Old alert", None, "quiet",
                                    now=now.replace(hour=1))
        bot = mock.MagicMock()

        async def send_message(**kwargs):
            await asyncio.sleep(0)  # the watcher runs meanwhile
            course_registry.queue_alert(self.UID, "files", "c1", "📄 New alert", None, "quiet", now=now)

        bot.send_message = send_message
        with mock.patch.object(course_registry, "lifecycle_state", return_value=course_registry.ACTIVE), \
                mock.patch.object(course_registry, "_queue_due",
                                  side_effect=lambda uid, item, now, with_morning=False: item["title"] == "📄 Old alert"), \
                mock.patch.object(course_registry.asyncio, "sleep", mock.AsyncMock()):
            self.assertEqual(asyncio.run(course_registry.flush_alert_queues(bot, now, user_ids=[self.UID])), 1)
        titles = [item["title"] for item in scope.private(course_registry.ALERT_QUEUE_KIND) or []]
        self.assertEqual(titles, ["📄 New alert"])  # the sent one went, the new one stayed


class BlockedOnUploadTests(unittest.TestCase):
    def test_a_document_to_a_student_who_blocked_the_bot_marks_them(self):
        from telegram.error import Forbidden
        from telegram.ext import ExtBot
        from telegram.request import BaseRequest

        class FakeRequest(BaseRequest):
            @property
            def read_timeout(self):
                return 5.0

            async def initialize(self):
                return None

            async def shutdown(self):
                return None

            async def do_request(self, url, method, request_data=None, **kwargs):
                if url.endswith("getMe"):
                    me = {"id": 1, "is_bot": True, "first_name": "Bot", "username": "bot"}
                    return 200, json.dumps({"ok": True, "result": me}).encode()
                body = {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}
                return 403, json.dumps(body).encode()

        marked = []

        async def run():
            bot = ExtBot("1:x", request=FakeRequest(), get_updates_request=FakeRequest(),
                         rate_limiter=telegram_ui.TelegramSafetyLimiter())
            await bot.initialize()
            with self.assertRaises(Forbidden):
                await bot.send_document(chat_id=4242, document=io.BytesIO(b"%PDF-1.4"), filename="slides.pdf")

        with mock.patch.object(telegram_ui, "mark_blocked_chat", lambda chat_id, e: marked.append(chat_id)):
            asyncio.run(run())
        self.assertEqual(marked, [4242])


class InviteHistoryTests(unittest.TestCase):
    def test_every_student_of_a_multi_use_code_is_kept(self):
        code = db.create_invite_token(created_by=1, max_uses=3)
        for uid in (9911, 9912, 9913):
            self.assertTrue(db.redeem_invite_token(code, uid)[0])
        self.assertEqual(db.invite_redemptions(code), [9911, 9912, 9913])
        listed = next(t for t in db.list_invite_tokens() if t["code"] == db.normalize_invite_code(code))
        self.assertEqual(listed["redeemed_by"], [9911, 9912, 9913])
        self.assertEqual(listed["use_count"], 3)

    def test_uses_from_before_the_table_are_carried_over(self):
        code = db.create_invite_token(created_by=1, max_uses=1)
        clean = db.normalize_invite_code(code)
        conn = db._connection()
        conn.execute("UPDATE invite_tokens SET used_by = ?, used_at = ? WHERE code = ?", (9921, 5.0, clean))
        conn.execute("DELETE FROM invite_redemptions WHERE code = ?", (clean,))
        db._migrate_columns(conn)
        db._migrate_columns(conn)  # idempotent
        self.assertEqual(db.invite_redemptions(code), [9921])


class WhatsAppProcessTests(unittest.TestCase):
    def test_a_process_whose_cwd_vanished_is_not_ours(self):
        from unibot import app
        proc = mock.MagicMock()
        proc.cmdline.return_value = ["node", "server.js"]
        proc.cwd.side_effect = FileNotFoundError("cwd removed")
        self.assertFalse(app._is_our_whatsapp_service(proc, "/x"))
        proc.cwd.side_effect = OSError(5, "I/O error")
        self.assertFalse(app._is_our_whatsapp_service(proc, "/x"))


if __name__ == "__main__":
    unittest.main()
