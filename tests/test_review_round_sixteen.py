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


class StudyBuddyDisclosureTests(unittest.TestCase):
    def test_the_screen_says_what_a_classmate_sees(self):
        from unibot import study_week
        for enabled in (False, True):
            with mock.patch.object(study_week, "get_study_buddy_enabled", lambda uid, enabled=enabled: enabled), \
                    mock.patch.object(study_week, "study_buddy_matches", mock.AsyncMock(return_value=[])):
                text, _markup = asyncio.run(study_week.study_buddy_view(UID))
            self.assertIn("never your timetable", text)
            self.assertIn("first name", text)
            self.assertIn("up to two courses you share", text)
            self.assertIn("Turn it off", text)

    def test_what_the_screen_promises_is_what_is_shown(self):
        from datetime import datetime
        from unibot import study_week
        from studip_utils import TZ_BERLIN
        start = datetime(2026, 11, 16, 10, tzinfo=TZ_BERLIN)
        match = {"user_id": 5, "name": "Maria Secret-Surname", "courses": ["A", "B", "C"],
                 "windows": [{"start_dt": start, "end_dt": start.replace(hour=12), "minutes": 120, "spot": None}]}
        text = "\n".join(study_week.format_buddy_lines([match]))
        self.assertIn("Maria", text)
        self.assertNotIn("Secret-Surname", text)  # the first name only
        self.assertIn("A, B", text)
        self.assertNotIn("C", text.replace("Common", ""))  # two courses at most


