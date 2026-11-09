"""Personal and shared data stay apart: per-student encryption, the private
data table, scoped sessions and personal-only senders."""

import asyncio
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

import studip_vault  # noqa: E402
from unibot import db  # noqa: E402


def _raw(table: str, user_id: int, key_col: str, key: str) -> str:
    return db._connection().execute(
        f"SELECT encrypted_blob FROM {table} WHERE user_id = ? AND {key_col} = ?", (user_id, key),
    ).fetchone()[0]


class PerUserVaultTests(unittest.TestCase):
    def test_a_blob_only_opens_for_its_student(self):
        blob = studip_vault.encrypt_user_payload(11, {"grade": "1.3"})
        self.assertTrue(blob.startswith(studip_vault.USER_MAGIC))
        self.assertNotIn("1.3", blob)
        self.assertEqual(studip_vault.decrypt_user_payload(11, blob), {"grade": "1.3"})
        with self.assertLogs("StudIPVault", level=logging.ERROR):
            self.assertIsNone(studip_vault.decrypt_user_payload(12, blob))
        # a fresh salt per record: the same data never gives the same blob
        self.assertNotEqual(blob, studip_vault.encrypt_user_payload(11, {"grade": "1.3"}))

    def test_user_id_is_required(self):
        for bad in (None, "11", True):
            with self.assertRaises(ValueError):
                studip_vault.encrypt_user_payload(bad, {})
            with self.assertRaises(ValueError):
                db.save_private(bad, "exam_cache", {})
            with self.assertRaises(ValueError):
                db.load_private(bad, "exam_cache")

    def test_shared_key_credentials_move_to_the_students_key(self):
        legacy = studip_vault.encrypt_payload([{"name": "Seminar_Session", "value": "abc"}])
        with db._lock:
            db._connection().execute(
                "INSERT OR REPLACE INTO user_credentials (user_id, credential_type, encrypted_blob, updated_at) "
                "VALUES (?, ?, ?, 0)", (21, "cookies", legacy))
        self.assertEqual(db.load_user_credential(21, "cookies")[0]["value"], "abc")
        migrated = _raw("user_credentials", 21, "credential_type", "cookies")
        self.assertTrue(migrated.startswith(studip_vault.USER_MAGIC))
        self.assertEqual(db.load_user_credential(21, "cookies")[0]["value"], "abc")

    def test_a_row_copied_to_another_student_does_not_open(self):
        db.save_user_credential(31, "cookies", [{"value": "secret"}])
        stolen = _raw("user_credentials", 31, "credential_type", "cookies")
        with db._lock:
            db._connection().execute(
                "INSERT OR REPLACE INTO user_credentials (user_id, credential_type, encrypted_blob, updated_at) "
                "VALUES (?, ?, ?, 0)", (32, "cookies", stolen))
        with self.assertLogs("StudIPVault", level=logging.ERROR):
            self.assertIsNone(db.load_user_credential(32, "cookies"))


class PrivateStoreTests(unittest.TestCase):
    def test_exam_cache_is_private_and_encrypted(self):
        import exam_reminder as er
        er.save_exam_cache({"known_grades": {"MOD1": "1.3|passed"}}, user_id=41)
        self.assertFalse(db.exists(er.exam_cache_path(41)))  # not in the shared document store
        raw = _raw("user_private_data", 41, "kind", er.PRIVATE_EXAM_KIND)
        self.assertNotIn("1.3", raw)
        self.assertEqual(er.load_exam_cache(41)["known_grades"], {"MOD1": "1.3|passed"})
        self.assertEqual(er.load_exam_cache(4999)["known_grades"], {})  # another student sees nothing
        with er.exam_cache_user(41):
            self.assertEqual(er.load_exam_cache()["known_grades"], {"MOD1": "1.3|passed"})

    def test_per_user_document_moves_into_the_private_table(self):
        import exam_reminder as er
        db.save(er.exam_cache_path(43), {"known_grades": {"X": 1}})
        self.assertEqual(er.load_exam_cache(43)["known_grades"], {"X": 1})
        self.assertFalse(db.exists(er.exam_cache_path(43)))
        self.assertTrue(db.has_private(43, er.PRIVATE_EXAM_KIND))

    def test_delete_private(self):
        db.save_private(44, "a", 1)
        db.save_private(44, "b", 2)
        db.delete_private(44, "a")
        self.assertEqual((db.load_private(44, "a"), db.load_private(44, "b")), (None, 2))
        db.delete_private(44)
        self.assertFalse(db.has_private(44, "b"))


