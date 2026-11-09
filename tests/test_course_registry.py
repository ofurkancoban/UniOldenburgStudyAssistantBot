"""Course registry, rotating scanner with failover, and per-user fan-out
(unibot.course_registry and its db tables)."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

from unibot import course_registry as cr  # noqa: E402
from unibot import db  # noqa: E402


class Sess:
    def __init__(self, uid):
        self.user_id = uid


def _clear_tables():
    with db._lock:
        db._connection().execute("DELETE FROM user_courses")
        db._connection().execute("DELETE FROM global_courses")


class RegistryTableTests(unittest.TestCase):
    def setUp(self):
        _clear_tables()

    def test_sync_adds_keeps_prefs_and_drops(self):
        db.get_user_scope(1).sync_courses({"c1", "c2"}, now=100.0)
        db.get_user_scope(1).set_course_pref("c1", "format", "title")
        db.get_user_scope(1).sync_courses({"c1", "c3"}, now=200.0)
        self.assertEqual(db.get_user_scope(1).course_ids(), ["c1", "c3"])
        self.assertEqual(db.get_user_scope(1).course_prefs("c1")["format"], "title")
        self.assertEqual(db.get_user_scope(1).course_prefs("c3"),
                         {"is_muted": False, "notify_files": True, "notify_announcements": True,
                          "notify_forum": True, "format": "silent"})
        self.assertIsNone(db.get_user_scope(1).course_prefs("c2"))

    def test_global_courses_hold_no_user_data(self):
        cols = [r[1] for r in db._connection().execute("PRAGMA table_info(global_courses)")]
        self.assertFalse(any("user" in c for c in cols))
        db.upsert_global_course("c1", "Math", "WiSe 26/27")
        db.upsert_global_course("c1", "Math I")
        db.mark_course_scanned("c1", 7, file_hash="abc", ts=50.0)
        db.mark_course_scanned("c1", 8, forum_hash="def", ts=60.0)
        row = db.get_global_course("c1")
        self.assertEqual((row["title"], row["semester"], row["scanner_uid"], row["file_hash"], row["forum_hash"],
                          row["last_scanned_at"]), ("Math I", "WiSe 26/27", 8, "abc", "def", 60.0))

    def test_bad_pref_values_are_rejected(self):
        db.get_user_scope(1).sync_courses({"c1"})
        with self.assertRaises(ValueError):
            db.get_user_scope(1).set_course_pref("c1", "format", "loud")
        with self.assertRaises(ValueError):
            db.get_user_scope(1).set_course_pref("c1", "user_id; DROP TABLE users", 1)
        self.assertFalse(db.get_user_scope(1).set_course_pref("nope", "is_muted", True))

    def test_registry_sync_only_touches_known_users(self):
        db.get_user_scope(2).sync_courses({"old"})
        stats = cr.sync_registry({"c1": "Math", "c2": "AI"}, {"c1": {1, 2}, "c2": {1}}, known_uids={1})
        self.assertEqual(db.get_user_scope(1).course_ids(), ["c1", "c2"])
        self.assertEqual(db.get_user_scope(2).course_ids(), ["old"])  # user 2's list was not fetched this cycle
        self.assertEqual((stats["courses"], stats["pairs"]), (2, 3))
        self.assertEqual(db.get_global_course("c2")["title"], "AI")


class ScannerTests(unittest.TestCase):
    def setUp(self):
        cr._rotation.clear()
        cr._failed_at.clear()
        cr._candidates.clear()
        cr.failover_count = 0
        self.patch = mock.patch.object(cr, "has_stored_login", lambda s: not getattr(s, "dead", False))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_round_robin_spreads_the_course_across_members(self):
        a, b, c = Sess(1), Sess(2), Sess(3)
        cr.set_candidates("c1", [(1, a), (2, b), (3, c), (1, a)])
        picks = [cr.pick_scanner("c1", now=0) for _ in range(6)]
        self.assertEqual([p.user_id for p in picks], [1, 2, 3, 1, 2, 3])
        self.assertIsNone(cr.pick_scanner("unknown"))

    def test_dead_or_failed_sessions_are_skipped(self):
        a, b, c = Sess(1), Sess(2), Sess(3)
        b.dead = True
        cr.set_candidates("c1", [(1, a), (2, b), (3, c)])
        cr.report_failure(c, now=1000.0)
        self.assertEqual({cr.pick_scanner("c1", now=1500.0).user_id for _ in range(4)}, {1})
        # after the cooldown the failed member is back in the rotation
        later = 1000.0 + cr.SCANNER_FAILURE_COOLDOWN + 1
        self.assertEqual({cr.pick_scanner("c1", now=later).user_id for _ in range(4)}, {1, 3})

    def test_failover_hands_the_scan_to_the_next_member(self):
        a, b = Sess(1), Sess(2)
        cr.set_candidates("c1", [(1, a), (2, b)])
        tried = []

        async def fetch(sess):
            tried.append(sess.user_id)
            if sess is a:
                raise PermissionError("cookie expired")
            return ["item"]

        result, used = asyncio.run(cr.scan_with_failover("c1", a, fetch))
        self.assertEqual((result, used, tried), (["item"], b, [1, 2]))
        self.assertIn(1, cr._failed_at)
        self.assertEqual(cr.failover_count, 1)
        # the failed member is skipped next cycle
        self.assertIs(cr.pick_scanner("c1"), b)

    def test_every_member_failing_raises(self):
        a, b = Sess(1), Sess(2)
        cr.set_candidates("c1", [(1, a), (2, b)])

        async def fetch(sess):
            raise RuntimeError(f"down {sess.user_id}")

        with self.assertRaisesRegex(RuntimeError, "down 2"):
            asyncio.run(cr.scan_with_failover("c1", a, fetch))

    def test_savings_line(self):
        line = cr.savings_line({"courses": 18, "pairs": 300})
        self.assertIn("18 course scans per cycle for 300 follower pairs (282 saved, 94%)", line)
        self.assertIsNone(cr.savings_line({"courses": 0, "pairs": 0}))
        self.assertEqual(cr.content_hash([{"b": 1, "a": 2}]), cr.content_hash([{"a": 2, "b": 1}]))


class FanOutTests(unittest.TestCase):
    def setUp(self):
        _clear_tables()
        for uid in (1, 2, 3, 4, 5):
            db.get_user_scope(uid).sync_courses({"c1"})
        db.get_user_scope(1).set_course_pref("c1", "format", "full")
        db.get_user_scope(2).set_course_pref("c1", "format", "title")
        db.get_user_scope(4).set_course_pref("c1", "is_muted", True)
        db.get_user_scope(5).set_course_pref("c1", "notify_files", False)
        self.sent = []
        self.docs = []
        outer = self

        class Bot:
            async def send_message(self, chat_id, text, disable_notification, **kwargs):
                outer.sent.append((chat_id, text, disable_notification))

        self.bot = Bot()
        self.patches = [
            mock.patch.object(cr, "broadcast_targets", lambda cid, members: set(members)),
            mock.patch.object(cr.asyncio, "sleep", mock.AsyncMock()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_each_follower_gets_their_format(self):
        counts = asyncio.run(cr.fan_out(self.bot, "c1", "files", "FULL", "TITLE", members={1, 2, 3, 4, 5, 6}))
        self.assertEqual(sorted(self.sent), [(1, "FULL", False), (2, "TITLE", False), (3, "FULL", True),
                                             (6, "FULL", True)])
        self.assertEqual(counts, {"full": 1, "title": 1, "silent": 2, "skipped": 2, "queued": 0})

    def test_kind_switches_only_affect_that_kind(self):
        asyncio.run(cr.fan_out(self.bot, "c1", "announcements", "FULL", "TITLE", members={5}))
        self.assertEqual(self.sent, [(5, "FULL", True)])

    def test_upload_file_delivery_uses_the_rich_sender(self):
        from unibot import preferences
        preferences.set_pref(1, "file_delivery", "upload")
        self.addCleanup(preferences.set_pref, 1, "file_delivery", "card")
        used = []

        async def send_full(bot, uid):
            used.append(uid)
            return True

        asyncio.run(cr.fan_out(self.bot, "c1", "files", "FULL", "TITLE", members={1, 3}, send_full=send_full))
        self.assertEqual(used, [1])
        self.assertEqual(self.sent, [(3, "FULL", True)])

    def test_rich_sender_can_fall_back_to_the_card(self):
        async def send_full(bot, uid):
            return False

        asyncio.run(cr.fan_out(self.bot, "c1", "files", "FULL", "TITLE", members={1}, send_full=send_full))
        self.assertEqual(self.sent, [(1, "FULL", False)])


class QuietHoursAndDigestTests(unittest.TestCase):
    """Held course alerts (quiet hours, evening digest) and link-only files."""

    def setUp(self):
        from unibot import preferences
        self.prefs = preferences
        _clear_tables()
        for uid in (21, 22, 23):
            db.get_user_scope(uid).sync_courses({"c1"})
            db.get_user_scope(uid).delete_private("alert_queue")
        preferences.set_pref(21, "quiet_hours", {"start": "23:00", "end": "08:00"})
        preferences.set_pref(22, "delivery_mode", "digest")
        preferences.set_pref(23, "file_delivery", "link")
        self.sent = []
        outer = self

        class Bot:
            async def send_message(self, chat_id, text, disable_notification=None, reply_markup=None, **kw):
                outer.sent.append((chat_id, text, reply_markup))

        self.bot = Bot()
        for p in (mock.patch.object(cr, "broadcast_targets", lambda cid, members: set(members)),
                  mock.patch.object(cr.asyncio, "sleep", mock.AsyncMock())):
            p.start()
            self.addCleanup(p.stop)

    def _at(self, hh, mm=0):
        from datetime import datetime
        return datetime(2026, 11, 10, hh, mm, tzinfo=cr.TZ_BERLIN)

    def test_alerts_wait_for_quiet_hours_and_the_digest(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        card = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download", callback_data="file|abc")]])
        counts = asyncio.run(cr.fan_out(self.bot, "c1", "files", "FULL", "📄 Stats: sheet.pdf", reply_markup=card,
                                        members={21, 22, 23}, now=self._at(23, 30)))
        self.assertEqual(counts["queued"], 2)           # 21 is asleep, 22 wants the digest
        self.assertEqual([c for c, _t, _m in self.sent], [23])
        self.assertEqual(self.sent[0][2].inline_keyboard[0][0].url,
                         "https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid=c1")  # link only
        self.sent.clear()
        self.assertEqual(asyncio.run(cr.flush_alert_queues(self.bot, self._at(6, 0))), 0)   # still quiet
        self.assertEqual(asyncio.run(cr.flush_alert_queues(self.bot, self._at(8, 5))), 1)   # 21 after 07:00 + quiet end
        uid, text, markup = self.sent[0]
        self.assertEqual(uid, 21)
        self.assertIn("📄 Stats: sheet.pdf", text)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "file|abc")  # download kept
        self.assertEqual(asyncio.run(cr.flush_alert_queues(self.bot, self._at(12, 0))), 0)  # 22 waits for 18:00
        self.assertEqual(asyncio.run(cr.flush_alert_queues(self.bot, self._at(18, 1))), 1)
        self.assertEqual(self.sent[-1][0], 22)
        self.assertEqual(asyncio.run(cr.flush_alert_queues(self.bot, self._at(18, 30))), 0)  # sent once

    def test_muted_course_is_not_queued_either(self):
        db.get_user_scope(21).set_course_pref("c1", "is_muted", True)
        counts = asyncio.run(cr.fan_out(self.bot, "c1", "files", "FULL", "T", members={21}, now=self._at(23, 30)))
        self.assertEqual((counts["queued"], counts["skipped"]), (0, 1))


class CourseAlertsUiTests(unittest.TestCase):
    def setUp(self):
        _clear_tables()
        db.upsert_global_course("c1", "Statistics")
        db.get_user_scope(1).sync_courses({"c1"})

    def _tap(self, data):
        query = mock.MagicMock()
        query.from_user.id = 1
        query.data = data
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        with mock.patch.object(cr, "is_user_allowed", lambda uid: True):
            asyncio.run(cr.handle_course_alerts(update, mock.MagicMock()))
        return query

    def test_toggles_and_format_cycle(self):
        q = self._tap("calert")
        self.assertIn("Statistics", str(q.message.reply_text.call_args))
        self._tap("calert|t|m|c1")
        self._tap("calert|t|p|c1")
        q = self._tap("calert|fmt|c1")
        prefs = db.get_user_scope(1).course_prefs("c1")
        self.assertEqual((prefs["is_muted"], prefs["notify_forum"], prefs["format"]), (True, False, "full"))
        self.assertIn("Statistics", q.edit_message_text.call_args.args[0])
        self._tap("calert|fmt|c1")
        self.assertEqual(db.get_user_scope(1).course_prefs("c1")["format"], "title")

    def test_unknown_course_shows_the_list(self):
        q = self._tap("calert|t|m|other")
        self.assertIsNone(db.get_user_scope(1).course_prefs("other"))
        self.assertIn("Course alerts", q.edit_message_text.call_args.args[0])


class FullFileSenderTests(unittest.TestCase):
    def test_file_is_downloaded_once_then_sent_by_file_id(self):
        from unibot import monitoring_alerts as ma
        gets = []

        class Resp:
            status = 200

            async def read(self):
                return b"%PDF-1.4"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Session:
            async def get(self, url):
                gets.append(url)
                return Resp()

        docs = []

        class Bot:
            async def send_document(self, chat_id, document, **kwargs):
                docs.append((chat_id, document if isinstance(document, str) else "bytes"))
                return mock.MagicMock(document=mock.MagicMock(file_id="FILEID"))

        it = {"url": "https://x/file", "name": "Sheet 1.pdf"}
        with mock.patch.object(ma, "get_user_session", lambda uid: Session()):
            send = ma._full_file_sender(it, "card", None, 1)
            self.assertTrue(asyncio.run(send(Bot(), 1)))
            self.assertTrue(asyncio.run(send(Bot(), 2)))
        self.assertEqual(gets, ["https://x/file"])
        self.assertEqual(docs, [(1, "bytes"), (2, "FILEID")])

    def test_missing_url_falls_back_to_the_card(self):
        from unibot import monitoring_alerts as ma
        send = ma._full_file_sender({"name": "x.pdf"}, "card", None, 1)
        self.assertFalse(asyncio.run(send(mock.MagicMock(), 1)))


if __name__ == "__main__":
    unittest.main()