class AdministratorDefaultTests(unittest.TestCase):
    def _admins(self, allowed, admin=""):
        import subprocess
        env = {**os.environ, "ALLOWED_USER_IDS": allowed, "ADMIN_USER_IDS": admin,
               "BOT_LOG_FILE": os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")}
        out = subprocess.run([sys.executable, "-c", "import unibot.config as c; print(sorted(c.ADMIN_USER_IDS))"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        return out.stdout.strip().splitlines()[-1]

    def test_without_admin_ids_only_the_first_allowed_account_is_an_administrator(self):
        self.assertEqual(self._admins("5,6"), "[5]")
        self.assertEqual(self._admins("7"), "[7]")
        self.assertEqual(self._admins(""), "[]")

    def test_explicit_admin_ids_win(self):
        self.assertEqual(self._admins("5,6", "6"), "[6]")
        self.assertEqual(self._admins("5,6", "5,6"), "[5, 6]")

    def test_an_allowed_but_ordinary_account_is_not_protected_from_demotion(self):
        from unibot import system
        with mock.patch.object(system, "ADMIN_USER_IDS", {5}), mock.patch.object(system, "PRIMARY_ADMIN_ID", 5):
            self.assertTrue(system._is_protected_admin(5))
            self.assertFalse(system._is_protected_admin(6))


class SignedOutStatusTests(unittest.TestCase):
    NAME = "Canary Student"

    def _status(self, *, admin, connected):
        from unibot import storage, system

        class Session:
            user_id = UID + 5

            async def is_logged_in(self):
                return connected

        storage.save_user_name(UID + 5, self.NAME)
        with mock.patch.object(system, "is_user_admin", return_value=admin), \
                mock.patch.object(system, "get_user_session", lambda uid=None: Session()), \
                mock.patch.object(system, "get_studip_user_profile", mock.AsyncMock(return_value={"formatted_name": self.NAME})), \
                mock.patch.object(system, "_login_public_url", lambda: "http://localhost"), \
                mock.patch.object(system, "shared_http", mock.MagicMock(side_effect=RuntimeError("no WhatsApp in a test"))):
            text, markup = asyncio.run(system.build_status_view(UID + 5))
        return text, markup

    def test_a_student_signed_out_sees_no_name(self):
        text, _ = self._status(admin=False, connected=False)
        self.assertNotIn(self.NAME, text)
        self.assertIn("Not connected", text)

    def test_a_student_signed_in_sees_the_name_but_no_internals(self):
        text, _ = self._status(admin=False, connected=True)
        self.assertIn("Connected", text)
        for internal in ("Self-Healing", "OAuth2", "Cookie"):
            self.assertNotIn(internal, text)

    def test_an_administrator_signed_out_sees_no_name_either(self):
        text, _ = self._status(admin=True, connected=False)
        self.assertNotIn(self.NAME, text)
        self.assertIn("Name: Not connected", text)

    def test_an_administrator_signed_in_sees_the_name_and_the_bot_status(self):
        text, markup = self._status(admin=True, connected=True)
        self.assertIn(self.NAME, text)
        self.assertIn("Bot Status", text)
        self.assertIn("Admin Dashboard", " ".join(b.text for row in markup.inline_keyboard for b in row))

    def test_the_student_screen_has_nothing_of_the_administrator(self):
        text, markup = self._status(admin=False, connected=True)
        buttons = " ".join(b.text for row in markup.inline_keyboard for b in row)
        for admin_only in ("Bot Status", "Unified Watcher", "WA Group", "Bot Memory", "Admin Dashboard", "WhatsApp", "Wardrobe"):
            self.assertNotIn(admin_only, text + " " + buttons, admin_only)


class LogoutForgetsTheNameTests(unittest.TestCase):
    def test_logout_drops_the_name_and_the_old_status_message_is_redrawn(self):
        import types
        from unibot import menus, state, storage, system
        uid = UID + 6
        storage.save_user_name(uid, "Canary Leaver")
        self.assertEqual(storage.get_user_name(uid), "Canary Leaver")

        class Session:
            user_id = uid
            logged_in = True

            async def logout(self):
                Session.logged_in = False

            async def close(self):
                pass

            async def is_logged_in(self):
                return Session.logged_in

        fake = Session()
        query = mock.AsyncMock()
        query.message.text = "old status"
        query.message.text_html = "old <b>status</b> Canary Leaver"
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid), callback_query=query)
        with mock.patch.dict(state.user_sessions, {uid: fake}), \
                mock.patch.object(menus, "is_user_allowed", return_value=True), \
                mock.patch.object(system, "get_user_session", lambda user_id=None: fake), \
                mock.patch.object(system, "is_user_admin", return_value=False), \
                mock.patch.object(system, "_login_public_url", lambda: "http://localhost"):
            asyncio.run(menus._status_button_studip_logout(update, None, query))
        self.assertIsNone(storage.get_user_name(uid))
        edited = query.edit_message_text.call_args.args[0]
        self.assertNotIn("Canary Leaver", edited)
        self.assertIn("Not connected", edited)
        self.assertNotIn("Disconnect", " ".join(b.text for row in query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row))
        self.assertIn("Logged out", query.message.reply_text.call_args.args[0])


class InviteReplyThrottleTests(unittest.TestCase):
    def setUp(self):
        from unibot import auth
        auth._reset_gate_replies()
        self.addCleanup(auth._reset_gate_replies)

    def test_a_stranger_is_answered_once_even_across_a_restart(self):
        from unibot import auth
        self.assertTrue(auth._gate_reply_due(424242))
        self.assertFalse(auth._gate_reply_due(424242))
        auth._gate_replied.clear()  # a restart forgets the memory, not the record
        self.assertFalse(auth._gate_reply_due(424242))
        self.assertTrue(auth._gate_reply_due(424243))  # another account is its own

    def test_the_stranger_is_logged_and_stored_only_as_a_hash(self):
        from unibot import auth, db
        with self.assertLogs(level="INFO") as logs:
            auth._gate_reply_due(424244)
        text = " ".join(logs.output)
        self.assertIn("Invite-only reply", text)
        self.assertNotIn("424244", text)
        self.assertNotIn("424244", str(db.load(auth.GATE_REPLIES_PATH)))


if __name__ == "__main__":
    unittest.main()
