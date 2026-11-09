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


if __name__ == "__main__":
    unittest.main()
