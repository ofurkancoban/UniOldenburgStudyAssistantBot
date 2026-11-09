"""Fixes from the second debugging round: restored fast-enroll jobs, the
course material search, students' own sessions, per-student switches,
admin-only controls, work kept off the event loop, group chats, the Stud.IP
user id, long button data, personal data out of the shared cache,
requirements and the instance lock."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import auth, db, preferences, state, storage  # noqa: E402


def _callback(user_id: int, data: str):
    query = mock.MagicMock()
    query.from_user.id = user_id
    query.data = data
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.message.reply_text = mock.AsyncMock()
    return mock.MagicMock(callback_query=query, effective_user=query.from_user), query


class SharedCacheMixin:
    """The shared general cache as a plain dict for the test."""

    def setUp(self):
        self.cache: dict = {}
        for target in (storage,):
            for name, fn in (("load_general_cache", lambda: self.cache),
                             ("save_general_cache", lambda d: self.cache.update(d))):
                patcher = mock.patch.object(target, name, fn)
                patcher.start()
                self.addCleanup(patcher.stop)
        storage._legacy_checked.clear()
        self.addCleanup(storage._legacy_checked.clear)


class FastEnrollTests(unittest.TestCase):
    def test_a_job_runs_with_the_students_own_login(self):
        from unibot import tasks
        seen = []

        async def login(user_id=None):
            seen.append(("login", user_id, auth.current_user_id()))
            return "own-session"

        async def enroll(session, *args, **kwargs):
            seen.append(("enroll", session, auth.current_user_id()))
            return True, "done"

        context = mock.MagicMock()
        context.bot.send_message = mock.AsyncMock()
        with mock.patch.object(tasks, "login_studip", login), \
                mock.patch.object(tasks, "run_fast_enroll", enroll), \
                mock.patch.object(tasks, "clear_pending_job", lambda *a: None):
            asyncio.run(tasks._run_fastenroll_task(701, "sem", "10:00", None, context))
        self.assertEqual(seen, [("login", 701, 701), ("enroll", "own-session", 701)])

    def test_restore_skips_students_who_left(self):
        from unibot import enrollment
        spawned = []

        def spawn(coro, name=None):
            coro.close()
            spawned.append(name)
            return mock.MagicMock()

        app = mock.MagicMock()
        app.bot.send_message = mock.AsyncMock()
        pending = {"702": {"s1": {"target_time_str": "10:00"}}, "703": {"s2": {"target_time_str": "10:00"}},
                   "704": {"s3": {"target_time_str": "10:00"}}}
        states = {702: auth.ACTIVE, 703: auth.INACTIVE, 704: auth.ACTIVE}
        with mock.patch.object(enrollment, "load_pending", lambda: pending), \
                mock.patch.object(enrollment, "is_user_allowed", lambda uid: uid != 704), \
                mock.patch.object(enrollment, "lifecycle_state", lambda uid: states[uid]), \
                mock.patch.object(state, "spawn", spawn):
            asyncio.run(enrollment.restore_pending_fastenroll_jobs(app))
        self.assertEqual(spawned, ["fastenroll:702:s1"])
        enrollment.fastenroll_tasks.pop(702, None)


class CourseSearchTests(unittest.TestCase):
    def setUp(self):
        from unibot import study_plan
        self.sp = study_plan
        vec = study_plan._pack_vector([1.0, 0.0])
        study_plan.save_course_index({"files": {
            "mine|a.pdf": {"course": "Mine", "file": "a.pdf", "chunks": [{"text": "own", "vec": vec}]},
            "other|b.pdf": {"course": "Other", "file": "b.pdf", "chunks": [{"text": "foreign", "vec": vec}]},
        }})
        self.addCleanup(study_plan.save_course_index, {"files": {}})

    def _search(self, user_id, courses):
        async def embed(texts):
            return [[1.0, 0.0]]

        with mock.patch.object(self.sp, "embed_texts", embed), \
                mock.patch.object(self.sp, "student_courses", lambda uid: courses.get(uid, {})):
            return asyncio.run(self.sp.search_course_material("q", user_id=user_id))

    def test_only_the_students_own_courses_are_searched(self):
        hits = self._search(705, {705: {"mine": "Mine"}})
        self.assertEqual([h["text"] for h in hits], ["own"])

    def test_no_student_or_no_courses_finds_nothing(self):
        self.assertEqual(self._search(None, {}), [])
        self.assertEqual(self._search(706, {}), [])


class OwnSessionTests(unittest.TestCase):
    def test_a_student_never_gets_the_watcher_session(self):
        from unibot import study_plan
        lapsed = mock.MagicMock()
        lapsed.is_logged_in = mock.AsyncMock(return_value=False)

        async def login(user_id=None):
            return lapsed

        with mock.patch.object(study_plan, "get_user_session", lambda uid: lapsed), \
                mock.patch.object(study_plan, "login_studip", login), \
                mock.patch.object(state, "global_session", "watcher"):
            with self.assertRaises(RuntimeError):
                asyncio.run(study_plan._own_session(707))
            self.assertEqual(asyncio.run(study_plan._own_session(None)), "watcher")


class PerStudentSwitchTests(unittest.TestCase):
    def test_weekly_plan_and_previous_semester_are_per_student(self):
        preferences.set_pref(708, "weekly_study_plan", True)
        preferences.set_pref(708, "scan_previous_semester", True)
        self.assertTrue(preferences.get_pref(708, "weekly_study_plan"))
        self.assertFalse(preferences.get_pref(709, "weekly_study_plan"))
        self.assertFalse(preferences.get_pref(709, "scan_previous_semester"))

    def test_the_old_global_switch_carries_over_to_the_primary_admin(self):
        db.get_user_scope(710).save_private(preferences.PREFS_KIND, {"diet": "vegan"})
        db.get_user_scope(711).save_private(preferences.PREFS_KIND, {"diet": "vegan"})
        legacy = {"weekly_study_plan": lambda: True, "scan_previous_semester": lambda: True}
        with mock.patch.object(preferences, "PRIMARY_ADMIN_ID", 710), \
                mock.patch.dict(preferences._LEGACY_GLOBAL_SWITCHES, legacy):
            self.assertTrue(preferences.get_pref(710, "weekly_study_plan"))
            self.assertTrue(preferences.get_pref(710, "scan_previous_semester"))
            self.assertFalse(preferences.get_pref(711, "weekly_study_plan"))
        self.assertEqual(preferences.get_pref(710, "diet"), "vegan")  # the rest is kept

    def test_status_toggle_changes_only_the_tapping_student(self):
        from unibot import menus
        update, query = _callback(712, "status_group_toggle|semester|study_plan")
        with mock.patch.object(menus, "is_user_allowed", lambda uid: True), \
                mock.patch.object(menus, "_status_group_view", lambda group, uid: ("t", None)):
            asyncio.run(menus.handle_status_group(update, mock.MagicMock()))
        self.assertTrue(preferences.get_pref(712, "weekly_study_plan"))
        self.assertFalse(preferences.get_pref(713, "weekly_study_plan"))

    def test_the_weekly_plan_runs_only_for_students_who_turned_it_on(self):
        from unibot import watcher
        ran = []

        async def once(key, period, label, start, job, bot, uids, session):
            ran.append(list(uids))

        sunday = datetime(2026, 11, 8, 19, 0, tzinfo=TZ_BERLIN)
        with mock.patch.object(watcher, "_get_active_user_ids", lambda: [714, 715]), \
                mock.patch.object(watcher, "_guarded", mock.AsyncMock()), \
                mock.patch.object(watcher, "_run_once_per_period", once), \
                mock.patch.object(watcher, "get_pref", lambda uid, key: uid == 715), \
                mock.patch.object(watcher, "EXAM_FILE_SCAN_HOUR", 25), \
                mock.patch.object(watcher, "GWDG_API_KEY", ""):
            asyncio.run(watcher._run_scheduled_jobs(mock.MagicMock(), None, sunday))
        self.assertEqual(ran[0], [715])

    def test_wardrobe_switch_is_admin_only(self):
        from unibot import wardrobe
        update, query = _callback(716, "toggle_wardrobe_feature")
        before = storage.get_wardrobe_feature_enabled()
        with mock.patch.object(wardrobe, "is_user_admin", lambda uid: False):
            asyncio.run(wardrobe.handle_toggle_wardrobe_feature(update, mock.MagicMock()))
        self.assertEqual(storage.get_wardrobe_feature_enabled(), before)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))


class AdminControlTests(unittest.TestCase):
    def test_watcher_and_whatsapp_buttons_are_admin_only(self):
        from unibot import menus
        for data in sorted(menus._ADMIN_STATUS_BUTTONS):
            handler = mock.AsyncMock()
            update, query = _callback(717, data)
            with mock.patch.object(menus, "is_user_admin", lambda uid: False), \
                    mock.patch.dict(menus._STATUS_BUTTON_HANDLERS, {data: handler}):
                asyncio.run(menus.handle_status_buttons(update, mock.MagicMock()))
            handler.assert_not_called()
            self.assertTrue(query.answer.call_args.kwargs.get("show_alert"), data)

    def test_admins_still_reach_them(self):
        from unibot import menus
        handler = mock.AsyncMock()
        update, query = _callback(718, "stop_watchers")
        with mock.patch.object(menus, "is_user_admin", lambda uid: True), \
                mock.patch.dict(menus._STATUS_BUTTON_HANDLERS, {"stop_watchers": handler}):
            asyncio.run(menus.handle_status_buttons(update, mock.MagicMock()))
        handler.assert_awaited_once()

    def test_whatsapp_group_is_set_by_admins_only(self):
        from unibot import whatsapp
        update, query = _callback(719, "set_wa_group|123@g.us")
        saved = []
        with mock.patch.object(whatsapp, "is_user_admin", lambda uid: False), \
                mock.patch.object(whatsapp, "set_wa_group_id", saved.append):
            asyncio.run(whatsapp.set_wa_group_handler(update, mock.MagicMock()))
        self.assertEqual(saved, [])


class EventLoopTests(unittest.TestCase):
    def test_text_extraction_runs_in_a_worker_thread(self):
        from unibot import documents
        threads = []

        def extract(name, content):
            threads.append(threading.get_ident())
            return "text " * 500

        with mock.patch.object(documents, "extract_text_from_file", extract), \
                mock.patch.object(documents, "_pdf_page_count", lambda c: (threads.append(threading.get_ident()), 1)[1]):
            asyncio.run(documents.extract_text_from_file_async("a.pdf", b"%PDF"))
        self.assertEqual(len(threads), 2)
        self.assertNotIn(threading.get_ident(), threads)


class StudentCoursesTests(unittest.TestCase):
    def test_another_student_never_sees_the_watchers_courses(self):
        watcher = mock.MagicMock(user_id=720)
        with mock.patch.object(state, "global_session", watcher), \
                mock.patch.object(state, "courses_map", {"c1": "Admin course"}), \
                mock.patch.object(state, "user_courses_map", {}):
            self.assertEqual(storage.student_courses(721), {})
            self.assertEqual(storage.student_courses(720), {"c1": "Admin course"})
            self.assertEqual(storage.student_courses(None), {})


class GroupChatTests(unittest.TestCase):
    def test_updates_from_groups_are_dropped(self):
        update = mock.MagicMock()
        update.effective_user.id = 722
        update.effective_chat.type = "group"
        limited = mock.AsyncMock(return_value=False)
        with mock.patch.object(auth, "is_user_allowed", lambda uid: True), \
                mock.patch.object(auth, "_rate_limited", limited):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(update, mock.MagicMock()))
        limited.assert_not_called()


class StudipUserIdTests(unittest.TestCase):
    def test_unknown_id_raises_instead_of_an_empty_path(self):
        from unibot import studip_api

        async def profile(user_id=None, session=None):
            return {}

        with mock.patch.object(studip_api, "get_studip_user_profile", profile), \
                mock.patch.object(studip_api, "_uses_global_profile", lambda uid: False):
            with self.assertRaises(studip_api.StudipApiError):
                asyncio.run(studip_api.studip_api_user_id(user_id=723))
        with mock.patch.object(studip_api, "get_studip_user_profile", profile), \
                mock.patch.object(state, "_studip_api_user_id", None):
            with self.assertRaises(studip_api.StudipApiError):
                asyncio.run(studip_api.studip_api_user_id(user_id=None, session=None))


class FoodButtonTests(unittest.TestCase):
    def test_long_keywords_fit_telegrams_limit(self):
        from unibot import mensa
        keywords = ["x" * 60, "çok uzun bir yemek adı ve daha fazlası" * 2]
        for i, kw in enumerate(keywords):
            data = mensa.foodpref_remove_data(i, kw)
            self.assertLessEqual(len(data.encode("utf-8")), 64)
            self.assertEqual(mensa._resolve_foodpref_remove(data.split("|", 1)[1], keywords), kw)

    def test_a_stale_button_removes_nothing(self):
        from unibot import mensa
        ref = mensa.foodpref_remove_data(0, "pilz").split("|", 1)[1]
        self.assertIsNone(mensa._resolve_foodpref_remove(ref, ["zwiebel", "pilz"]))
        self.assertEqual(mensa._resolve_foodpref_remove("S", ["pilz"]), "S")  # an old button's value


class PersonalRecordTests(SharedCacheMixin, unittest.TestCase):
    def _raw(self, user_id: int, kind: str) -> str:
        row = db._connection().execute(
            "SELECT encrypted_blob FROM user_private_data WHERE user_id = ? AND kind = ?", (user_id, kind),
        ).fetchone()
        return row[0] if row else ""

    def test_old_shared_entries_move_into_the_students_record(self):
        self.cache["travel_modes"] = {"724": "bus", "725": "bike"}
        self.assertEqual(storage.get_travel_mode(724), "bus")
        self.assertNotIn("724", self.cache["travel_modes"])
        self.assertIn("725", self.cache["travel_modes"])  # other students untouched until they are read
        raw = self._raw(724, "cache.travel_modes")
        self.assertTrue(raw)
        self.assertNotIn("bus", raw)

    def test_new_personal_data_never_lands_in_the_shared_cache(self):
        storage.record_study_block(726, int(time.time()), "sent", course="Secret course", minutes=30)
        storage.set_study_buddy_enabled(726, True)
        storage.save_cached_outfit_today(726, "blue shirt", None)
        storage.record_worn_items(726, ["i1"])
        dumped = json.dumps(self.cache)
        for text in ("Secret course", "blue shirt", "726"):
            self.assertNotIn(text, dumped)
        self.assertTrue(storage.get_study_buddy_enabled(726))
        self.assertEqual(storage.get_recently_worn_item_ids(726), {"i1"})
        self.assertEqual(storage.get_cached_outfit_today(726)["description"], "blue shirt")

    def test_calendar_feed_link_finds_its_student_by_hash_only(self):
        token = storage.calendar_token(727)
        self.assertNotIn(token, json.dumps(self.cache))
        self.assertEqual(storage.user_for_calendar_token(token), 727)
        new = storage.reset_calendar_token(727)
        self.assertIsNone(storage.user_for_calendar_token(token))
        self.assertEqual(storage.user_for_calendar_token(new), 727)
        self.assertIsNone(storage.user_for_calendar_token("guess"))

    def test_an_old_calendar_link_keeps_working(self):
        self.cache["calendar_tokens"] = {"728": "legacy-token"}
        self.assertEqual(storage.user_for_calendar_token("legacy-token"), 728)
        self.assertNotIn("728", self.cache["calendar_tokens"])
        self.assertEqual(storage.user_for_calendar_token("legacy-token"), 728)

    def _schedule_patches(self, fan_outs):
        from unibot import course_registry, schedule

        async def fan_out(bot, cid, kind, text, title_text, **kwargs):
            fan_outs.append((cid, kind, set(kwargs.get("members") or ()), title_text))
            self.urgent.append(kwargs.get("urgent", False))
            return {}

        self.urgent: list = []

        return [mock.patch.object(schedule, "load_general_cache", lambda: self.cache),
                mock.patch.object(schedule, "save_general_cache", lambda d: self.cache.update(d)),
                mock.patch.object(course_registry, "fan_out", fan_out)]

    def test_session_changes_reach_only_the_courses_students(self):
        from unibot import course_registry, monitoring, schedule
        noon = datetime(2026, 11, 10, 12, 0, tzinfo=TZ_BERLIN)

        def event(cancelled):
            return {"id": "e1", "attributes": {"start": "2026-11-12T10:00:00+01:00", "end": "2026-11-12T12:00:00+01:00",
                                               "is-cancelled": cancelled, "location": "A1"}}

        feeds = {"c1": [event(False)], "c2": [event(False)]}

        async def get_all(path, session=None, **kwargs):
            return feeds[path.split("/")[2]], None

        async def scan(cid, primary, fetch):
            return await fetch(primary), primary

        fan_outs: list = []
        plan = [("Course One", "c1", "s1"), ("Course Two", "c2", "s2")]
        patches = self._schedule_patches(fan_outs) + [
            mock.patch.object(monitoring, "watched_courses", mock.AsyncMock(return_value=plan)),
            mock.patch.object(course_registry, "scan_with_failover", scan),
            mock.patch.object(schedule, "studip_api_get_all", get_all),
            mock.patch.object(state, "course_members", {"c1": {801, 802}, "c2": {803}}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        first = asyncio.run(schedule.check_course_session_changes(None, now=noon, force=True))
        self.assertEqual((first["courses"], fan_outs), (2, []))  # the first scan is the baseline
        feeds["c1"] = [event(True)]
        asyncio.run(schedule.check_course_session_changes(None, now=noon, force=True))
        self.assertEqual([(cid, kind, members) for cid, kind, members, _ in fan_outs], [("c1", "schedule", {801, 802})])
        self.assertIn("CANCELLED", fan_outs[0][3])
        self.assertEqual(self.urgent, [True])  # two days ahead: comes right away, with sound
        feeds["c2"] = [{"id": "e1", "attributes": {"start": "2026-11-20T10:00:00+01:00",
                                                   "end": "2026-11-20T12:00:00+01:00", "is-cancelled": True}}]
        asyncio.run(schedule.check_course_session_changes(None, now=noon, force=True))
        self.assertEqual((fan_outs[-1][0], self.urgent[-1]), ("c2", False))  # ten days ahead: their own way
        self.assertIn("Course One", fan_outs[0][3])
        self.assertNotIn("801", json.dumps(self.cache))  # the shared state holds course facts only

    def test_each_student_gets_their_own_timetable_changes(self):
        from unibot import schedule

        def slot(room):
            return [{"id": "s1", "weekday": 1, "start": "10:00", "end": "12:00", "title": "Course", "location": room}]

        timetables = {811: slot("A1"), 812: slot("B2")}

        async def fetch(timestamp=None, user_id=None, session=None):
            return timetables[user_id]

        accounts = [mock.MagicMock(user_id=811, session="s811"), mock.MagicMock(user_id=812, session="s812")]
        fan_outs: list = []
        patches = self._schedule_patches(fan_outs) + [
            mock.patch.object(schedule, "signed_in_accounts", lambda: accounts),
            mock.patch.object(schedule, "fetch_user_schedule_api", fetch),
            mock.patch.dict(schedule._timetable_checked_at, clear=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(asyncio.run(schedule.check_personal_timetable_changes(None)), 2)
        self.assertEqual(fan_outs, [])
        schedule._timetable_checked_at.clear()
        timetables[811] = slot("C3")
        asyncio.run(schedule.check_personal_timetable_changes(None))
        self.assertEqual([(cid, members) for cid, _, members, _ in fan_outs], [(None, {811})])
        self.assertEqual(storage.personal_record(812, "user_schedule_slots")["s1"]["location"], "B2")
        self.assertNotIn("C3", json.dumps(self.cache))


class UrgentDeliveryTests(unittest.TestCase):
    def _deliver(self, fmt, reason, urgent):
        from unibot import course_registry
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        queued = []
        with mock.patch.object(course_registry, "broadcast_targets", lambda cid, members: {830}), \
                mock.patch.object(course_registry, "delivery_format", lambda uid, cid, kind: fmt), \
                mock.patch.object(course_registry, "_hold_reason", lambda uid, now: reason), \
                mock.patch.object(course_registry, "get_pref", lambda uid, key: "card"), \
                mock.patch.object(course_registry, "queue_alert", lambda *a: queued.append(a)), \
                mock.patch.object(course_registry.asyncio, "sleep", mock.AsyncMock()):
            counts = asyncio.run(course_registry.fan_out(bot, "c1", "schedule", "text", "title", urgent=urgent))
        return counts, bot.send_message.await_args_list

    def test_urgent_comes_with_sound_and_skips_the_digest(self):
        counts, sent = self._deliver("silent", "digest", urgent=True)
        self.assertEqual(counts["full"], 1)
        self.assertFalse(sent[0].kwargs["disable_notification"])
        self.assertEqual(sent[0].kwargs["text"], "text")

    def test_urgent_still_respects_quiet_hours_and_mute(self):
        self.assertEqual(self._deliver("silent", "quiet", urgent=True)[0]["queued"], 1)
        self.assertEqual(self._deliver(None, None, urgent=True)[0]["skipped"], 1)

    def test_not_urgent_keeps_the_students_choice(self):
        counts, sent = self._deliver("silent", None, urgent=False)
        self.assertTrue(sent[0].kwargs["disable_notification"])
        self.assertEqual(self._deliver("silent", "digest", urgent=False)[0]["queued"], 1)


class RequirementsTests(unittest.TestCase):
    def test_brotli_is_a_declared_dependency(self):
        with open(os.path.join(ROOT, "requirements.txt")) as f:
            names = {line.split(">=")[0].split("==")[0].strip().lower() for line in f if line.strip() and not line.startswith("#")}
        self.assertIn("brotli", names)


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.lock = os.path.join(tempfile.mkdtemp(prefix="unibot-lock-"), ".bot_instance.lock")
        for target, name, value in ((auth, "LOCK_FILE", self.lock), (auth.sys, "argv", ["studip_bot.py"])):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(auth.atexit, "register", lambda fn: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, pid: int) -> None:
        with open(self.lock, "w") as f:
            f.write(str(pid))

    def _process(self, *extra):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *extra])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_a_reused_pid_of_another_program_does_not_block_the_start(self):
        self._write(self._process().pid)
        self.assertTrue(auth.acquire_instance_lock())
        with open(self.lock) as f:
            self.assertEqual(f.read(), str(os.getpid()))

    def test_a_running_bot_still_blocks_the_start(self):
        self._write(self._process("studip_bot.py").pid)
        self.assertFalse(auth.acquire_instance_lock())

    def test_our_own_pid_after_a_restart_is_stale(self):
        self._write(os.getpid())
        self.assertTrue(auth.acquire_instance_lock())

    def test_release_leaves_another_instances_lock_alone(self):
        self._write(os.getpid() + 1)
        auth.release_instance_lock()
        self.assertTrue(os.path.exists(self.lock))
        self._write(os.getpid())
        auth.release_instance_lock()
        self.assertFalse(os.path.exists(self.lock))


if __name__ == "__main__":
    unittest.main()
