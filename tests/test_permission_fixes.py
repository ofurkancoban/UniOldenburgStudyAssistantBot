"""Admin and student permissions: /watch never starts a second watcher,
a student's /check covers only their own courses, the last-5 views show
only the student's courses, the shared course map stays the watcher's,
environment admins are protected in the admin panel, /healthcheck keeps
server details for admins and students can't start exam scans at will."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import db, state  # noqa: E402


def _message_update(user_id: int):
    update = mock.MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = user_id
    update.message.reply_text = mock.AsyncMock(return_value=mock.MagicMock(edit_text=mock.AsyncMock()))
    return update


def _query_update(user_id: int, data: str = ""):
    query = mock.MagicMock()
    query.from_user.id = user_id
    query.data = data
    query.message.chat_id = user_id
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.message.reply_text = mock.AsyncMock(return_value=mock.MagicMock(edit_text=mock.AsyncMock(), delete=mock.AsyncMock()))
    return mock.MagicMock(callback_query=query), query


class WatchCommandTests(unittest.TestCase):
    def test_a_student_never_starts_a_watcher(self):
        from unibot import monitoring, watcher
        start = mock.AsyncMock()
        update = _message_update(1001)
        with mock.patch.object(monitoring, "is_user_allowed", lambda uid: True), \
                mock.patch.object(monitoring, "is_user_admin", lambda uid: False), \
                mock.patch.object(watcher, "start_unified_watcher", start):
            asyncio.run(monitoring.watch(update, mock.MagicMock()))
        start.assert_not_called()
        self.assertIn("automatically", update.message.reply_text.call_args.args[0])

    def test_an_admin_resumes_the_one_watcher_instead_of_a_second(self):
        from unibot import monitoring, watcher
        start = mock.AsyncMock()
        with mock.patch.object(monitoring, "is_user_allowed", lambda uid: True), \
                mock.patch.object(monitoring, "is_user_admin", lambda uid: True), \
                mock.patch.object(watcher, "start_unified_watcher", start), \
                mock.patch.object(state, "watcher_controller_running", True), \
                mock.patch.object(state, "global_watcher_paused", True):
            asyncio.run(monitoring.watch(_message_update(1), mock.MagicMock()))
            self.assertFalse(state.global_watcher_paused)
        start.assert_not_called()
        with mock.patch.object(monitoring, "is_user_allowed", lambda uid: True), \
                mock.patch.object(monitoring, "is_user_admin", lambda uid: True), \
                mock.patch.object(watcher, "start_unified_watcher", start), \
                mock.patch.object(state, "watcher_controller_running", False):
            asyncio.run(monitoring.watch(_message_update(1), mock.MagicMock()))
        start.assert_awaited_once()


class ManualCheckTests(unittest.TestCase):
    def setUp(self):
        from unibot import monitoring
        self.m = monitoring
        monitoring._manual_check_at.clear()
        self.plans = []

        async def check(bot, chat_id, silent=False, courses=None):
            self.plans.append(courses)
            return False

        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)
        self.session = session
        self.patches = [
            mock.patch.object(monitoring, "check_new_messages", mock.AsyncMock(return_value=False)),
            mock.patch.object(monitoring, "check_new_announcements_parallel", check),
            mock.patch.object(monitoring, "check_new_files_parallel", check),
            mock.patch.object(monitoring, "check_new_forum_posts_parallel", check),
            mock.patch.object(monitoring, "get_user_session", lambda uid: session),
            mock.patch.object(monitoring, "student_courses", lambda uid: {"mine": "My course"}),
            mock.patch.object(monitoring, "watched_courses", mock.AsyncMock(return_value=[("All", "all", "w")])),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_student_checks_only_their_courses_and_waits_between_checks(self):
        with mock.patch.object(self.m, "is_user_admin", lambda uid: False), \
                mock.patch.object(state, "global_watcher_paused", False):
            asyncio.run(self.m._run_check_now(_message_update(1002), mock.MagicMock()))
            self.assertFalse(state.global_watcher_paused)  # the watchers keep running for everyone
            second = _message_update(1002)
            asyncio.run(self.m._run_check_now(second, mock.MagicMock()))
        self.assertEqual(self.plans, [[("My course", "mine", self.session)]] * 3)
        self.assertIn("again in", second.message.reply_text.call_args.args[0])

    def test_a_student_must_be_signed_in(self):
        self.session.is_logged_in = mock.AsyncMock(return_value=False)
        update = _message_update(1003)
        with mock.patch.object(self.m, "is_user_admin", lambda uid: False):
            asyncio.run(self.m._run_check_now(update, mock.MagicMock()))
        self.assertEqual(self.plans, [])
        self.assertIn("/login", update.message.reply_text.call_args.args[0])

    def test_an_admin_checks_every_watched_course(self):
        with mock.patch.object(self.m, "is_user_admin", lambda uid: True):
            asyncio.run(self.m._run_check_now(_message_update(1), mock.MagicMock()))
            asyncio.run(self.m._run_check_now(_message_update(1), mock.MagicMock()))  # no cooldown
        self.assertEqual(self.plans, [[("All", "all", "w")]] * 6)


class LastFiveTests(unittest.TestCase):
    def test_only_the_students_own_courses_are_shown(self):
        from unibot import monitoring
        db.save("files_cache.json", {"mine": {"a.pdf": {"course": "Mine", "modified_ts": 1}},
                                     "other": {"b.pdf": {"course": "Other", "modified_ts": 2}}})
        db.save("announcement_cache.json", {"seen": [], "history": [
            {"key": "mine:1", "cid": "mine", "course": "Mine", "subject": "own"},
            {"key": "other:1", "cid": "other", "course": "Other", "subject": "foreign"}]})
        db.save(monitoring.FORUM_CACHE_PATH, {"mine:t:1": {"course": "Mine", "body": "own post"},
                                              "other:t:1": {"course": "Other", "body": "foreign post"}})
        context = mock.MagicMock()
        context.bot.send_message = mock.AsyncMock()
        with mock.patch.object(monitoring, "student_courses", lambda uid: {"mine": "Mine"}), \
                mock.patch.object(monitoring.asyncio, "sleep", mock.AsyncMock()):
            for handler in (monitoring.show_last_files, monitoring.show_last_announcements,
                            monitoring.show_last_forum_posts):
                update, _ = _query_update(1004)
                asyncio.run(handler(update, context))
        sent = " ".join(str(c.kwargs.get("text", "")) for c in context.bot.send_message.await_args_list)
        self.assertIn("a.pdf", sent)
        self.assertIn("own", sent)
        for foreign in ("b.pdf", "foreign", "Other"):
            self.assertNotIn(foreign, sent)


class SharedCourseMapTests(unittest.TestCase):
    def test_only_the_watcher_sessions_owner_fills_the_shared_map(self):
        from unibot import studip_api
        shared: dict = {}
        with mock.patch.object(state, "courses_map", shared), \
                mock.patch.object(state, "user_courses_map", {}), \
                mock.patch.object(state, "global_session", mock.MagicMock(user_id=1)):
            studip_api._record_course("c-second-admin", "Theirs", user_id=2)
            studip_api._record_course("c-watcher", "Watcher's", user_id=1)
            self.assertEqual(shared, {"c-watcher": "Watcher's"})
            self.assertEqual(state.user_courses_map[2], {"c-second-admin": "Theirs"})


class AdminPanelTests(unittest.TestCase):
    def _tap(self, admin_id: int, data: str):
        from unibot import system
        update, query = _query_update(admin_id, data)
        with mock.patch.object(system, "is_user_admin", lambda uid: True), \
                mock.patch.object(system, "ADMIN_USER_IDS", {1}), \
                mock.patch.object(system, "ALLOWED_USER_IDS", set()), \
                mock.patch.object(system, "PRIMARY_ADMIN_ID", 1), \
                mock.patch.object(system, "_perform_logout", mock.AsyncMock()) as logout:
            asyncio.run(system.handle_admin_callbacks(update, mock.MagicMock()))
        return query, logout

    def test_a_promoted_admin_cannot_touch_an_environment_admin(self):
        db.upsert_user(1, "env-admin")
        db.set_user_role(1, "admin")
        for action in ("admin_toggle_user|1", "admin_role_user|1", "admin_revoke_user|1"):
            query, logout = self._tap(1005, action)
            logout.assert_not_called()
            self.assertTrue(query.answer.call_args.kwargs.get("show_alert"), action)
        self.assertTrue(db.get_user(1)["is_active"])
        self.assertEqual(db.get_user(1)["role"], "admin")

    def test_students_can_still_be_managed(self):
        db.upsert_user(1006, "student")
        _query, logout = self._tap(1005, "admin_revoke_user|1006")
        logout.assert_awaited_once_with(user_id=1006)


class HealthCheckTests(unittest.TestCase):
    def test_students_see_their_connection_and_no_server_details(self):
        from unibot import system
        update = _message_update(1007)
        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)
        live = mock.AsyncMock()
        with mock.patch.object(system, "is_user_allowed", lambda uid: True), \
                mock.patch.object(system, "is_user_admin", lambda uid: False), \
                mock.patch.object(system, "get_user_session", lambda uid: session), \
                mock.patch.object(system, "run_health_checks", live), \
                mock.patch.object(system, "load_general_cache", lambda: {"health_state": {"disk": False}}):
            asyncio.run(system.healthcheck_command(update, mock.MagicMock()))
        live.assert_not_called()  # no live (paid) AI check for students
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("connected", text)
        self.assertIn("Some services", text)
        self.assertNotIn("Disk", text)


if __name__ == "__main__":
    unittest.main()


class RescanFixTests(unittest.TestCase):
    def test_a_failed_round_keeps_the_course_session_state(self):
        from unibot import course_registry, monitoring, schedule
        cache = {schedule.COURSE_SESSION_STATE_KEY: {"c1": {"e1": {"ts": int(time.time())}}}}

        async def scan(cid, primary, fetch):
            raise RuntimeError("Stud.IP down")

        with mock.patch.object(monitoring, "watched_courses", mock.AsyncMock(return_value=[("One", "c1", "s")])), \
                mock.patch.object(course_registry, "scan_with_failover", scan), \
                mock.patch.object(schedule, "load_general_cache", lambda: cache), \
                mock.patch.object(schedule, "save_general_cache", cache.update):
            asyncio.run(schedule.check_course_session_changes(None, force=True))
            with mock.patch.object(monitoring, "watched_courses", mock.AsyncMock(return_value=[])):
                asyncio.run(schedule.check_course_session_changes(None, force=True))
        self.assertIn("c1", cache[schedule.COURSE_SESSION_STATE_KEY])

    def test_a_students_first_check_loads_their_course_list(self):
        from unibot import monitoring
        monitoring._manual_check_at.clear()
        courses: dict = {}
        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)

        async def list_courses(session=None, user_id=None, **kwargs):
            courses.update({"c1": "One"})

        with mock.patch.object(monitoring, "is_user_admin", lambda uid: False), \
                mock.patch.object(monitoring, "get_user_session", lambda uid: session), \
                mock.patch.object(monitoring, "student_courses", lambda uid: dict(courses)), \
                mock.patch.object(monitoring, "list_courses", list_courses):
            plan, refused = asyncio.run(monitoring._manual_check_plan(1010))
        self.assertIsNone(refused)
        self.assertEqual(plan, [("One", "c1", session)])


class CrossAccountTests(unittest.TestCase):
    def test_course_content_reaches_only_students_of_that_course(self):
        from unibot import telegram_ui
        db.upsert_user(1101, "in-registry")
        db.upsert_user(1102, "outsider")
        db.get_user_scope(1101).sync_courses({"c-reg"})
        db.get_user_scope(1102).sync_courses(set())
        with mock.patch.object(telegram_ui, "ALLOWED_USER_IDS", {1102}), \
                mock.patch.object(state, "user_courses_map", {}):
            targets = telegram_ui.broadcast_targets("c-reg", None)
        self.assertIn(1101, targets)  # known from the course registry
        self.assertNotIn(1102, targets)  # an allowed user who doesn't take it gets nothing

    def test_a_stopped_student_is_not_offered_as_a_study_buddy(self):
        from unibot import study_week
        courses = {1103: {"c1": "One"}, 1104: {"c1": "One"}, 1105: {"c1": "One"}}
        states = {1103: "active", 1104: "inactive", 1105: "active"}
        with mock.patch.object(state, "user_courses_map", courses), \
                mock.patch.object(study_week, "get_study_buddy_enabled", lambda uid: True), \
                mock.patch.object(study_week, "is_user_allowed", lambda uid: True), \
                mock.patch.object(study_week, "lifecycle_state", lambda uid: states[uid]):
            self.assertEqual([uid for uid, _ in study_week.buddy_candidates(1103)], [1105])
