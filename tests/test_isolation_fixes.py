"""Third review round: exam dates from course files per course, admin-only
health alerts and WhatsApp forwarding, nothing for students who stopped
the bot, the inbox and study progress in encrypted records, a complete
"delete my data", and the smaller scheduling fixes."""

import asyncio
import json
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402

from unibot import auth, db, state, storage, telegram_ui  # noqa: E402

WA_ROW = [InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]


def _raw_private(user_id: int, kind: str) -> str:
    row = db._connection().execute(
        "SELECT encrypted_blob FROM user_private_data WHERE user_id = ? AND kind = ?", (user_id, kind),
    ).fetchone()
    return row[0] if row else ""


class AdminOnlyTests(unittest.TestCase):
    def test_health_alerts_reach_admins_only(self):
        from unibot import watcher
        calls = []

        async def broadcast(bot, text, **kwargs):
            calls.append(kwargs.get("members"))

        cache = {"health_state": {"studip": True}}
        with mock.patch.object(watcher, "run_health_checks", mock.AsyncMock(return_value={"studip": (False, "x")})), \
                mock.patch.object(watcher, "broadcast", broadcast), \
                mock.patch.object(watcher, "ADMIN_USER_IDS", {1}), \
                mock.patch.object(watcher, "load_general_cache", lambda: cache), \
                mock.patch.object(watcher, "update_general_cache", cache.update):
            asyncio.run(watcher._report_health_changes(mock.MagicMock()))
        self.assertEqual(calls, [{1}])

    def test_forward_button_is_hidden_from_students(self):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Task", callback_data="t|1")], WA_ROW])
        with mock.patch.object(telegram_ui, "ADMIN_USER_IDS", {1}), \
                mock.patch.object(telegram_ui.db, "is_user_admin_db", lambda uid: False):
            student = telegram_ui.markup_for(2, markup)
            self.assertEqual([[b.callback_data for b in row] for row in student.inline_keyboard], [["t|1"]])
            self.assertIs(telegram_ui.markup_for(1, markup), markup)
            self.assertIsNone(telegram_ui.markup_for(2, InlineKeyboardMarkup([WA_ROW])))

    def test_students_cannot_forward_to_the_whatsapp_group(self):
        from unibot import whatsapp
        sent = []
        for handler in (whatsapp.forward_to_whatsapp, whatsapp.handle_forward_wa_ask, whatsapp.handle_forward_wa_cancel):
            query = mock.MagicMock()
            query.from_user.id = 907
            query.answer = mock.AsyncMock()
            query.edit_message_reply_markup = mock.AsyncMock()
            with mock.patch.object(whatsapp, "is_user_admin", lambda uid: False), \
                    mock.patch.object(whatsapp, "_send_text_to_whatsapp", lambda text: sent.append(text)):
                asyncio.run(handler(mock.MagicMock(callback_query=query), mock.MagicMock()))
            query.edit_message_reply_markup.assert_not_called()
            self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        self.assertEqual(sent, [])

    def test_a_file_sent_as_the_file_itself_has_no_forward_button_for_students(self):
        from unibot import monitoring_alerts
        session = mock.MagicMock()
        response = mock.MagicMock()
        response.status = 200
        response.read = mock.AsyncMock(return_value=b"pdf")
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=False)
        session.get = mock.AsyncMock(return_value=response)
        bot = mock.MagicMock()
        bot.send_document = mock.AsyncMock(return_value=mock.MagicMock(document=None))
        keyboard = InlineKeyboardMarkup([WA_ROW])
        send = monitoring_alerts._full_file_sender({"url": "https://x/f.pdf", "name": "f.pdf"}, "text", keyboard, 1)
        with mock.patch.object(monitoring_alerts, "get_user_session", lambda uid: session), \
                mock.patch.object(telegram_ui, "ADMIN_USER_IDS", {1}), \
                mock.patch.object(telegram_ui.db, "is_user_admin_db", lambda uid: False):
            self.assertTrue(asyncio.run(send(bot, 2)))
            self.assertTrue(asyncio.run(send(bot, 1)))
        student, admin = (c.kwargs["reply_markup"] for c in bot.send_document.await_args_list)
        self.assertIsNone(student)
        self.assertIs(admin, keyboard)

    def test_a_held_digest_never_carries_the_forward_button(self):
        from unibot import course_registry
        self.assertIsNone(course_registry._first_button(InlineKeyboardMarkup([WA_ROW])))


