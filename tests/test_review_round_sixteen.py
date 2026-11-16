"""Fixes from the isolation review: a scan shared with every follower skips
folders only some members see, voice notes need the student's OK before they
leave for an outside service, a student's calendar reminders live in their
encrypted record, and the study buddy screen says what the other student sees."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import studip_api, study_plan  # noqa: E402

UID = 9981


def _folder(name, folder_type):
    return {"type": "folder", "name": name, "url": f"https://x/{name}", "folder_type": folder_type}


def _file(name):
    return {"type": "file", "name": name, "url": f"https://x/f/{name}", "size": "1 KB",
            "modified": "-", "modified_iso": None}


class SharedScanFolderTests(unittest.TestCase):
    def _lister(self, tree):
        async def list_files(cid, folder_url=None, session=None, user_id=None):
            return tree[folder_url] if folder_url else tree[None]
        return list_files

    def test_restricted_folder_types_are_recognised(self):
        for kind in ("PermissionEnabledFolder", "HiddenFolder", "CourseGroupFolder", "HomeworkFolder", "TimedFolder"):
            self.assertFalse(studip_api.is_shared_folder(_folder("x", kind)), kind)
        for kind in ("StandardFolder", "MaterialFolder", "RootFolder", "CourseTopicFolder", None):
            self.assertTrue(studip_api.is_shared_folder(_folder("x", kind)), kind)

    def test_the_index_and_topics_scan_leaves_restricted_folders_out(self):
        tree = {None: [_file("root.pdf"), _folder("Slides", "StandardFolder"), _folder("Tutors only", "HiddenFolder"),
                       _folder("Group B", "CourseGroupFolder"), _folder("Unknown", None)],
                "https://x/Slides": [_file("week1.pdf")], "https://x/Tutors only": [_file("solutions.pdf")],
                "https://x/Group B": [_file("groupb.pdf")], "https://x/Unknown": [_file("html-only.pdf")]}
        with mock.patch.object(study_plan, "list_files", self._lister(tree)):
            files = asyncio.run(study_plan._list_course_files("c1"))
        self.assertEqual(sorted(f["name"] for _folder_name, f in files), ["html-only.pdf", "root.pdf", "week1.pdf"])

    def test_the_workload_scan_leaves_them_out_too(self):
        tree = {None: [_folder("20261013", "StandardFolder"), _folder("20261020", "HiddenFolder")],
                "https://x/20261013": [_file("slides.pdf"), _folder("PS", "HomeworkFolder"), _folder("Code", "StandardFolder")],
                "https://x/20261020": [_file("secret.pdf")], "https://x/PS": [_file("hand-ins.pdf")]}
        with mock.patch.object(study_plan, "list_files", self._lister(tree)):
            files = asyncio.run(study_plan._list_course_files_deep("c1"))
        names = sorted(f["name"] for _folder_name, f in files)
        self.assertEqual(names, ["slides.pdf"])

    def test_the_students_own_browsing_still_lists_everything(self):
        async def get_all(path, params=None, session=None, user_id=None):
            if path == "/courses/c1/folders":
                return [{"id": "root", "attributes": {"folder-type": "RootFolder"}, "relationships": {}}], []
            if path == "/folders/root/folders":
                return [{"id": "h", "attributes": {"name": "Tutors only", "folder-type": "HiddenFolder", "chdate": "2026-11-01T10:00:00+01:00"}}], []
            return [], []

        with mock.patch.object(studip_api, "studip_api_get_all", get_all):
            items = asyncio.run(studip_api._list_files_api("c1", None, session=object(), user_id=UID))
        self.assertEqual([(i["name"], i["folder_type"]) for i in items], [("Tutors only", "HiddenFolder")])  # shown, with its type


class VoiceNoteDisclosureTests(unittest.TestCase):
    def test_the_privacy_screen_says_what_happens_to_a_voice_message(self):
        from unibot import privacy
        text, _markup = privacy.privacy_view(UID)
        lowered = text.lower()
        self.assertIn("voice messages need the same permission", lowered)
        self.assertIn("recording itself is sent", lowered)
        for service in ("GWDG Whisper", "Google"):
            self.assertIn(service, text)

    def test_a_voice_message_without_permission_is_refused_before_anything_is_sent(self):
        import types
        from unibot import monitoring
        message = mock.AsyncMock()
        message.voice = types.SimpleNamespace(file_id="f", file_unique_id="u")
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID), message=message,
                                       effective_chat=types.SimpleNamespace(id=UID))
        context = types.SimpleNamespace(user_data={}, bot=mock.AsyncMock())
        with mock.patch.object(monitoring, "is_user_allowed", return_value=True), \
                mock.patch.object(monitoring, "get_ai_personal_enabled", lambda uid: False):
            asyncio.run(monitoring.handle_voice_message(update, context))
        self.assertIn("Privacy", message.reply_text.call_args.args[0])
        context.bot.get_file.assert_not_awaited()  # the recording was never even fetched


class EncryptedRemindersTests(unittest.TestCase):
    def setUp(self):
        from unibot import db
        self.db = db
        self.uid = UID + 1

    def test_reminders_are_stored_encrypted_and_per_student(self):
        from unibot import storage
        storage.save_reminders_cache({"Statistics canaryreminder|1700000000": 1700000000}, user_id=self.uid)
        storage.save_reminders_cache({"Other|1": 1}, user_id=self.uid + 100)
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid), {"Statistics canaryreminder|1700000000": 1700000000})
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid + 100), {"Other|1": 1})
        self.assertFalse(self.db.exists(f"reminders_cache_{self.uid}.json"))  # no plain document
        self.db._connection().execute("PRAGMA wal_checkpoint(FULL)")
        data = b""
        for suffix in ("", "-wal"):
            if os.path.exists(self.db._db_path() + suffix):
                with open(self.db._db_path() + suffix, "rb") as fh:
                    data += fh.read()
        self.assertNotIn(b"canaryreminder", data)  # the title is not in the file in the clear

    def test_a_plain_document_from_before_is_moved_on_first_read(self):
        from unibot import storage
        legacy = f"reminders_cache_{self.uid + 1}.json"
        self.db.save(legacy, {"Lecture|5": 5})
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid + 1), {"Lecture|5": 5})
        self.assertFalse(self.db.exists(legacy))
        self.assertEqual(self.db.get_user_scope(self.uid + 1).private("cache.calendar_reminders"), {"Lecture|5": 5})
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid + 1), {"Lecture|5": 5})  # and it stays

    def test_saving_removes_a_plain_copy_left_over(self):
        from unibot import storage
        legacy = f"reminders_cache_{self.uid + 2}.json"
        self.db.save(legacy, {"Old|1": 1})
        storage.save_reminders_cache({"New|2": 2}, user_id=self.uid + 2)
        self.assertFalse(self.db.exists(legacy))
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid + 2), {"New|2": 2})

    def test_deleting_a_students_data_clears_it(self):
        from unibot import storage
        storage.save_reminders_cache({"Gone|1": 1}, user_id=self.uid + 3)
        self.db.get_user_scope(self.uid + 3).delete_private(keep=(self.db.TASKS_KIND,))
        self.assertEqual(storage.load_reminders_cache(user_id=self.uid + 3), {})

    def test_the_account_without_a_student_keeps_its_shared_document(self):
        from unibot import storage
        storage.save_reminders_cache({"Shared|1": 1})
        self.assertEqual(storage.load_reminders_cache(), {"Shared|1": 1})
        self.assertTrue(self.db.exists(storage.REMINDERS_CACHE_PATH))
        self.db.delete(storage.REMINDERS_CACHE_PATH)


if __name__ == "__main__":
    unittest.main()