class ScopedSessionTests(unittest.TestCase):
    def test_personal_session_never_falls_back(self):
        from unibot import auth
        with self.assertRaises(ValueError):
            auth.personal_session(None)
        sentinel = object()
        with mock.patch.object(auth, "get_user_session", lambda uid: sentinel if uid == 5 else None):
            self.assertIs(auth.personal_session(5), sentinel)

    def test_documents_use_the_tapping_students_session(self):
        from unibot import exams
        used = []

        async def download(session, key):
            used.append(session)
            return b"%PDF", "doc.pdf"

        query = mock.MagicMock()
        query.from_user.id = 77
        query.data = "stums_doc|imma"
        query.answer = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        query.message.reply_document = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        own = object()
        with mock.patch.object(exams, "is_user_allowed", lambda uid: True), \
                mock.patch.object(exams, "personal_session", lambda uid: own if uid == 77 else None), \
                mock.patch.object(exams, "download_stums_document", download), \
                mock.patch.object(exams, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(exams, "_stop_loading_animation", mock.AsyncMock()):
            asyncio.run(exams.handle_stums_doc_download(update, mock.MagicMock()))
        self.assertEqual(used, [own])
        sent = query.message.reply_document.call_args.kwargs["document"]
        self.assertEqual(sent.getvalue(), b"%PDF")  # streamed from memory, no file on disk


class PersonalSenderTests(unittest.TestCase):
    def test_single_account_alerts_reach_only_that_student(self):
        from unibot import watcher
        sent = []

        class Bot:
            async def send_message(self, chat_id, **kwargs):
                sent.append(chat_id)

        sess = mock.MagicMock(user_id=9)
        self.assertEqual(watcher._single_account_owner(sess), 9)
        asyncio.run(watcher._user_sender(9)(Bot(), "🎓 Grade: 1.3"))
        self.assertEqual(sent, [9])

    def test_grade_notifications_are_not_logged(self):
        import exam_reminder as er
        with open(er.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("Notified: grade result for", source)


class PrivacyScreenTests(unittest.TestCase):
    def setUp(self):
        from unibot import privacy, storage
        self.privacy, self.storage = privacy, storage
        self.cache = {}
        self.patches = [
            mock.patch.object(privacy, "load_general_cache", lambda: dict(self.cache)),
            mock.patch.object(privacy, "update_general_cache", lambda d: self.cache.update(d)),
            mock.patch.object(storage, "load_general_cache", lambda: self.cache),
            mock.patch.object(storage, "save_general_cache", lambda d: self.cache.update(d)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_view_shows_the_students_own_times(self):
        from datetime import datetime
        from unibot import stagger
        now = datetime(2026, 11, 9, 10, 0, tzinfo=self.privacy.TZ_BERLIN)
        text, markup = self.privacy.privacy_view(501, now)
        self.assertIn(stagger.describe_slots(501, now.date(), False), text)
        self.assertIn("2 a day", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, ["privacy|check", "privacy|ai", "privacy|delete"])

    def test_check_now_is_rate_limited_and_needs_a_login(self):
        runs = []

        async def checks(bot, session, send):
            runs.append(session)

        own = mock.MagicMock()
        with mock.patch.object(self.privacy, "personal_session", lambda uid: own), \
                mock.patch.object(self.privacy, "has_stored_login", lambda s: True), \
                mock.patch.object(self.privacy, "_run_stums_checks", checks), \
                mock.patch.object(self.privacy, "mark_personal_check", lambda uid: None):
            first = asyncio.run(self.privacy.run_manual_check(None, 502))
            second = asyncio.run(self.privacy.run_manual_check(None, 502))
        self.assertTrue(first.startswith("✅"))
        self.assertTrue(second.startswith("⏳"))
        self.assertEqual(runs, [own])
        with mock.patch.object(self.privacy, "personal_session", lambda uid: own), \
                mock.patch.object(self.privacy, "has_stored_login", lambda s: False):
            self.assertIn("Sign in", asyncio.run(self.privacy.run_manual_check(None, 503)))

    def test_ai_grades_are_opt_in(self):
        self.assertFalse(self.storage.get_ai_grades_enabled(504))
        self.storage.set_ai_grades_enabled(504, True)
        self.assertTrue(self.storage.get_ai_grades_enabled(504))

    def test_assistant_context_leaves_grades_out_without_opt_in(self):
        from unibot import voice
        fetched = []

        async def grades(session):
            fetched.append(session)
            return []

        async def login(user_id=None):
            return object()

        async def empty(*a, **k):
            return []

        with mock.patch.object(voice, "login_studip", login), \
                mock.patch.object(voice, "get_grades", grades), \
                mock.patch.object(voice, "get_registered_exam_schedule", empty), \
                mock.patch.object(voice, "get_ai_grades_enabled", lambda uid: False):
            context = asyncio.run(voice.build_qa_context(user_id=505))
        self.assertEqual(fetched, [])
        self.assertIn("not shared with the assistant", context)

    def test_delete_my_data(self):
        import exam_reminder as er
        from unibot import exams
        uid = 506
        er.save_exam_cache({"known_grades": {"M": "1.0"}}, user_id=uid)
        db.sync_user_courses(uid, {"c1"})
        self.cache.update({"study_blocks": {str(uid): {"1": {}}, "9": {"1": {}}}, "ai_grades": {str(uid): True}})
        exams.exam_action_cache["s1"] = {"user_id": uid}
        exams.exam_action_cache["s2"] = {"user_id": 9}
        logged_out = []

        async def logout(user_id=None):
            logged_out.append(user_id)

        with mock.patch.object(self.privacy, "_perform_logout", logout):
            asyncio.run(self.privacy.delete_personal_data(uid))
        self.assertEqual(logged_out, [uid])
        self.assertFalse(db.has_private(uid, er.PRIVATE_EXAM_KIND))
        self.assertEqual(db.user_course_ids(uid), [])
        self.assertNotIn(str(uid), self.cache["study_blocks"])
        self.assertIn("9", self.cache["study_blocks"])  # other students untouched
        self.assertNotIn("s1", exams.exam_action_cache)
        self.assertIn("s2", exams.exam_action_cache)
        exams.exam_action_cache.pop("s2", None)

    def test_delete_asks_first(self):
        query = mock.MagicMock()
        query.from_user.id = 507
        query.data = "privacy|delete"
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        update = mock.MagicMock(callback_query=query)
        deleted = []

        async def delete(uid):
            deleted.append(uid)

        with mock.patch.object(self.privacy, "is_user_allowed", lambda uid: True), \
                mock.patch.object(self.privacy, "delete_personal_data", delete):
            asyncio.run(self.privacy.handle_privacy(update, mock.MagicMock()))
            self.assertEqual(deleted, [])
            self.assertIn("cannot be undone", query.edit_message_text.call_args.args[0])
            query.data = "privacy|delete_yes"
            asyncio.run(self.privacy.handle_privacy(update, mock.MagicMock()))
        self.assertEqual(deleted, [507])


class PrivacyDigestTests(unittest.TestCase):
    def test_digest_line_has_counts_only(self):
        from datetime import datetime
        from unibot import watcher
        db.save_user_credential(601, "cookies", [{"value": "x"}])
        with mock.patch.object(watcher, "load_general_cache",
                               lambda: {"personal_check_last": {"601": datetime(2026, 11, 9, 9).timestamp()}}):
            line = watcher._privacy_digest_line(datetime(2026, 11, 9, 12, tzinfo=watcher.TZ_BERLIN))
        self.assertIn("logins under per-student keys", line)
        self.assertIn("1 students checked in 24 h", line)
        self.assertNotIn("601", line)


if __name__ == "__main__":
    unittest.main()
