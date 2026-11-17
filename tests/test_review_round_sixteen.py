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
from tests.testenv import run_as, use_temp_db  # noqa: E402
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


class _Resp:
    def __init__(self, status, text="", url="https://stums.uni-oldenburg.de/qisserver/page", headers=None):
        self.status, self._text, self.url, self.headers = status, text, url, headers or {}

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StumsSession:
    """A signed-in session whose StuMS pages all answer with `status`."""
    user_id = UID + 30

    def __init__(self, status=403, text="<html><title>403 Forbidden</title></html>"):
        import studip_session
        self.status, self.text = status, text
        self.stums = studip_session.StumsState()
        self.explicitly_logged_out = False
        self.handoffs = 0
        jar = types_namespace(filter_cookies=lambda url: {}, clear_domain=lambda host: None)
        self.session = types_namespace(cookie_jar=jar)

    async def get(self, url, **kwargs):
        return _Resp(self.status, self.text, headers={"Server": "Apache/2.4", "X-Proxy-Upstream": "1"})

    async def sso_handoff(self, entry, host, referer=None):
        self.handoffs += 1
        return "https://stums.uni-oldenburg.de/qisserver/pages/cs/sys/portal/hisinoneStartPage.faces"


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


class StumsRefusalTests(unittest.TestCase):
    def test_a_403_is_a_refused_session_and_the_log_says_who_refused(self):
        import stums_client
        session = _StumsSession()
        session.stums.verified_at = 123.0
        with self.assertLogs(level="WARNING") as logs, self.assertRaises(stums_client.StumsPageRefused) as caught:
            asyncio.run(stums_client._read_stums_page(session, stums_client.STUDY_PLANNER_URL, "r"))
        self.assertEqual(str(caught.exception), "StuMS page answered HTTP 403")  # the message logs always had
        text = " ".join(logs.output)
        for detail in ("403 Forbidden", "Apache/2.4", "via worker '1'"):
            self.assertIn(detail, text)
        self.assertEqual(session.stums.verified_at, 0.0)  # not trusted any more

    def test_other_errors_are_not_a_refused_session(self):
        import stums_client
        session = _StumsSession(status=500, text="boom")
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(stums_client._read_stums_page(session, stums_client.STUDY_PLANNER_URL, "r"))
        self.assertNotIsInstance(caught.exception, stums_client.StumsPageRefused)

    def test_a_refused_session_pauses_background_checks_instead_of_signing_in_again_every_time(self):
        import stums_client
        session = _StumsSession()
        with stums_client.background_stums():
            with self.assertRaises(stums_client.StumsLoginUnavailable):
                asyncio.run(stums_client._establish_stums_session(session))
            tried = session.handoffs
            self.assertGreaterEqual(tried, 1)
            self.assertGreater(session.stums.blocked_until, 0)
            with self.assertRaises(stums_client.StumsLoginUnavailable) as again:
                asyncio.run(stums_client._establish_stums_session(session))
        self.assertTrue(again.exception.quiet)  # the pause is on: quiet, and no new sign-in attempt
        self.assertEqual(session.handoffs, tried)


