"""Isolation audit: one student's data must not reach another student, and a
student must not reach administrator functions.

Student A gets a unique "canary" text in every kind of personal record the bot
keeps. The test then looks for it everywhere student B (and the administrator's
dashboard) could see: B's own accessors and screens, B's key against A's
encrypted blobs, and the raw database file."""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_handler, use_temp_db  # noqa: E402
use_temp_db()

import exam_reminder  # noqa: E402
import studip_vault  # noqa: E402
from unibot import (  # noqa: E402
    course_registry,
    db,
    menus,
    monitoring,
    privacy,
    schedule,
    settings_menu,
    storage,
    system,
    tasks,
    wardrobe,
    weather,
)
from unibot import preferences as prefs  # noqa: E402

A, B, ADMIN = 9971, 9972, 9973
CANARY = "canary"


def _populate_a():
    storage.save_user_name(A, "CanaryAlice")
    prefs.set_pref(A, "food_avoid_keywords", ["canaryfood"])
    storage.add_user_task(A, {"id": "t1", "user_id": A, "text": "canary task", "due_time": None, "course": "Canary Course"})
    weather.save_weather_location("CanaryTown", "DE", 53.0, 8.0, user_id=A)
    storage.save_default_semester("sem1", "CanarySemester", user_id=A)
    storage.add_wardrobe_item(A, {"type": "top", "label": "canary jacket"}, file_id="canaryfile")
    monitoring.save_messages(A, [{"id": "m1", "title": "canary subject", "sender": "Canary Sender",
                                  "date": "01/01/26 10:00", "body": "canary body"}])
    exam_reminder.save_exam_cache({"known_grades": {"M1": "canary-grade"}}, user_id=A)
    storage.remember_user_callback(A, "exam:x", {"title": "canary exam"})
    course_registry.queue_alert(A, "files", "c1", "canary alert", None, "quiet")
    storage.save_personal_record(A, "studip_name", "Canary Studip Name")
    storage.save_personal_record(A, "study_blocks", [{"course": "canary course", "status": "done"}])


def _texts_of(markup) -> list:
    return [b.text for row in (getattr(markup, "inline_keyboard", None) or []) for b in row]


class IsolationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_student(A)
        activate_student(B)
        db.upsert_user(ADMIN, role="admin")
        _populate_a()
        storage.save_user_name(B, "Bob")  # B has data of their own: it must show, A's must not
        # the raw rows of A, read once
        cls.rows_a = db._connection().execute(
            "SELECT kind, encrypted_blob FROM user_private_data WHERE user_id = ?", (A,)).fetchall()

    def test_every_kind_of_record_was_really_written_for_a(self):
        kinds = {kind for kind, _ in self.rows_a}
        self.assertGreaterEqual(len(kinds), 8, kinds)

    @staticmethod
    def _everything_of(uid) -> dict:
        return {
            "name": storage.get_user_name(uid),
            "tasks": storage.user_tasks(uid),
            "weather": weather.load_weather_location(uid),
            "semester": storage.load_default_semester(uid),
            "wardrobe": storage.get_wardrobe_entry(uid),
            "messages": monitoring.stored_messages(uid),
            "exam cache": exam_reminder.load_exam_cache(uid),
            "button action": storage.recall_user_callback(uid, "exam:x"),
            "alert queue": db.get_user_scope(uid).private("alert_queue"),
            "studip name": storage.personal_record(uid, "studip_name"),
            "study blocks": storage.personal_record(uid, "study_blocks"),
            "preferences": prefs.get_prefs(uid),
            "food filter": {k: sorted(v) if isinstance(v, set) else v for k, v in prefs.food_filter(uid).items()},
        }

    def test_the_probe_finds_the_canary_where_it_belongs(self):
        # without this, "nothing found for B" would prove nothing
        own = self._everything_of(A)
        for name in ("name", "tasks", "weather", "semester", "wardrobe", "messages", "exam cache", "button action",
                     "alert queue", "studip name", "study blocks", "preferences"):
            self.assertIn(CANARY, json.dumps(own[name], default=str).lower(), name)

    def test_b_reads_none_of_a_through_any_accessor(self):
        seen = self._everything_of(B)
        seen["courses"] = storage.student_courses(B)
        self.assertNotIn(CANARY, json.dumps(seen, default=str).lower())
        self.assertEqual(seen["name"], "Bob")  # B's own data is there

    def test_b_cannot_decrypt_any_blob_of_a(self):
        with self.assertLogs("StudIPVault", level="ERROR"):
            for kind, blob in self.rows_a:
                self.assertIsNone(studip_vault.decrypt_user_payload(B, blob), kind)
        for kind, blob in self.rows_a:  # while A's own key opens every one of them
            self.assertIsNotNone(studip_vault.decrypt_user_payload(A, blob), kind)

    def test_the_database_file_holds_no_plain_text_of_a(self):
        db._connection().execute("PRAGMA wal_checkpoint(FULL)")
        path = db._db_path()
        data = b""
        for suffix in ("", "-wal"):
            if os.path.exists(path + suffix):
                with open(path + suffix, "rb") as fh:
                    data += fh.read()
        self.assertGreater(len(data), 0)
        self.assertNotIn(CANARY.encode(), data.lower())

    def test_the_screens_b_sees_show_none_of_a(self):
        shown = [
            settings_menu.main_view(B)[0], settings_menu.life_view(B)[0], settings_menu.notifications_view(B)[0],
            privacy.privacy_view(B)[0],
            menus._status_group_view("profile", B)[0], menus._status_group_view("semester", B)[0],
            wardrobe._wardrobe_list_view(B)[0],
        ]
        shown += _texts_of(wardrobe._wardrobe_list_view(B)[1])
        sent = []
        message = mock.AsyncMock()
        message.reply_text = mock.AsyncMock(side_effect=lambda text, **kw: sent.append(text))
        asyncio.run(tasks.send_task_list(message, B))
        shown += sent
        shown += [t.get("text", "") for t in storage.tasks_due_between(storage.user_tasks(B), date(2000, 1, 1), date(2100, 1, 1))]
        with mock.patch.object(schedule, "get_open_exam_registrations", mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, "get_registered_exam_schedule", mock.AsyncMock(return_value=[])):
            shown.append(asyncio.run(schedule.build_upcoming_dashboard(object(), B)))
        self.assertTrue(all(isinstance(t, str) for t in shown))
        self.assertNotIn(CANARY, "\n".join(shown).lower())

    def test_the_administrator_dashboard_shows_no_private_record(self):
        view = system._build_user_detail_view(A, ADMIN)
        self.assertIsNotNone(view)
        text = view[0] + " ".join(_texts_of(view[1]))
        self.assertNotIn(CANARY, text.lower())
        overview = system._build_admin_overview_view()
        self.assertNotIn(CANARY, (overview[0] + " ".join(_texts_of(overview[1]))).lower())


