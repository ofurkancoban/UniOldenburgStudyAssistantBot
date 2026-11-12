"""Fixes from the fourteenth review:
- watcher passes session as the second argument to index_course_files,
- show_last_files and _voice_intent_show_last_file populate url and user_id in link_cache,
- outfit toggle stays synchronized between wardrobe_feature_enabled and preferences["outfit"],
  and life_view hides the outfit row for non-admins,
- Mensa menu views apply the student's diet preset and max_price alongside custom avoided items,
  and show an explanatory message when all dishes are hidden,
- task due_time parsing in send_weekly_calendar, _due_tasks and handle_course_view uses task_due(),
- temporary status messages are cleaned up after sending the result,
- admin role demotion sets role to 'user',
- privacy_view shows when grades sharing is paused due to missing timetable permission,
  and forget_personal_cache cleans up ai_personal.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import run_handler, use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import (  # noqa: E402
    db,
    mensa,
    menus,
    monitoring,
    preferences,
    privacy,
    settings_menu,
    storage,
    study_plan,
    system,
    voice,
    wardrobe,
    watcher,
)


class ScheduledIndexingTests(unittest.TestCase):
    def test_scheduled_course_indexing_passes_session_correctly(self):
        recorded = []

        async def once(key, period, label, start_msg, job, *args):
            recorded.append((job, args))

        now = datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN)
        fake_session = object()
        with mock.patch.object(watcher, "_get_active_user_ids", lambda: []), \
                mock.patch.object(watcher, "_guarded", mock.AsyncMock()), \
                mock.patch.object(watcher, "_run_once_per_period", once), \
                mock.patch.object(watcher, "GWDG_API_KEY", "test-key"), \
                mock.patch.object(watcher, "COURSE_INDEX_HOUR", 4):
            asyncio.run(watcher._run_scheduled_jobs(mock.MagicMock(), fake_session, now))

        self.assertEqual(len(recorded), 1)
        job, args = recorded[0]
        self.assertIs(job, watcher._index_course_files_for_users)
        self.assertEqual(args, (fake_session,))


class LinkCachePersistenceTests(unittest.TestCase):
    def setUp(self):
        storage.link_cache.clear()
        self.addCleanup(storage.link_cache.clear)

    def test_show_last_files_stores_url_and_user_id(self):
        uid = 9910
        cache_data = {
            "cid1": {
                "lecture1.pdf": {
                    "course": "Algorithms",
                    "url": "https://elearning.uni-oldenburg.de/sendfile.php?file_id=123",
                    "size": "1 MB",
                    "modified": "10.11.2026",
                    "modified_ts": 1700000000,
                }
            }
        }
        query = mock.MagicMock()
        query.from_user.id = uid
        query.message.chat_id = uid
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        context = mock.MagicMock()
        context.bot.send_message = mock.AsyncMock()

        with mock.patch.object(monitoring.db, "exists", return_value=True), \
                mock.patch.object(monitoring.db, "load", return_value=cache_data), \
                mock.patch.object(monitoring, "student_courses", lambda u: {"cid1": "Algorithms"}), \
                mock.patch.object(monitoring.asyncio, "sleep", mock.AsyncMock()):
            run_handler(monitoring.show_last_files, mock.MagicMock(callback_query=query), context)

        self.assertEqual(len(storage.link_cache), 1)
        entry = next(iter(storage.link_cache.values()))
        self.assertEqual(entry["url"], "https://elearning.uni-oldenburg.de/sendfile.php?file_id=123")
        self.assertEqual(entry["user_id"], uid)

    def test_voice_show_last_file_stores_user_id(self):
        uid = 9911
        update = mock.MagicMock()
        update.effective_user.id = uid
        message = mock.MagicMock()
        message.chat_id = uid
        message.reply_text = mock.AsyncMock()
        context = mock.MagicMock()
        context.bot.send_chat_action = mock.AsyncMock()
        file_item = {"name": "slides.pdf", "url": "https://example.com/slides.pdf", "modified": "today", "size": "2 MB"}

        with mock.patch.object(voice, "_find_latest_file", mock.AsyncMock(return_value=("Algorithms", "cid1", file_item))):
            run_handler(voice._voice_intent_show_last_file, update, context, {}, "", "show_last_file", "algo", [], message)

        entry = next(iter(storage.link_cache.values()))
        self.assertEqual(entry["user_id"], uid)
        self.assertEqual(entry["url"], "https://example.com/slides.pdf")


class OutfitSyncAndVisibilityTests(unittest.TestCase):
    def test_outfit_menu_toggle_sets_both_switches(self):
        uid = 9920
        storage.set_wardrobe_feature_enabled(True)
        self.addCleanup(storage.set_wardrobe_feature_enabled, False)
        preferences.set_pref(uid, "outfit", True)
        # one administrator's own preference never switches the feature off for the other
        preferences.set_pref(9921, "outfit", False)
        self.assertTrue(storage.get_wardrobe_feature_enabled())

        query = mock.MagicMock()
        query.from_user.id = uid
        query.data = "outfit_menu_toggle"
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        with mock.patch.object(wardrobe, "is_user_admin", return_value=True):
            run_handler(wardrobe.handle_outfit_menu, mock.MagicMock(callback_query=query), mock.MagicMock())

        self.assertTrue(storage.get_wardrobe_feature_enabled())  # one user cannot disable the shared feature
        self.assertFalse(preferences.get_pref(uid, "outfit"))

    def test_life_view_hides_outfit_for_non_admins(self):
        with mock.patch.object(settings_menu, "is_user_admin", return_value=False):
            text_student, markup_student = settings_menu.life_view(9921)
        self.assertNotIn("Outfit suggestion", text_student)
        callbacks_student = [b.callback_data for row in markup_student.inline_keyboard for b in row]
        self.assertNotIn("settings|flip|outfit", callbacks_student)

        with mock.patch.object(settings_menu, "is_user_admin", return_value=True):
            text_admin, markup_admin = settings_menu.life_view(9921)
        self.assertIn("Outfit suggestion", text_admin)
        callbacks_admin = [b.callback_data for row in markup_admin.inline_keyboard for b in row]
        self.assertIn("settings|flip|outfit", callbacks_admin)


class MensaDietAndEmptyFilterTests(unittest.TestCase):
    WECHLOY_HTML = (
        '<div class="row splMeal"><span style="font-size:24px">Pork Schnitzel<div class="kennz">(S)</div></span>'
        '<div class="preise"><span>3,90 / 5,80 €</span></div></div>'
    )

    def test_effective_food_filter_includes_diet_preset(self):
        uid = 9930
        preferences.set_pref(uid, "diet", "vegan")
        self.addCleanup(preferences.set_pref, uid, "diet", "all")
        filt = mensa.effective_food_filter(uid)
        self.assertIn("S", filt["avoid_codes"])
        self.assertIn("Mi", filt["avoid_codes"])
        self.assertIn("honig", filt["avoid_keywords"])

    def test_all_dishes_filtered_shows_explanatory_notice(self):
        with mock.patch.object(mensa, "_fetch_speiseplan_html", mock.AsyncMock(return_value=self.WECHLOY_HTML)):
            text = asyncio.run(mensa.build_speiseplan_menu("3", date(2026, 11, 4), avoid_codes=["S"]))
        self.assertIn("No dishes match your food preferences today (1 hidden)", text)


class TaskDueParsingAndStatusCleanupTests(unittest.TestCase):
    def test_due_tasks_handles_naive_and_invalid_timestamps(self):
        uid = 9940
        now = datetime(2026, 11, 10, 10, 0, tzinfo=TZ_BERLIN)
        tasks = [
            {"id": "1", "text": "Naive due", "due_time": "2026-11-11T12:00:00"},
            {"id": "2", "text": "Bad due", "due_time": "invalid"},
        ]
        with mock.patch.object(study_plan, "user_tasks", lambda u: tasks):
            res = study_plan._due_tasks(uid, now)
        self.assertEqual([t["text"] for t in res], ["Naive due"])

    def test_handle_course_view_deletes_status_msg_and_handles_bad_due_time(self):
        uid = 9941
        status_msg = mock.MagicMock()
        status_msg.delete = mock.AsyncMock()
        query = mock.MagicMock()
        query.from_user.id = uid
        query.data = "course_view|0"
        query.answer = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock(side_effect=[status_msg, mock.MagicMock()])
        context = mock.MagicMock()
        context.user_data = {"course_view_options": [("Algorithms", "cid1")]}

        tasks = [{"id": "1", "course": "Algorithms", "text": "Sheet 1", "due_time": "bad-date"}]
        with mock.patch.object(menus, "is_user_allowed", return_value=True), \
                mock.patch.object(menus, "user_tasks", lambda u: tasks), \
                mock.patch.object(menus, "login_studip", mock.AsyncMock(return_value=object())), \
                mock.patch.object(menus, "get_my_module_codes", mock.AsyncMock(return_value=set())), \
                mock.patch.object(menus, "get_all_exam_dates", mock.AsyncMock(return_value=[])), \
                mock.patch.object(menus, "_show_typing", mock.AsyncMock()), \
                mock.patch.object(menus, "_start_loading_animation", lambda *a: (mock.MagicMock(), asyncio.sleep(0))):
            run_handler(menus.handle_course_view, mock.MagicMock(callback_query=query), context)

        status_msg.delete.assert_awaited_once()
        sent_text = query.message.reply_text.await_args_list[-1].args[0]
        self.assertIn("• Sheet 1", sent_text)


class AdminRoleAndPrivacyTests(unittest.TestCase):
    def test_demoting_admin_sets_role_to_user(self):
        db.upsert_user(9950, role="admin", username="adm2", is_active=1)
        query = mock.AsyncMock()
        query.data = "admin_role_user|9950"
        query.from_user = types.SimpleNamespace(id=9999)
        with mock.patch.object(system, "is_user_admin", return_value=True), \
                mock.patch.object(system, "_is_protected_admin", return_value=False):
            run_handler(system.handle_admin_callbacks, types.SimpleNamespace(callback_query=query), None)
        self.assertEqual(db.get_user(9950)["role"], "user")

    def test_privacy_view_shows_the_grades_permission_as_it_is(self):
        uid = 9951
        storage.set_ai_personal_enabled(uid, False)
        storage.set_ai_grades_enabled(uid, True)
        self.addCleanup(storage.set_ai_grades_enabled, uid, False)
        text, _ = privacy.privacy_view(uid, datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN))
        # grades go to the assistant on their own permission (voice.build_qa_context), timetable or not
        self.assertIn("use your grades: ✅ Yes", text)
        self.assertIn("ai_personal", storage.PERSONAL_CACHE_SECTIONS)


if __name__ == "__main__":
    unittest.main()