class StumsReconnectNoticeTests(unittest.TestCase):
    def setUp(self):
        from tests.testenv import activate_student
        self.uid = UID + 31
        activate_student(self.uid)

    def _notice(self):
        from unibot import auth
        bot = mock.AsyncMock()
        server = mock.MagicMock()
        server.start = mock.AsyncMock()
        server.app_url = lambda target="": f"https://bot.example/app?target={target}"
        with mock.patch.object(auth, "_get_login_server", lambda: server), \
                mock.patch.object(auth, "_login_public_url", lambda: "https://bot.example"):
            sent = run_as(self.uid, auth.notify_stums_reconnect(bot, self.uid))
        return sent, bot

    def test_the_notice_comes_with_a_reconnect_button_and_not_again_for_half_a_day(self):
        from unibot import storage
        storage.delete_personal_record(self.uid, "stums_reconnect_notice")
        sent, bot = self._notice()
        self.assertTrue(sent)
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn("StuMS needs a fresh sign-in", kwargs["text"])
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🎓 Reconnect StuMS")
        self.assertTrue(button.web_app.url.endswith("target=stums"))
        sent_again, bot_again = self._notice()
        self.assertFalse(sent_again)
        bot_again.send_message.assert_not_awaited()

    def test_the_notice_only_comes_after_automatic_sign_in_failed_for_hours(self):
        from unibot import watcher
        import time as _time
        for case in ("first failure", "failing for hours", "not paused"):
            session = _StumsSession()
            session.user_id = self.uid
            session.stums.blocked_until = 0.0 if case == "not paused" else _time.time() + 600
            if case == "failing for hours":
                session.stums.failing_since = _time.time() - watcher.STUMS_NOTICE_AFTER_SECONDS - 60
            notice = mock.AsyncMock()
            with mock.patch.object(watcher, "check_exam_reminders", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(watcher, "check_grade_reminders", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(watcher, "check_tuition_reminders", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(watcher, "notify_stums_reconnect", notice):
                with self.assertRaises(RuntimeError):
                    run_as(self.uid, watcher._run_stums_checks(mock.AsyncMock(), session, mock.AsyncMock()))
            self.assertEqual(notice.await_count, 1 if case == "failing for hours" else 0, case)


class CookieFlagTests(unittest.TestCase):
    def test_a_cookie_with_an_unknown_flag_is_kept(self):
        from http.cookies import SimpleCookie
        import studip_session
        header = "JSESSIONID=abc; Path=/qisserver; Secure; HttpOnly; SameSite=Lax; Partitioned"
        self.assertEqual(len(SimpleCookie(header)), 0)  # the stock parser loses it
        jar = SimpleCookie()
        jar.load(studip_session._strip_unknown_cookie_flags(header))
        self.assertEqual(jar["JSESSIONID"].value, "abc")


class StumsSkipsTheWorkerTests(unittest.TestCase):
    def test_stums_goes_direct_unless_asked_otherwise(self):
        import os
        import studip_session
        sess = studip_session.StudIPSession.__new__(studip_session.StudIPSession)
        host = "stums.uni-oldenburg.de"
        with mock.patch.object(studip_session, "_worker_url", return_value="https://w.example"), \
                mock.patch.object(studip_session, "_worker_available", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STUMS_VIA_WORKER", None)
            self.assertFalse(sess._via_worker(host))
            self.assertTrue(sess._via_worker("elearning.uni-oldenburg.de"))
            os.environ["STUMS_VIA_WORKER"] = "1"
            self.assertTrue(sess._via_worker(host))


class StumsNavigationHeadersTests(unittest.TestCase):
    def test_pages_are_requested_like_a_browser_click(self):
        import stums_client

        seen = {}

        class Resp:
            status = 200
            url = "https://stums.uni-oldenburg.de/qisserver/x"
            headers = {}

            async def text(self):
                return "<html>ok</html>"

            def release(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Sess:
            async def get(self, url, **kw):
                seen.update(kw.get("headers") or {})
                return Resp()

        asyncio.run(stums_client._read_stums_page(Sess(), "https://stums.uni-oldenburg.de/qisserver/x",
                                                  "https://stums.uni-oldenburg.de/qisserver/start"))
        self.assertEqual(seen["Sec-Fetch-Mode"], "navigate")
        self.assertEqual(seen["Sec-Fetch-Site"], "same-origin")
        self.assertIn("text/html", seen["Accept"])
        self.assertTrue(seen["Referer"].endswith("/start"))


class StumsLoginPageDetectionTests(unittest.TestCase):
    SIGNED_IN = ('<html><title>Planner of studies</title><form id="sessionTimeoutLoginForm" action="/x">'
                 '<input id="sessionTimeoutLoginForm:login"></form>'
                 '<form action="/qisserver/rds?state=user" name="loginform" class="formular"></form></html>')

    def test_the_hidden_session_timeout_dialog_of_a_signed_in_page_is_not_a_login_page(self):
        from stums_parsers import is_stums_login_page
        self.assertFalse(is_stums_login_page(self.SIGNED_IN))

    def test_the_real_login_pages_are_still_recognised(self):
        from stums_parsers import is_stums_login_page
        self.assertTrue(is_stums_login_page("<title>Login notwendig - Universit\u00e4t Oldenburg</title>"))
        self.assertTrue(is_stums_login_page('<form id="loginForm" action="/x"></form>'))
        self.assertTrue(is_stums_login_page('<form name="loginform" action="/x"></form>'))
        self.assertFalse(is_stums_login_page(""))

    def test_a_signed_in_page_passes_the_page_reader(self):
        import stums_client

        class Resp:
            status = 200
            url = "https://stums.uni-oldenburg.de/qisserver/x"
            headers = {}

            async def text(self):
                return StumsLoginPageDetectionTests.SIGNED_IN

            def release(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Sess:
            async def get(self, url, **kw):
                return Resp()

        text, _ = asyncio.run(stums_client._read_stums_page(Sess(), "https://stums.uni-oldenburg.de/qisserver/x", "r"))
        self.assertIn("Planner of studies", text)


class NoRegisteredExamsTests(unittest.TestCase):
    def _schedule(self, html):
        import stums_client
        with mock.patch.object(stums_client, "_establish_stums_session", mock.AsyncMock(return_value="r")), \
                mock.patch.object(stums_client, "_read_stums_page", mock.AsyncMock(return_value=(html, "u"))):
            return asyncio.run(stums_client.get_registered_exam_schedule(object()))

    def test_a_page_without_any_table_means_nothing_is_registered(self):
        html = ('<html><form action="/x?_flowId=searchOwnEnrollmentInfo-flow"></form>'
                "<p>You haven't applied any courses or examinations yet.</p></html>")
        self.assertEqual(self._schedule(html), [])

    def test_a_changed_page_with_other_tables_is_still_an_error(self):
        html = '<html><form action="/x?_flowId=searchOwnEnrollmentInfo-flow"></form><table class="other"></table></html>'
        with self.assertRaises(RuntimeError):
            self._schedule(html)

    def test_an_unrelated_page_is_still_an_error(self):
        with self.assertRaises(RuntimeError):
            self._schedule("<html><p>something else</p></html>")


class UniversityProxyTests(unittest.TestCase):
    def test_without_a_proxy_the_connector_is_plain_and_the_worker_may_be_used(self):
        import os
        import studip_http
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNI_PROXY_URL", None)
            self.assertEqual(studip_http.uni_proxy_url(), "")
            self.assertIsNone(studip_http.uni_proxy_reachable())

            async def build():
                return type(studip_http.make_uni_connector(4, 2)).__name__
            self.assertEqual(asyncio.run(build()), "TCPConnector")

    def test_with_a_proxy_requests_use_it_and_the_worker_is_skipped(self):
        import os
        import studip_http
        import studip_session
        from studip_utils import route_proxy_url
        env = {"UNI_PROXY_URL": "socks5://127.0.0.1:40000", "CLOUDFLARE_WORKER_URL": "https://w.example",
               "CLOUDFLARE_WORKER_SECRET": "s"}
        with mock.patch.dict(os.environ, env):
            async def build():
                return type(studip_http.make_uni_connector(4, 2)).__name__
            self.assertEqual(asyncio.run(build()), "ProxyConnector")
            sess = studip_session.StudIPSession.__new__(studip_session.StudIPSession)
            self.assertFalse(sess._via_worker("elearning.uni-oldenburg.de"))
            url = "https://elearning.uni-oldenburg.de/dispatch.php/login"
            self.assertEqual(route_proxy_url(url)[0], url)

    def test_a_closed_proxy_port_is_reported_unreachable(self):
        import os
        import socket
        import studip_http
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with mock.patch.dict(os.environ, {"UNI_PROXY_URL": f"socks5://127.0.0.1:{port}"}):
            self.assertFalse(studip_http.uni_proxy_reachable())


class QuietLogTests(unittest.TestCase):
    def test_a_student_without_tasks_is_not_a_warning(self):
        from unibot import db
        from tests.testenv import activate_student
        activate_student(7790)
        with self.assertNoLogs(level="WARNING"):
            self.assertEqual(db.get_user_scope(7790).tasks(), [])

    def test_a_really_malformed_task_record_still_warns(self):
        from unibot import db
        from tests.testenv import activate_student
        activate_student(7791)
        db.get_user_scope(7791).save_private(db.TASKS_KIND, {"not": "a list"})
        with self.assertLogs(level="WARNING"):
            self.assertEqual(db.get_user_scope(7791).tasks(), [])

    def test_the_worker_is_not_reported_while_a_university_proxy_replaces_it(self):
        import os
        import studip_session
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://w.example",
                                          "UNI_PROXY_URL": "socks5://127.0.0.1:40000"}):
            self.assertFalse(studip_session.worker_status()["configured"])
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://w.example"}):
            os.environ.pop("UNI_PROXY_URL", None)
            self.assertTrue(studip_session.worker_status()["configured"])


class UniversityProxyHealthTests(unittest.TestCase):
    def test_the_check_only_exists_with_a_proxy(self):
        import os
        from unibot import system
        env_without = {k: v for k, v in os.environ.items() if k != "UNI_PROXY_URL"}
        quiet = mock.AsyncMock(return_value=(True, "OK"))
        with mock.patch.dict(os.environ, env_without, clear=True), \
                mock.patch.object(system, "check_studip_health", quiet), \
                mock.patch.object(system, "check_whatsapp_health", quiet), \
                mock.patch.object(system, "check_weather_health", quiet), \
                mock.patch.object(system, "check_openrouter_health", quiet), \
                mock.patch.object(system, "check_gwdg_health", quiet):
            self.assertNotIn("uni_proxy", asyncio.run(system.run_health_checks()))
            with mock.patch.dict(os.environ, {"UNI_PROXY_URL": "socks5://127.0.0.1:1"}), \
                    mock.patch.object(system, "check_uni_proxy_health", mock.AsyncMock(return_value=(False, "down"))):
                results = asyncio.run(system.run_health_checks())
        self.assertEqual(results["uni_proxy"], (False, "down"))
        self.assertIn("uni_proxy", system.HEALTH_CHECK_LABELS)

    def test_a_closed_port_is_reported_with_the_fix(self):
        import os
        import socket
        from unibot import system
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with mock.patch.dict(os.environ, {"UNI_PROXY_URL": f"socks5://127.0.0.1:{port}"}):
            ok, detail = asyncio.run(system.check_uni_proxy_health())
        self.assertFalse(ok)
        self.assertIn("warp-cli connect", detail)


class ProxyRepairTests(unittest.TestCase):
    PROXY = {"UNI_PROXY_URL": "socks5://127.0.0.1:40000"}

    def setUp(self):
        from unibot import system
        self.system = system
        system._uni_proxy_repaired_at = 0.0
        self.addCleanup(lambda: setattr(system, "_uni_proxy_repaired_at", 0.0))

    def _run(self, statuses, reachable=True, which=lambda name: "/usr/bin/" + name):
        import os
        commands = []
        outputs = iter(statuses)

        async def run_command(*args, timeout=25.0):
            commands.append(" ".join(args[1:] if args[0] == "warp-cli" else args[1:]))
            if args[-1] == "status":
                return 0, next(outputs)
            return 0, "Success"

        async def no_wait(seconds):
            return None

        with mock.patch.dict(os.environ, self.PROXY), \
                mock.patch.object(self.system, "_run_command", run_command), \
                mock.patch.object(self.system.shutil, "which", which), \
                mock.patch.object(self.system.asyncio, "sleep", no_wait), \
                mock.patch("studip_http.uni_proxy_reachable", return_value=reachable):
            result = asyncio.run(self.system.ensure_uni_proxy())
        return result, commands

    def test_a_connected_proxy_is_left_alone(self):
        result, commands = self._run(["Status update: Connected"])
        self.assertIsNone(result)
        self.assertEqual(commands, ["--accept-tos status"])

    def test_a_dropped_connection_is_reconnected(self):
        result, commands = self._run(["Status update: Disconnected. Reason: Manual", "Status update: Connected"])
        self.assertEqual(result, "reconnected")
        self.assertIn("--accept-tos connect", commands)
        self.assertNotIn("restart warp-svc", commands)

    def test_when_reconnecting_does_not_help_the_service_is_restarted(self):
        result, commands = self._run(["Disconnected", "Disconnected", "Status update: Connected"])
        self.assertEqual(result, "service restarted")
        self.assertIn("restart warp-svc", commands)

    def test_a_failed_repair_is_reported_and_not_repeated_within_the_cooldown(self):
        result, _ = self._run(["Disconnected", "Disconnected", "Disconnected"])
        self.assertEqual(result, "failed")
        result_again, commands = self._run(["Disconnected"])
        self.assertIsNone(result_again)
        self.assertEqual(commands, ["--accept-tos status"])  # looked, but within the cooldown nothing is tried

    def test_nothing_happens_without_a_proxy_or_without_warp_cli(self):
        import os
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "UNI_PROXY_URL"}, clear=True):
            self.assertIsNone(asyncio.run(self.system.ensure_uni_proxy()))
        result, commands = self._run(["Disconnected"], which=lambda name: None)
        self.assertIsNone(result)
        self.assertEqual(commands, [])


class WorkerUsageTests(unittest.TestCase):
    def setUp(self):
        import studip_session
        from unibot import db
        self.module = studip_session
        self.db = db
        studip_session._WORKER_USAGE.update(day="", count=0, saved=0, warned=[])
        self.addCleanup(lambda: db.delete(studip_session.WORKER_USAGE_PATH))
        db.delete(studip_session.WORKER_USAGE_PATH)

    def test_every_request_is_counted_for_the_utc_day(self):
        for _ in range(7):
            self.module.count_worker_request()
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://w.example"}):
            status = self.module.worker_status()
        self.assertEqual(status["requests_today"], 7)
        self.assertEqual(status["daily_limit"], 100_000)

    def test_the_count_survives_a_restart_and_starts_over_on_a_new_day(self):
        for _ in range(self.module.WORKER_USAGE_SAVE_EVERY):
            self.module.count_worker_request()
        self.module._WORKER_USAGE.update(day="", count=0, saved=0, warned=[])  # a restart
        self.assertEqual(self.module._worker_usage_today()["count"], self.module.WORKER_USAGE_SAVE_EVERY)
        self.db.save(self.module.WORKER_USAGE_PATH, {"day": "2000-01-01", "count": 99999})
        self.module._WORKER_USAGE.update(day="", count=0, saved=0, warned=[])
        self.assertEqual(self.module._worker_usage_today()["count"], 0)  # yesterday's count is not today's

    def test_it_warns_at_seventy_and_ninety_percent_once_each(self):
        with mock.patch.object(self.module, "worker_daily_limit", lambda: 10), self.assertLogs(level="WARNING") as logs:
            for _ in range(10):
                self.module.count_worker_request()
        warnings = [line for line in logs.output if "free requests used today" in line]
        self.assertEqual(len(warnings), 2)
        self.assertIn("7 of 10", warnings[0])
        self.assertIn("9 of 10", warnings[1])

    def test_the_health_report_and_the_digest_show_it(self):
        from unibot import diagnostics
        for count, icon in ((10, "☁️"), (75_000, "🟠"), (95_000, "🔴")):
            line = diagnostics._worker_usage_line({"requests_today": count, "daily_limit": 100_000})
            self.assertTrue(line.startswith(icon), line)
            self.assertIn(f"{count} of 100000", line)


class ContentScanFrequencyTests(unittest.TestCase):
    def _run(self, moments):
        from datetime import datetime
        from unibot import watcher
        from studip_utils import TZ_BERLIN
        ran = []
        cache: dict = {}

        def job(name):
            async def run(*args, **kwargs):
                ran.append(name)
            return run

        patches = {
            "watched_courses": mock.AsyncMock(return_value=[]),
            "check_new_announcements_parallel": job("announcements"),
            "check_new_forum_posts_parallel": job("forum"),
            "check_new_files_parallel": job("files"),
            "load_general_cache": lambda: dict(cache),
            "update_general_cache": lambda data: cache.update(data),
        }
        history = []
        with mock.patch.multiple(watcher, **patches), mock.patch.object(watcher.asyncio, "sleep", mock.AsyncMock()):
            for hh, mm in moments:
                before = len(ran)
                now = datetime(2026, 11, 16, hh, mm, tzinfo=TZ_BERLIN)
                asyncio.run(watcher._run_content_watchers(None, UID, 1, now))
                history.append(ran[before:])
        return history

    def test_announcements_and_forum_run_about_every_second_cycle(self):
        # cycles are about 4.5 minutes apart
        history = self._run([(10, 0), (10, 4), (10, 9), (10, 13), (10, 18)])
        self.assertEqual([("announcements" in h, "forum" in h) for h in history],
                         [(True, True), (False, False), (True, True), (False, False), (True, True)])

    def test_files_keep_their_own_thirty_minute_rhythm(self):
        history = self._run([(10, 0), (10, 9), (10, 18), (10, 27), (10, 31)])
        self.assertEqual(["files" in h for h in history], [True, False, False, False, True])


class _JsonResp:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ForumSession:
    def __init__(self, discussions, posts):
        self.discussions, self.posts, self.requested = discussions, posts, []

    async def get(self, url, **kwargs):
        self.requested.append(url)
        if url.endswith("/forum-discussions"):
            return _JsonResp({"data": self.discussions})
        disc_id = url.split("/forum-discussions/")[1].split("/")[0]
        return _JsonResp({"data": self.posts[disc_id], "included": []})


class ForumUnchangedDiscussionTests(unittest.TestCase):
    def _session(self):
        def disc(i, chdate):
            return {"id": i, "attributes": {"title": f"Topic {i}", "chdate": chdate}, "meta": {}}

        def post(i, pid):
            return [{"id": pid, "attributes": {"content-html": "<p>hello</p>", "mkdate": "2026-11-16T09:00:00+01:00"},
                     "relationships": {"author": {"data": {"id": "a"}}}}]
        return _ForumSession([disc("d1", "2026-11-16T09:00:00+01:00"), disc("d2", "2026-11-16T10:00:00+01:00")],
                             {"d1": post("d1", "p1"), "d2": post("d2", "p2")})

    def _fetch(self, session, known=None):
        from unibot import monitoring_fetch
        return asyncio.run(monitoring_fetch._fetch_course_forum_strict(session, "Course", "c1", known))

    def _read_postings(self, session):
        return sorted(u.split("/forum-discussions/")[1].split("/")[0] for u in session.requested if "/postings" in u)

    def test_first_scan_reads_every_discussion_and_keeps_what_the_next_scan_needs(self):
        session = self._session()
        entries = self._fetch(session)
        self.assertEqual(self._read_postings(session), ["d1", "d2"])
        self.assertEqual({e["disc_id"]: e["disc_chdate"] for e in entries},
                         {"d1": "2026-11-16T09:00:00+01:00", "d2": "2026-11-16T10:00:00+01:00"})
        self.assertTrue(all(e["checked_at"] > 0 for e in entries))

    def test_an_unchanged_discussion_is_not_read_again_a_changed_one_is(self):
        first = self._fetch(self._session())
        known = {str(e["disc_id"]): e for e in first}
        again = self._session()
        again.discussions[1]["attributes"]["chdate"] = "2026-11-16T11:30:00+01:00"  # a reply in d2
        entries = self._fetch(again, known)
        self.assertEqual(self._read_postings(again), ["d2"])
        self.assertEqual(sorted(e["disc_id"] for e in entries), ["d1", "d2"])  # d1 is still reported, from the cache

    def test_everything_is_read_again_after_an_hour(self):
        first = self._fetch(self._session())
        known = {str(e["disc_id"]): {**e, "checked_at": e["checked_at"] - 3601} for e in first}
        again = self._session()
        self._fetch(again, known)
        self.assertEqual(self._read_postings(again), ["d1", "d2"])

    def test_an_old_cache_entry_without_the_new_fields_is_read_normally(self):
        again = self._session()
        self._fetch(again, {"d1": {"key": "c1:Topic d1:d1", "cid": "c1"}})
        self.assertEqual(self._read_postings(again), ["d1", "d2"])


if __name__ == "__main__":
    unittest.main()