def _callback(uid, data):
    query = mock.AsyncMock()
    query.data = data
    query.from_user = types.SimpleNamespace(id=uid)
    query.message.chat_id = uid
    return query


class StudentCannotUseAdministratorFunctionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_student(B)

    def _denied(self, handler, data, **patches):
        query = _callback(B, data)
        update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=B))
        run_handler(handler, update, types.SimpleNamespace(user_data={}, bot=mock.AsyncMock(), args=[]))
        return query

    def test_admin_dashboard_buttons(self):
        for data in ("admin_menu", "admin_users", "admin_invites", "admin_new_invite", "admin_broadcast_prompt",
                     "admin_system_health", "admin_restart_confirm", f"admin_user_detail|{A}",
                     f"admin_revoke_user|{A}", f"admin_toggle_user|{A}", f"admin_role_user|{A}"):
            query = self._denied(system.handle_admin_callbacks, data)
            self.assertIn("Access denied", query.answer.call_args.args[0], data)
            query.edit_message_text.assert_not_awaited()

    def test_status_buttons_that_act_on_the_whole_bot(self):
        for data in ("wa_menu", "start_watchers", "stop_watchers", "request_wa_qr", "force_wa_qr",
                     "wa_test_message", "detect_wa_groups"):
            query = self._denied(menus.handle_status_buttons, data)
            self.assertIn("restricted", query.answer.call_args.args[0], data)

    def test_wardrobe_and_whatsapp_forwarding(self):
        for handler, data in ((wardrobe.handle_wardrobe_menu, "wardrobe_menu"),
                              (wardrobe.handle_wardrobe_add_item, "wardrobe_add_item"),
                              (wardrobe.handle_wardrobe_remove, "wardrobe_remove|w1"),
                              (wardrobe.handle_wardrobe_clear_ask, "wardrobe_clear_ask"),
                              (wardrobe.handle_wardrobe_clear_confirm, "wardrobe_clear_confirm"),
                              (wardrobe.handle_wardrobe_suggest_now, "wardrobe_suggest_now"),
                              (wardrobe.handle_outfit_menu, "outfit_menu"),
                              (wardrobe.handle_toggle_wardrobe_feature, "toggle_wardrobe_feature")):
            query = self._denied(handler, data)
            self.assertIn("restricted", query.answer.call_args.args[0], data)
        from unibot import whatsapp
        for handler, data in ((whatsapp.handle_forward_wa_ask, "forward_wa"),
                              (whatsapp.forward_to_whatsapp, "forward_wa_confirm"),
                              (whatsapp.set_wa_group_handler, "set_wa_group|123@g.us")):
            query = self._denied(handler, data)
            self.assertTrue(query.answer.call_args.args, data)
            self.assertIn("administrators", query.answer.call_args.args[0].lower(), data)

    def test_admin_commands(self):
        for command in (system.admin_command, system.broadcast_command, system.invite_command, system.restart_command):
            message = mock.AsyncMock()
            update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=B), message=message,
                                           effective_message=message, effective_chat=types.SimpleNamespace(id=B))
            run_handler(command, update, types.SimpleNamespace(user_data={}, bot=mock.AsyncMock(), args=[]))
            self.assertIn("restricted", message.reply_text.call_args.args[0], command.__name__)


if __name__ == "__main__":
    unittest.main()