class InactiveStudentTests(unittest.TestCase):
    def test_students_who_stopped_get_no_broadcasts(self):
        db.upsert_user(908, "a")
        db.upsert_user(909, "b")
        db.set_lifecycle(909, "inactive", "test")
        with mock.patch.object(telegram_ui, "ALLOWED_USER_IDS", set()):
            targets = telegram_ui.broadcast_targets(None, {908, 909})
        self.assertEqual(targets, {908})


class ExactAudienceTests(unittest.TestCase):
    def test_given_members_without_a_course_are_the_whole_audience(self):
        db.upsert_user(940, "student")
        db.upsert_user(941, "admin")
        with mock.patch.object(telegram_ui, "ALLOWED_USER_IDS", set()):
            self.assertEqual(telegram_ui.broadcast_targets(None, {941}), {941})  # e.g. the admin digest
            self.assertIn(940, telegram_ui.broadcast_targets(None, None))  # a real broadcast still reaches everyone


class EncryptedRecordTests(unittest.TestCase):
    def setUp(self):
        self.cache: dict = {}
        for name, value in (("load_general_cache", lambda: self.cache),
                            ("save_general_cache", lambda d: self.cache.update(d))):
            patcher = mock.patch.object(storage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        storage._legacy_checked.clear()
        for uid in (910, 911, 912, 913, 914):  # the test database outlives one run
            db.get_user_scope(uid).delete_private()

    def test_inbox_moves_into_the_encrypted_record(self):
        from unibot import monitoring
        db.save("messages_cache_910.json", [{"id": "m1", "body": "secret body"}])
        self.assertEqual(monitoring.load_message_cache(910), [{"id": "m1", "body": "secret body"}])
        self.assertFalse(db.exists("messages_cache_910.json"))
        raw = _raw_private(910, storage.PERSONAL_RECORD_PREFIX + monitoring.MESSAGES_RECORD)
        self.assertTrue(raw)
        self.assertNotIn("secret body", raw)
        self.assertIsNone(monitoring.stored_messages(911))  # nothing yet: the first check is a baseline

    def test_study_progress_moves_into_the_encrypted_record(self):
        from unibot import study_plan
        db.save("study_progress_912.json", {"tracked": ["c9"]})
        self.assertEqual(study_plan.load_study_progress(user_id=912), {"tracked": ["c9"]})
        self.assertFalse(db.exists("study_progress_912.json"))
        study_plan.save_study_progress({"tracked": ["c8"]}, user_id=912)
        self.assertEqual(storage.load_student_progress(912), {"tracked": ["c8"]})
        # quotes never occur in the base64 ciphertext, so this only fails on plain JSON
        self.assertNotIn('"c8"', _raw_private(912, storage.PERSONAL_RECORD_PREFIX + storage.STUDY_PROGRESS_RECORD))

    def test_the_shared_admin_mirror_goes_only_to_the_primary_admin(self):
        db.save(storage.LEGACY_SHARED_PROGRESS, {"tracked": ["admin"]})
        with mock.patch.object(storage, "PRIMARY_ADMIN_ID", 913):
            self.assertEqual(storage.load_student_progress(914), {})
            self.assertEqual(storage.load_student_progress(913), {"tracked": ["admin"]})
        self.assertFalse(db.exists(storage.LEGACY_SHARED_PROGRESS))


class DeleteMyDataTests(unittest.TestCase):
    def test_everything_personal_is_gone(self):
        import fast_enroll
        from unibot import privacy, schedule
        uid = 915
        for key in (f"messages_cache_{uid}.json", f"study_progress_{uid}.json", f"reminders_cache_{uid}.json"):
            db.save(key, {"x": 1})
        fast_enroll.save_pending_job(uid, "sem1", "10:00", None)
        task = mock.MagicMock()
        schedule.fastenroll_tasks[uid] = {"sem1": task}
        with mock.patch.object(privacy, "_perform_logout", mock.AsyncMock()):
            removed = asyncio.run(privacy.delete_personal_data(uid))
        self.assertEqual(len(removed["documents"]), 3)
        for key in (f"messages_cache_{uid}.json", f"study_progress_{uid}.json", f"reminders_cache_{uid}.json"):
            self.assertFalse(db.exists(key))
        self.assertEqual(fast_enroll.list_pending_jobs(uid), {})
        task.cancel.assert_called_once()
        self.assertNotIn(uid, schedule.fastenroll_tasks)


class SchedulingTests(unittest.TestCase):
    def test_timetables_are_read_a_few_per_cycle_and_retried_sooner(self):
        from unibot import schedule
        accounts = [mock.MagicMock(user_id=920 + i, session=f"s{i}") for i in range(7)]
        read = []

        async def fetch(timestamp=None, user_id=None, session=None):
            read.append(user_id)
            if user_id == 920:
                raise RuntimeError("down")
            return []

        with mock.patch.object(schedule, "signed_in_accounts", lambda: accounts), \
                mock.patch.object(schedule, "fetch_user_schedule_api", fetch), \
                mock.patch.object(schedule, "personal_record", lambda *a, **k: {}), \
                mock.patch.object(schedule, "load_general_cache", lambda: {}), \
                mock.patch.dict(schedule._timetable_checked_at, clear=True):
            asyncio.run(schedule.check_personal_timetable_changes(None))
            self.assertEqual(len(read), schedule.PERSONAL_TIMETABLES_PER_CYCLE)
            retry_in = schedule._timetable_checked_at[920] + schedule.PERSONAL_TIMETABLE_CHECK_SECONDS - time.time()
            self.assertLess(retry_in, schedule.PERSONAL_TIMETABLE_RETRY_SECONDS + 5)
            read.clear()
            asyncio.run(schedule.check_personal_timetable_changes(None))
            self.assertEqual(set(read), {925, 926})  # the rest, not the ones just read

    def test_restore_drops_jobs_of_students_who_stopped(self):
        import fast_enroll
        from unibot import enrollment
        fast_enroll.save_pending_job(930, "sem", "10:00", None)
        app = mock.MagicMock()
        app.bot.send_message = mock.AsyncMock()
        with mock.patch.object(enrollment, "is_user_allowed", lambda uid: True), \
                mock.patch.object(enrollment, "lifecycle_state", lambda uid: auth.INACTIVE), \
                mock.patch.object(enrollment, "load_pending", lambda: {"930": {"sem": {"target_time_str": "10:00"}}}):
            asyncio.run(enrollment.restore_pending_fastenroll_jobs(app))
        self.assertEqual(fast_enroll.list_pending_jobs(930), {})

    def test_a_students_previous_semester_switch_leaves_the_watchers_map(self):
        from unibot import menus
        query = mock.MagicMock()
        query.from_user.id = 931
        query.data = "status_group_toggle|semester|watch_prev"
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        watcher_map = {"c1": "Course"}
        with mock.patch.object(menus, "is_user_allowed", lambda uid: True), \
                mock.patch.object(menus, "_status_group_view", lambda g, uid: ("t", None)), \
                mock.patch.object(state, "global_session", mock.MagicMock(user_id=1)), \
                mock.patch.object(state, "courses_map", watcher_map):
            asyncio.run(menus.handle_status_group(mock.MagicMock(callback_query=query), mock.MagicMock()))
            self.assertEqual(state.courses_map, {"c1": "Course"})
        self.assertNotIn("931", json.dumps(watcher_map))


if __name__ == "__main__":
    unittest.main()
