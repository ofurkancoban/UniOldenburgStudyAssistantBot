"""Cross-account course content must require trusted membership at every entry."""

from contextlib import ExitStack
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from tests.testenv import use_temp_db

use_temp_db()

from unibot import auth, course_registry as registry, db, monitoring, monitoring_alerts  # noqa: E402
from unibot import state, storage, study_plan, tasks, telegram_ui  # noqa: E402


class CourseContentIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.courses = {101: {"private": "Private course"}, 202: {"other": "Other course"},
                        303: {"private": "Private course"}}
        self.rows = {uid: set(courses) for uid, courses in self.courses.items()}
        self.queues = {uid: [] for uid in self.courses}

        def scope(uid):
            return SimpleNamespace(
                course_ids=lambda: list(self.rows.get(uid, set())), course_prefs=lambda cid: None,
                private=lambda kind: list(self.queues[uid]),
                save_private=lambda kind, value: self.queues.__setitem__(uid, value),
            )

        for patch in (
            mock.patch.object(state, "user_courses_map", self.courses),
            mock.patch.object(state, "global_session", None),
            mock.patch.object(state, "courses_map", {"private": "Admin course"}),
            mock.patch.object(db, "get_user_scope", side_effect=scope),
            mock.patch.object(db, "get_all_users", return_value=[
                {"telegram_id": uid, "is_active": 1, "role": "admin" if uid == 202 else "user"}
                for uid in self.courses]),
            mock.patch.object(auth, "is_user_allowed", return_value=True),
            mock.patch.object(telegram_ui, "_is_inactive", return_value=False),
            mock.patch.object(registry, "is_user_allowed", return_value=True),
            mock.patch.object(registry, "lifecycle_state", return_value=registry.ACTIVE),
            mock.patch.object(registry, "_hold_reason", return_value=None),
            mock.patch.object(registry, "get_pref", return_value="upload"),
            mock.patch.object(registry, "markup_for", side_effect=lambda uid, value: value),
        ):
            self.stack.enter_context(patch)

    def query(self, uid, data):
        return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data,
                               answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock(),
                               edit_message_reply_markup=mock.AsyncMock(),
                               message=SimpleNamespace(reply_text=mock.AsyncMock()))

    def revoke(self, uid):
        self.courses[uid].pop("private", None)
        self.rows[uid].discard("private")

    async def test_polluted_study_selections_and_admin_role_do_not_grant_access(self):
        with mock.patch.object(storage, "load_student_progress", return_value={"tracked": ["private"]}):
            self.assertFalse(telegram_ui._takes_course(202, "private"))
            self.assertEqual(telegram_ui.broadcast_targets("private", {101, 202}), {101, 303})

    async def test_registry_membership_survives_restart_without_a_memory_map(self):
        with mock.patch.object(state, "user_courses_map", {}):
            self.assertTrue(storage.has_course_access(101, "private"))
            self.assertFalse(storage.has_course_access(202, "private"))

    async def test_nonmember_gets_neither_file_upload_nor_card(self):
        rich = mock.AsyncMock(return_value=True)
        bot = SimpleNamespace(send_message=mock.AsyncMock())
        await registry.fan_out(bot, "private", "files", "Restricted body", "File", members={101, 202}, send_full=rich)
        self.assertEqual([call.args[1] for call in rich.await_args_list], [101, 303])
        bot.send_message.assert_not_awaited()

    async def test_explicit_recipient_cannot_bypass_enrollment(self):
        rich = mock.AsyncMock()
        bot = SimpleNamespace(send_message=mock.AsyncMock())
        result = await registry.fan_out(bot, "private", "files", "Secret", "File", recipients={202}, send_full=rich)
        self.assertEqual(result["skipped"], 1)
        rich.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_direct_file_sender_rejects_an_outsider_before_downloading(self):
        send = monitoring_alerts._full_file_sender(
            {"cid": "private", "url": "https://elearning.uni-oldenburg.de/file"}, "File", None, 101)
        with auth.acting_as(202), \
                mock.patch.object(monitoring_alerts, "is_user_allowed", return_value=True), \
                mock.patch.object(monitoring_alerts, "lifecycle_state", return_value=registry.ACTIVE), \
                mock.patch.object(monitoring_alerts, "get_user_session") as session:
            self.assertFalse(await send(None, 202))
        session.assert_not_called()

    async def test_file_download_cannot_outlive_recipient_membership(self):
        send = monitoring_alerts._full_file_sender(
            {"cid": "private", "url": "https://elearning.uni-oldenburg.de/file"}, "File", None, 101)
        response = mock.MagicMock(status=200)
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=False)
        session = SimpleNamespace(get=mock.AsyncMock(return_value=response))
        bot = SimpleNamespace(send_document=mock.AsyncMock())

        async def read(*args):
            self.revoke(303)
            return b"Restricted file bytes"

        with auth.acting_as(303), \
                mock.patch.object(monitoring_alerts, "is_user_allowed", return_value=True), \
                mock.patch.object(monitoring_alerts, "lifecycle_state", return_value=registry.ACTIVE), \
                mock.patch.object(monitoring_alerts, "get_user_session", return_value=session), \
                mock.patch.object(monitoring_alerts, "read_response_limited", side_effect=read):
            self.assertFalse(await send(bot, 303))
        bot.send_document.assert_not_awaited()

    async def test_forged_track_button_cannot_store_an_unowned_course(self):
        progress = {"tracked": []}
        query = self.query(202, "sp_track|private")
        with auth.acting_as(202), \
                mock.patch.object(study_plan, "is_user_allowed", return_value=True), \
                mock.patch.object(study_plan, "_load_track_menu_progress", mock.AsyncMock(return_value=progress)), \
                mock.patch.object(study_plan, "save_study_progress") as save:
            await study_plan.handle_study_track_menu(
                SimpleNamespace(callback_query=query, effective_user=query.from_user), None)
        save.assert_not_called()
        self.assertEqual(progress["tracked"], [])
        query.message.reply_text.assert_awaited_once()

    async def test_valid_track_button_still_works(self):
        progress = {"tracked": []}
        query = self.query(202, "sp_track|other")
        with auth.acting_as(202), \
                mock.patch.object(study_plan, "is_user_allowed", return_value=True), \
                mock.patch.object(study_plan, "_load_track_menu_progress", mock.AsyncMock(return_value=progress)), \
                mock.patch.object(study_plan, "save_study_progress") as save:
            await study_plan.handle_study_track_menu(
                SimpleNamespace(callback_query=query, effective_user=query.from_user), None)
        save.assert_called_once()
        self.assertEqual(progress["tracked"], ["other"])

    async def test_old_unauthorized_queue_items_are_dropped(self):
        self.queues[202] = [{"cid": "private", "kind": "forum", "title": "Restricted text",
                             "ts": time.time(), "reason": "retry"},
                            {"kind": "announcements", "title": "Unknown course", "ts": time.time()}]
        bot = SimpleNamespace(send_message=mock.AsyncMock())
        with mock.patch.object(db, "private_owner_ids", return_value=[202]):
            await registry.flush_alert_queues(bot)
        bot.send_message.assert_not_awaited()
        self.assertEqual(self.queues[202], [])

    async def press_content(self, uid, forum=False, legacy=False, revoke_at=None):
        cache = {"sid": {"course": "Private course", "subject": "Subject", "thread": "Thread",
                         "body": "Restricted content", **({} if legacy else {"cid": "private"})}}
        query = self.query(uid, ("tasksug_add" if forum else "ann_to_task") + "|sid")
        if revoke_at == "answer":
            async def answer(*args, **kwargs):
                self.revoke(uid)
            query.answer.side_effect = answer

        async def convert(*args, **kwargs):
            if revoke_at == "conversion":
                self.revoke(uid)
            return {"text": "Derived task", "due_time": None}

        converter = mock.AsyncMock(side_effect=convert)
        save = mock.Mock(return_value="Saved")
        with auth.acting_as(uid), \
                mock.patch.object(tasks, "is_user_allowed", return_value=True), \
                mock.patch.object(tasks, "personal_ai_allowed", return_value=True), \
                mock.patch.object(tasks, "get_ai_personal_enabled", return_value=True), \
                mock.patch.object(tasks, "task_suggestion_cache" if forum else "announcement_task_cache", cache), \
                mock.patch.object(tasks, "extract_task_suggestion" if forum else "convert_announcement_to_task", converter), \
                mock.patch.object(tasks, "_save_task_and_confirmation_text", save):
            handler = tasks.handle_task_suggestion_add if forum else tasks.handle_announcement_to_task
            await handler(SimpleNamespace(callback_query=query), None)
        return query, converter, save

    async def test_known_announcement_sid_does_not_grant_source_access(self):
        query, convert, save = await self.press_content(202)
        convert.assert_not_awaited()
        save.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_known_forum_sid_does_not_grant_source_access(self):
        _, convert, save = await self.press_content(202, forum=True)
        convert.assert_not_awaited()
        save.assert_not_called()

    async def test_legacy_callbacks_without_course_id_are_rejected(self):
        for forum in (False, True):
            _, convert, save = await self.press_content(101, forum=forum, legacy=True)
            convert.assert_not_awaited()
            save.assert_not_called()

    async def test_every_actual_follower_can_use_shared_course_callbacks(self):
        for uid in (101, 303):
            for forum in (False, True):
                _, convert, save = await self.press_content(uid, forum=forum)
                self.assertEqual(convert.await_args.kwargs["course_id"], "private")
                save.assert_called_once()
                self.assertEqual(save.call_args.args[0], uid)

    async def test_membership_is_rechecked_after_answering_callback(self):
        _, convert, save = await self.press_content(101, revoke_at="answer")
        convert.assert_not_awaited()
        save.assert_not_called()

    async def test_revocation_during_ai_work_prevents_saving_and_display(self):
        query, convert, save = await self.press_content(101, forum=True, revoke_at="conversion")
        convert.assert_awaited_once()
        save.assert_not_called()
        query.message.reply_text.assert_not_awaited()

    async def test_provider_retry_guard_rechecks_membership(self):
        async def provider(*args, **kwargs):
            self.assertTrue(kwargs["can_send"]())
            self.revoke(101)
            self.assertFalse(kwargs["can_send"]())
            return None
        with mock.patch.object(tasks, "personal_ai_allowed", return_value=True), \
                mock.patch.object(tasks, "_call_openrouter", side_effect=provider) as llm:
            await tasks.convert_announcement_to_task("Course", "Subject", "Body", user_id=101, course_id="private")
        llm.assert_awaited_once()

    async def test_forum_button_cache_keeps_course_identity(self):
        cache = {}
        with mock.patch.object(monitoring_alerts, "task_suggestion_cache", cache), \
                mock.patch.object(monitoring_alerts, "_short_id", return_value="sid"):
            monitoring_alerts._forum_alert_markup({"cid": "private", "last_message_plain": "Content"})
        self.assertEqual(cache["sid"]["cid"], "private")

    async def test_announcement_button_cache_keeps_course_identity(self):
        cache = {}
        entry = {"cid": "private", "key": "private:announcement", "course": "Private course",
                 "subject": "Subject", "body": "Body", "sender": "Ada", "date": "12/11/26", "dt": "2026-11-12"}
        with mock.patch.object(monitoring, "announcement_task_cache", cache), \
                mock.patch.object(monitoring, "_short_id", return_value="sid"), \
                mock.patch.object(db, "exists", return_value=True), \
                mock.patch.object(db, "load", return_value={"seeded": ["private"], "history": [], "seen": []}), \
                mock.patch.object(db, "save"), \
                mock.patch.object(registry, "scan_with_failover", mock.AsyncMock(return_value=([entry], None))), \
                mock.patch.object(registry, "record_scan"), \
                mock.patch.object(registry, "fan_out", mock.AsyncMock()):
            await monitoring._check_new_announcements_parallel_unlocked(None, 101, True, [("Course", "private", None)])
        self.assertEqual(cache["sid"]["cid"], "private")
