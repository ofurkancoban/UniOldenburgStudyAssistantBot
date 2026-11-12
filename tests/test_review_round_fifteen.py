"""Fixes from the full-project review (batch 1): the watcher pause buttons use
the flag the watcher reads, long and markup-heavy messages are cut into valid
parts, the weather line is escaped for its message, and the privacy texts name
the services that really receive the data."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_as, run_handler, use_temp_db  # noqa: E402
use_temp_db()

from unibot import formatting, menus, privacy, state, weather  # noqa: E402

UID = 9901


class WatcherPauseButtonTests(unittest.TestCase):
    def test_stop_and_start_flip_the_flag_the_watcher_reads(self):
        query = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID))
        with mock.patch.object(state, "global_watcher_paused", False):
            asyncio.run(menus._status_button_stop_watchers(update, None, query))
            self.assertTrue(state.global_watcher_paused)
            asyncio.run(menus._status_button_start_watchers(update, None, query))
            self.assertFalse(state.global_watcher_paused)


class EscapedChunkTests(unittest.TestCase):
    def test_every_part_fits_once_escaped(self):
        parts = formatting.escaped_chunks("&" * 3500 + "<b>" * 200, 3500)
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        self.assertEqual("".join(parts).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"),
                         "&" * 3500 + "<b>" * 200)

    def test_no_entity_is_cut(self):
        for part in formatting.escaped_chunks("a&b" * 2000, 100):
            self.assertNotRegex(part, r"&[a-z]*$")


class SplitHtmlTests(unittest.TestCase):
    def test_open_tags_are_closed_and_reopened(self):
        text = "<b>" + "\n".join(f"line {i}" for i in range(400)) + "</b>"
        parts = formatting.split_html(text, 500)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 500)
            self.assertEqual(part.count("<b>"), part.count("</b>"))

    def test_a_very_long_line_is_cut_inside_its_text(self):
        parts = formatting.split_html("<i>" + "word &amp; " * 1000 + "</i>", 400)
        for part in parts:
            self.assertLessEqual(len(part), 400)
            self.assertEqual(part.count("<i>"), part.count("</i>"))
            self.assertNotRegex(part.replace("</i>", ""), r"&[a-z]*$")

    def test_short_text_is_one_part(self):
        self.assertEqual(formatting.split_html("<b>hi</b>\nthere"), ["<b>hi</b>\nthere"])


class WeatherLineTests(unittest.TestCase):
    SNAP = {"emoji": "x", "is_wet": False, "description": "light rain", "temp_min": 8, "temp_max": 12,
            "precip_chance": 10, "location_name": "Bad_Zwischenahn *north*"}

    def test_markdown_screen_gets_escaped_place(self):
        from studip_utils import md_escape
        line = weather.build_weather_text_line(self.SNAP, escape=md_escape)
        self.assertIn("Bad\\_Zwischenahn \\*north\\*", line)

    def test_html_message_gets_escaped_place(self):
        line = weather.build_weather_text_line({**self.SNAP, "location_name": "A & <B>"})
        self.assertIn("A &amp; &lt;B&gt;", line)


class ServiceNamesTests(unittest.TestCase):
    def test_privacy_screen_names_the_real_services(self):
        text, _markup = privacy.privacy_view(UID)
        for name in ("GWDG", "OpenRouter", "Google"):
            self.assertIn(name, text)


class FastEnrollSpinTests(unittest.TestCase):
    def test_the_final_wait_lets_other_tasks_run(self):
        import time as _time
        from datetime import datetime, timedelta
        import fast_enroll
        from studip_utils import TZ_BERLIN
        counter = []

        async def ticker():
            while True:
                counter.append(1)
                await asyncio.sleep(0)

        async def scenario():
            target = datetime.now(TZ_BERLIN) + timedelta(seconds=0.2)
            task = asyncio.ensure_future(ticker())
            try:
                with mock.patch.object(fast_enroll, "resolve_target", lambda *a: target), \
                        mock.patch.object(fast_enroll, "_site_clock_offset", mock.AsyncMock(return_value=0.0)), \
                        mock.patch.object(fast_enroll, "_fetch_security_token", mock.AsyncMock(return_value="t")), \
                        mock.patch.object(fast_enroll, "_is_enrollment_open", mock.AsyncMock(return_value=False)), \
                        mock.patch.object(fast_enroll, "_submit_enrollment", mock.AsyncMock(return_value=(200, "<title>ok</title>"))):
                    started = _time.time()
                    ok, _message = await fast_enroll.run_fast_enroll(object(), "s", "00:00:00")
                    return ok, _time.time() - started
            finally:
                task.cancel()

        ok, took = asyncio.run(scenario())
        self.assertTrue(ok)
        self.assertGreaterEqual(took, 0.19)  # it still waits for the target
        self.assertGreater(len(counter), 20)  # and other tasks ran meanwhile (a busy loop starves them)


class ZipConcurrencyTests(unittest.TestCase):
    def test_one_zip_is_built_at_a_time_and_the_limit_is_lower(self):
        from unibot import studip_api
        self.assertEqual(studip_api.ZIP_RAW_LIMIT_BYTES, 100 * 1024 * 1024)
        running, peak = [0], [0]

        async def build(*args, **kwargs):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.01)
            running[0] -= 1
            return (None, 0, "x", 0, 0)

        async def scenario():
            with mock.patch.object(studip_api, "_build_zip", build):
                await asyncio.gather(*(studip_api.create_recursive_zip("c", "u", "r", session=object(), user_id=UID)
                                       for _ in range(4)))

        asyncio.run(scenario())
        self.assertEqual(peak[0], 1)


class TaskReminderRetryTests(unittest.TestCase):
    def setUp(self):
        activate_student(UID)

    def _run(self, error):
        from unibot import tasks
        removed = []
        task = {"id": "t1", "text": "Hand in", "due_time": "2020-01-01T10:00:00+01:00", "user_id": UID}
        bot = mock.AsyncMock()
        bot.send_message.side_effect = error
        with mock.patch.object(tasks, "students_with_tasks", lambda: [UID]), \
                mock.patch.object(tasks, "user_tasks", lambda uid: [task]), \
                mock.patch.object(tasks, "remove_user_tasks", lambda uid, ids: removed.append(set(ids))), \
                mock.patch.object(tasks, "remember_user_callback", lambda *a: True), \
                mock.patch.object(tasks, "record_task_outcome", lambda *a: None):
            asyncio.run(tasks.check_task_reminders(bot))
        return removed

    def test_an_unexpected_error_keeps_the_task_for_the_next_minute(self):
        self.assertEqual(self._run(RuntimeError("boom")), [])

    def test_a_blocked_chat_preserves_the_task(self):
        from telegram.error import Forbidden
        self.assertEqual(self._run(Forbidden("bot was blocked by the user")), [])


class WeeklyPlanCatchUpTests(unittest.TestCase):
    def _runs(self, moment, last_done=None):
        from unibot import watcher
        from studip_utils import TZ_BERLIN
        calls = []

        async def once(key, period, *args):
            calls.append((key, period))

        with mock.patch.object(watcher, "_get_active_user_ids", lambda: [UID]), \
                mock.patch.object(watcher, "get_pref", lambda uid, key: True), \
                mock.patch.object(watcher, "run_morning_summary_schedule", mock.AsyncMock()), \
                mock.patch.object(watcher, "GWDG_API_KEY", ""), \
                mock.patch.object(watcher, "_run_once_per_period", once):
            asyncio.run(watcher._run_scheduled_jobs(None, None, moment.replace(tzinfo=TZ_BERLIN)))
        return [c for c in calls if c[0] == "last_weekly_study_plan"]

    def test_sunday_evening_and_monday_morning_share_the_period(self):
        from datetime import datetime
        sunday = self._runs(datetime(2026, 11, 8, 19, 0))   # a Sunday
        monday = self._runs(datetime(2026, 11, 9, 8, 0))    # the Monday after
        self.assertEqual(len(sunday), 1)
        self.assertEqual(sunday, monday)  # same period key: no second plan once Sunday's went out

    def test_other_times_do_not_run(self):
        from datetime import datetime
        self.assertEqual(self._runs(datetime(2026, 11, 9, 13, 0)), [])
        self.assertEqual(self._runs(datetime(2026, 11, 8, 10, 0)), [])


class BlockedStudentAlertTests(unittest.TestCase):
    def test_a_blocked_account_is_not_an_audience(self):
        from unibot import db, telegram_ui
        db.upsert_user(telegram_id=9951, role="user")
        db.upsert_user(telegram_id=9952, role="user")
        db.get_user_scope(9951).sync_courses({"c1"})
        db.get_user_scope(9952).sync_courses({"c1"})
        db.set_user_active(9952, False)
        targets = telegram_ui.broadcast_targets("c1", members={9951, 9952})
        self.assertIn(9951, targets)
        self.assertNotIn(9952, targets)


class UploadRespectsTitleOnlyTests(unittest.TestCase):
    def setUp(self):
        activate_student(UID, {"c1": "Course"})

    def _fan(self, fmt):
        from unibot import course_registry as cr
        bot = mock.AsyncMock()
        sent_full = []

        async def send_full(b, uid):
            sent_full.append(uid)
            return True

        with mock.patch.object(cr, "delivery_format", lambda uid, cid, kind: fmt), \
                mock.patch.object(cr, "get_pref", lambda uid, key: "upload" if key == "file_delivery" else None), \
                mock.patch.object(cr, "_hold_reason", lambda uid, now: None), \
                mock.patch.object(cr, "broadcast_targets", lambda cid, members=None: {UID}), \
                mock.patch.object(cr.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(cr.fan_out(bot, "c1", "files", "FULL", "TITLE", send_full=send_full))
        return sent_full, bot.send_message.call_args

    def test_title_only_course_gets_a_line_not_the_file(self):
        sent_full, call = self._fan("title")
        self.assertEqual(sent_full, [])
        self.assertEqual(call.kwargs["text"], "TITLE")

    def test_other_formats_still_upload_the_file(self):
        for fmt in ("full", "silent"):
            sent_full, _call = self._fan(fmt)
            self.assertEqual(sent_full, [UID])


class ButtonTextTests(unittest.TestCase):
    def test_buttons_old_and_new_are_recognised(self):
        cases = {"📁 Files": "files", "⬇️ Files": "files", "📅 Schedule": "calendar", "✅ Tasks": "tasks",
                 "🍽️ Mensa": "menu", "📝 Exams": "exams", "🔄 Check": "check", "🔁 Check": "check",
                 "⚙️ Status": "status", "ℹ️ Status": "status", "exam": "exams", "Status": "status"}
        for text, target in cases.items():
            self.assertEqual(menus.button_target(text), target, text)

    def test_sentences_are_not_buttons(self):
        for text in ("status of my exam?", "please check the task list for tomorrow", "I want to start studying",
                     "what is the schedule for friday", ""):
            self.assertIsNone(menus.button_target(text), text)

    def test_a_sentence_gets_a_hint_not_a_command(self):
        message = mock.AsyncMock()
        message.text = "status of my exam?"
        update = types.SimpleNamespace(callback_query=None, message=message, effective_message=message,
                                       effective_user=types.SimpleNamespace(id=UID))
        with mock.patch.object(menus, "is_user_allowed", return_value=True), \
                mock.patch.object(menus, "handle_pending_step", mock.AsyncMock(return_value=False)), \
                mock.patch.object(menus, "status_command", mock.AsyncMock()) as status, \
                mock.patch.object(menus, "exams_command", mock.AsyncMock()) as exams:
            run_handler(menus.handle_reply_buttons, update, types.SimpleNamespace(user_data={}))
        status.assert_not_awaited()
        exams.assert_not_awaited()
        self.assertIn("buttons", message.reply_text.call_args.args[0])


class CourseViewCallbackTests(unittest.TestCase):
    def test_a_garbled_index_says_the_list_expired(self):
        query = mock.AsyncMock()
        query.data = "course_view|abc"
        query.from_user = types.SimpleNamespace(id=UID)
        update = types.SimpleNamespace(callback_query=query)
        context = types.SimpleNamespace(user_data={"course_view_options": [("A", "c1")]})
        with mock.patch.object(menus, "is_user_allowed", return_value=True):
            run_handler(menus.handle_course_view, update, context)
        self.assertIn("expired", query.message.reply_text.call_args.args[0])


class OAuthRefreshPerAccountTests(unittest.TestCase):
    def test_one_students_failed_refresh_does_not_block_another(self):
        import studip_oauth
        studip_oauth._refresh_blocked.clear()
        expired = {"access_token": "old", "refresh_token": "r", "expires_at": 0}
        fresh = {"access_token": "new", "refresh_token": "r2", "expires_at": 9e12}

        async def refresh(tokens, user_id=None):
            if user_id == 1:
                raise RuntimeError("down")
            return fresh

        with mock.patch.object(studip_oauth, "load_tokens", lambda user_id=None: expired), \
                mock.patch.object(studip_oauth, "refresh", refresh):
            self.assertIsNone(asyncio.run(studip_oauth.get_access_token(user_id=1)))
            self.assertEqual(asyncio.run(studip_oauth.get_access_token(user_id=2)), "new")
        studip_oauth._refresh_blocked.clear()


class TaskButtonsSurviveRestartTests(unittest.TestCase):
    def test_announcement_and_forum_task_buttons_are_stored(self):
        from unibot import monitoring_alerts
        for cache in (monitoring_alerts.task_suggestion_cache, monitoring_alerts.announcement_task_cache):
            self.assertEqual(type(cache).__name__, "_PersistentLinkCache")


class ConfigWarningAndRecipientTests(unittest.TestCase):
    def test_empty_allowlist_warning_is_accurate(self):
        from unibot import config
        with mock.patch.object(config, "TELEGRAM_TOKEN", "t"), mock.patch.object(config, "ALLOWED_USER_IDS", set()):
            warnings = config.validate_settings()
        text = " ".join(warnings)
        self.assertIn("invite code", text)
        self.assertNotIn("every Telegram user will be rejected", text)

    def test_the_whatsapp_service_is_told_who_the_administrator_is(self):
        import subprocess
        env = {**os.environ, "ALLOWED_USER_IDS": "5,6", "ADMIN_USER_IDS": "6",
               "BOT_LOG_FILE": os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")}
        out = subprocess.run([sys.executable, "-c", "import os, unibot.config; print(os.environ['WA_NOTIFY_USER_ID'])"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "6")


class FirstDietTests(unittest.TestCase):
    def test_halal_is_only_carried_over_with_the_old_food_file(self):
        from unibot import db, preferences
        with mock.patch.object(preferences, "PRIMARY_ADMIN_ID", 9961):
            self.assertEqual(preferences.get_prefs(9961)["diet"], "all")  # a fresh install
            db.save("food_preferences.json", {"avoid_codes": ["Kn"], "avoid_keywords": []})
            try:
                with mock.patch.object(preferences, "PRIMARY_ADMIN_ID", 9962):
                    prefs = preferences.get_prefs(9962)
                self.assertEqual(prefs["diet"], "halal")
                self.assertEqual(prefs["food_avoid_codes"], ["Kn"])
            finally:
                db.delete("food_preferences.json")


class SignInIsNotActivityTests(unittest.TestCase):
    def test_the_profile_link_leaves_last_active_alone(self):
        import studip_oauth
        import stums_client
        from unibot import auth, db, studip_api
        uid = 9963
        db.upsert_user(telegram_id=uid, role="user")
        with db._lock:
            db._connection().execute("UPDATE users SET last_active_at = 1000 WHERE telegram_id = ?", (uid,))
        session = mock.MagicMock()
        session.session = None
        session.save_cookies = mock.AsyncMock()
        session.is_html_logged_in = mock.AsyncMock(return_value=True)
        with mock.patch.object(auth, "get_user_session", lambda user_id=None: session), \
                mock.patch.object(studip_oauth, "oauth_enabled", return_value=False), \
                mock.patch.object(studip_api, "get_studip_user_profile",
                                  mock.AsyncMock(return_value={"username": "ab1234", "display_name": "A B"})), \
                mock.patch.object(stums_client, "_establish_stums_session", mock.AsyncMock(return_value="r")), \
                mock.patch.object(auth.state, "_menu_app", None):
            asyncio.run(auth._on_proxy_session_authenticated(uid))
        user = db.get_user(uid)
        self.assertEqual(user["studip_username"], "ab1234")
        self.assertEqual(user["last_active_at"], 1000)


class RegistrySemesterTests(unittest.TestCase):
    def test_sync_does_not_write_one_students_semester_into_the_shared_table(self):
        from unibot import course_registry, db, storage
        storage.save_default_semester("w26", "WiSe 26/27", user_id=9964)
        course_registry.sync_registry({"cs1": "Statistics"}, {"cs1": {9964}}, {9964})
        self.assertIsNone(db.get_global_course("cs1")["semester"])


class AudioSummarySessionTests(unittest.TestCase):
    def setUp(self):
        activate_student(UID, {"c1": "A", "c2": "B"})
        from unibot import storage
        storage.set_ai_personal_enabled(UID, True)
        self.addCleanup(storage.set_ai_personal_enabled, UID, False)

    def test_the_fresh_link_is_fetched_with_the_students_own_session(self):
        from unibot import documents
        query = mock.AsyncMock()
        query.data = "file_audio_sum|s7"
        query.from_user = types.SimpleNamespace(id=UID)
        query.message.reply_text = mock.AsyncMock(return_value=mock.AsyncMock())
        own = object()
        fresh = mock.AsyncMock(return_value=None)
        with mock.patch.dict(documents.link_cache, {"s7": {"cid": "c1", "name": "slides.pdf", "user_id": UID}}), \
                mock.patch.object(documents, "is_user_allowed", return_value=True), \
                mock.patch.object(documents, "login_studip", mock.AsyncMock(return_value=own)), \
                mock.patch.object(documents, "get_fresh_file_url", fresh), \
                mock.patch.object(documents, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(documents, "_stop_loading_animation", mock.AsyncMock()):
            run_handler(documents.handle_file_audio_summary, types.SimpleNamespace(callback_query=query), None)
        self.assertIs(fresh.call_args.kwargs["session"], own)
        self.assertEqual(fresh.call_args.kwargs["user_id"], UID)


class FolderViewCleanupTests(unittest.TestCase):
    def test_the_cache_is_pruned_twice_not_three_times(self):
        query = mock.AsyncMock()
        calls = []
        with mock.patch.object(menus, "cleanup_link_cache", lambda: calls.append(1)):
            asyncio.run(menus.send_folder(query, [{"type": "file", "name": "a.pdf", "size": "1", "modified": "x",
                                                    "url": "https://elearning.uni-oldenburg.de/f"}], "c1", UID))
        self.assertEqual(len(calls), 2)


class EnrollTimeFormatTests(unittest.TestCase):
    def setUp(self):
        activate_student(UID)

    def test_short_times_are_accepted(self):
        from unibot.enrollment import normalize_enroll_time, validate_fastenroll_args
        self.assertEqual(normalize_enroll_time("8:30"), "08:30:00")
        self.assertEqual(normalize_enroll_time("08:30"), "08:30:00")
        self.assertEqual(normalize_enroll_time("08:30:15"), "08:30:15")
        self.assertEqual(normalize_enroll_time("soon"), "soon")
        self.assertIsNone(validate_fastenroll_args("a" * 32, normalize_enroll_time("8:30"), None))
        self.assertIsNotNone(validate_fastenroll_args("a" * 32, normalize_enroll_time("soon"), None))

    def test_the_scheduler_takes_a_short_time(self):
        from unibot import schedule
        saved = []

        async def run(*args):
            return None

        with mock.patch.object(schedule, "save_pending_job", lambda *a: saved.append(a)), \
                mock.patch("unibot.tasks._run_fastenroll_task", run), \
                mock.patch.dict(schedule.fastenroll_tasks, {}, clear=True):
            reply = run_as(UID, schedule.schedule_fastenroll_job(UID, "a" * 32, "8:30", None, None))
        self.assertIn("scheduled", reply)
        self.assertEqual(saved[0][2], "08:30:00")


class TaskTimePhraseTests(unittest.TestCase):
    NOW = None

    def _p(self, text):
        from datetime import datetime
        from studip_utils import TZ_BERLIN
        from unibot.tasks import _parse_task_due_time
        return _parse_task_due_time(text, datetime(2026, 11, 11, 10, 0, tzinfo=TZ_BERLIN))  # a Wednesday

    def test_new_phrases(self):
        self.assertEqual(self._p("tomorrow").isoformat(), "2026-11-12T09:00:00+01:00")
        self.assertEqual(self._p("yarın").isoformat(), "2026-11-12T09:00:00+01:00")
        self.assertEqual(self._p("next friday").isoformat(), "2026-11-13T09:00:00+01:00")
        self.assertEqual(self._p("friday 15:30").isoformat(), "2026-11-13T15:30:00+01:00")
        self.assertEqual(self._p("Mittwoch").isoformat(), "2026-11-18T09:00:00+01:00")  # never today
        self.assertEqual(self._p("14:00").isoformat(), "2026-11-11T14:00:00+01:00")
        self.assertEqual(self._p("09:00").isoformat(), "2026-11-12T09:00:00+01:00")  # already past today

    def test_nonsense_is_still_rejected(self):
        for text in ("hello", "sometime", "next never"):
            with self.assertRaises(ValueError):
                self._p(text)


class EnvDocumentationTests(unittest.TestCase):
    """Every setting .env.example documents is read by the code; nothing
    documents a password, 2FA key or switch that does nothing."""

    def test_every_documented_setting_is_used(self):
        import re
        import subprocess
        files = subprocess.run(["git", "ls-files", "*.py", "*.js"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        code = ""
        for name in files:
            if name.startswith(("tests/", "whatsapp_service/package")):
                continue
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                code += fh.read()
        with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as fh:
            keys = re.findall(r"^#?\s*([A-Z][A-Z0-9_]{3,})=", fh.read(), re.MULTILINE)
        self.assertTrue(keys)
        unused = [k for k in keys if k not in code]
        self.assertEqual(unused, [], f"documented but never read: {unused}")

    def test_no_password_settings_are_documented(self):
        for name in (".env.example", "README.md"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                text = fh.read()
            for key in ("PASSWORD=", "TOTP_SECRET=", "HEADLESS=", "SET_MENU_BUTTON="):
                self.assertNotIn(key, text, f"{name} documents {key}")


class DeadCodeTests(unittest.TestCase):
    def test_the_unused_text_deleter_is_gone(self):
        from unibot import telegram_ui
        self.assertFalse(hasattr(telegram_ui, "delete_text"))


def _html_problems(part: str, limit: int) -> list[str]:
    """What Telegram would reject in one HTML message: too long, unbalanced
    tags, a bare '<' or '&', or an entity cut in half."""
    import re
    from html.parser import HTMLParser
    problems = []
    if len(part) > limit:
        problems.append(f"{len(part)} characters")
    stack, errors = [], []

    class Checker(HTMLParser):
        def handle_starttag(self, tag, attrs):
            stack.append(tag)

        def handle_endtag(self, tag):
            if not stack or stack[-1] != tag:
                errors.append(f"</{tag}> closes {stack[-1:] or 'nothing'}")
            else:
                stack.pop()

    checker = Checker(convert_charrefs=False)
    checker.feed(part)
    checker.close()
    if stack:
        problems.append(f"unclosed {stack}")
    problems += errors
    plain = re.sub(r"<[^>]*>", "", part)
    if re.search(r"&(?!(amp|lt|gt|quot|#\d+);)", plain):
        problems.append("bare or cut '&'")
    if "<" in plain or ">" in plain:
        problems.append("bare '<' or '>'")
    return problems


class LargeAccountMessageTests(unittest.TestCase):
    """A student with far more courses than any real one (production has at most
    22, titles up to 92 characters): every message part must be valid and fit."""

    def _plan(self, n):
        names = {f"c{i}": f"Course {i}: R&D <Advanced> Topics in Economics & Statistics, Part {i} " + "x" * 40 for i in range(n)}
        sessions = []
        for i in range(n):
            sessions.append({"day": ["Mon", "Tue", "Wed", "Thu", "Fri"][i % 5], "date": f"2026-11-{9 + i % 5:02d}",
                             "start": "09:00", "end": "10:30", "pieces": [
                                 {"cid": f"c{i}", "unit": f"u{i}", "course": names[f"c{i}"], "unit_label": "Lecture " + "L&M " * 20,
                                  "reason": "behind", "title": "Read slides <chapter> & exercises", "minutes": 45,
                                  "task_minutes": 90, "part": "1/2"}]})
        stats = {"needed": {c: 120 for c in names}, "scheduled": {c: 45 for c in names}, "names": names,
                 "free_minutes": 900, "missed_prep": []}
        rows = [{"name": names[c], "backlog": [1, 2, 3], "backlog_minutes": 180} for c in names]
        return {"sessions": sessions, "stats": stats}, rows

    def test_weekly_plan_for_60_courses_splits_into_valid_parts(self):
        from unibot.monitoring import render_weekly_study_plan_message
        plan, rows = self._plan(60)
        text = render_weekly_study_plan_message(plan, [], [], rows)
        self.assertGreater(len(text), 8000)  # really needs several messages
        parts = formatting.split_html(text, 3800)
        self.assertGreater(len(parts), 2)
        for i, part in enumerate(parts):
            self.assertEqual(_html_problems(part, 4096), [], f"part {i}")
        import re
        strip = lambda t: re.sub(r"<[^>]*>|\s+", "", t)  # noqa: E731
        self.assertEqual(strip("".join(parts)), strip(text))  # nothing lost or doubled

    def test_full_message_view_of_a_huge_inbox_message(self):
        body = ("Dear students, R&D <meeting> & lab: please read & reply. " * 400)
        parts = formatting.escaped_chunks(body, 3500)
        header = "✉️ <b>FULL MESSAGE</b>\n" + "x" * 300
        for part in parts:
            self.assertEqual(_html_problems(header + part, 4096), [])

    def test_random_html_always_splits_into_valid_parts(self):
        import random
        import re
        rng = random.Random(20261111)
        words = ["alpha", "R&amp;D", "&lt;x&gt;", "lecture", "Übung", "😀", "a" * 150, "\n", "\n\n", " "]
        for case in range(300):
            out, depth = [], []
            for _ in range(rng.randint(5, 400)):
                roll = rng.random()
                if roll < 0.12 and len(depth) < 4:
                    tag = rng.choice(["b", "i", "u", "code"])
                    out.append(f"<{tag}>")
                    depth.append(tag)
                elif roll < 0.24 and depth:
                    out.append(f"</{depth.pop()}>")
                else:
                    out.append(rng.choice(words))
            out += [f"</{t}>" for t in reversed(depth)]
            text = "".join(out)
            limit = rng.choice([300, 800, 3800])
            parts = formatting.split_html(text, limit)
            for part in parts:
                self.assertEqual(_html_problems(part, limit), [], f"case {case} limit {limit}")
            strip = lambda t: re.sub(r"<[^>]*>|\s+", "", t)  # noqa: E731
            self.assertEqual(strip("".join(parts)), strip(text), f"case {case}")


class PersonalCalendarWindowTests(unittest.TestCase):
    """/users/{id}/events answers with the events from filter[timestamp] on for two
    weeks and nothing before it (Stud.IP's UserEventsIndex); the calendar must ask
    from the first day of the week, or a Monday to Wednesday meeting never shows."""

    WEEK = None

    def _events(self, days):
        from datetime import date
        from unibot import schedule
        week_start = date(2026, 11, 9)  # a Monday
        meetings = {"Team meeting": "2026-11-10T10:00:00+01:00",   # Tuesday: yesterday's case
                    "Supervisor call": "2026-11-09T08:30:00+01:00",  # Monday
                    "Colloquium": "2026-11-13T14:00:00+01:00"}       # Friday
        asked = []

        async def get_all(path, params=None, session=None, user_id=None):
            if path.endswith("/events") and path.startswith("/users/"):
                from datetime import datetime, timedelta
                since = datetime.fromtimestamp(params["filter[timestamp]"], schedule.TZ_BERLIN)
                asked.append(since.date())
                until = since + timedelta(days=14)  # what Stud.IP really answers
                return [{"id": title, "attributes": {"title": title, "start": start, "end": start.replace("T10", "T11")
                                                      .replace("T08", "T09").replace("T14", "T15")}}
                        for title, start in meetings.items()
                        if since <= datetime.fromisoformat(start) < until], []
            return [], []

        with mock.patch.object(schedule, "studip_api_get_all", get_all), \
                mock.patch.object(schedule, "studip_api_user_id", mock.AsyncMock(return_value="u1")), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[])):
            events = asyncio.run(schedule._get_calendar_events_api(week_start, days=days, user_id=UID, session=object()))
        return events, asked

    def test_monday_to_wednesday_meetings_are_shown(self):
        events, asked = self._events(7)
        self.assertEqual(sorted(e["title"] for e in events), ["Colloquium", "Supervisor call", "Team meeting"])
        self.assertEqual(asked[0].isoformat(), "2026-11-09")  # from the first day, not from Thursday

    def test_a_longer_range_asks_in_two_week_steps(self):
        _events, asked = self._events(21)
        self.assertEqual([d.isoformat() for d in asked], ["2026-11-09", "2026-11-23"])

    def test_a_failing_personal_calendar_is_logged(self):
        from datetime import date
        from unibot import schedule

        async def get_all(path, params=None, session=None, user_id=None):
            if path.startswith("/users/"):
                raise RuntimeError("HTTP 500")
            return [], []

        with mock.patch.object(schedule, "studip_api_get_all", get_all), \
                mock.patch.object(schedule, "studip_api_user_id", mock.AsyncMock(return_value="u1")), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[])), \
                self.assertLogs(level="WARNING") as logs:
            asyncio.run(schedule._get_calendar_events_api(date(2026, 11, 9), user_id=UID, session=object()))
        self.assertIn("Personal calendar events unavailable", " ".join(logs.output))


class CourseDatesOutsideTheTimetableTests(unittest.TestCase):
    """A course whose dates are all single appointments (a mentor programme, a
    block seminar) is not in /users/{id}/schedule; its dates still belong in the
    calendar."""

    COURSE = "mentorcourse"

    def setUp(self):
        from unibot import schedule
        schedule._COURSE_EVENTS_CACHE.clear()
        self.addCleanup(schedule._COURSE_EVENTS_CACHE.clear)
        self.calls = []

    def _dates(self):
        def date(start, end, **extra):
            return {"id": start, "attributes": {"title": "Mentor programme cohort 3", "start": start, "end": end,
                                                 "location": extra.get("location", ""), "is-cancelled": extra.get("cancelled", False)},
                    "meta": {"topics": [{"title": "Mentor meeting"}]} if extra.get("topic") else {}}
        return [date("2023-11-07T10:15:00+01:00", "2023-11-07T11:15:00+01:00"),   # years ago
                date("2026-11-09T09:00:00+01:00", "2026-11-09T10:00:00+01:00", topic=True),  # Monday
                date("2026-11-24T19:00:00+01:00", "2026-11-24T22:00:00+01:00")]    # a later week

    def _week(self, fail_for=()):
        from datetime import date
        from unibot import schedule

        async def get_all(path, params=None, session=None, user_id=None):
            self.calls.append(path)
            if path == f"/courses/{self.COURSE}/events":
                if self.COURSE in fail_for:
                    raise RuntimeError("HTTP 403")
                return self._dates(), []
            return [], []

        with mock.patch.object(schedule, "studip_api_get_all", get_all), \
                mock.patch.object(schedule, "studip_api_user_id", mock.AsyncMock(return_value="u1")), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, "student_courses", lambda uid: {self.COURSE: "Mentor programme"}), \
                mock.patch.object(schedule, "_fetch_course_rooms_map", mock.AsyncMock(return_value={})):
            return asyncio.run(schedule.get_calendar_events(object(), date(2026, 11, 9), user_id=UID))

    def test_the_monday_meeting_shows_and_other_weeks_do_not(self):
        events = self._week()
        self.assertEqual([e["start_dt"].strftime("%a %H:%M") for e in events], ["Mon 09:00"])
        self.assertEqual(events[0]["topics"], ["Mentor meeting"])
        self.assertFalse(events[0]["is_cancelled"])
        self.assertIs(events[0]["lesson"], False)  # shown, but not as a lecture

    def test_the_course_is_asked_once_within_ten_minutes(self):
        self._week()
        self._week()
        self.assertEqual(self.calls.count(f"/courses/{self.COURSE}/events"), 1)

    def test_a_course_that_cannot_be_read_does_not_break_the_calendar(self):
        self.assertEqual(self._week(fail_for=(self.COURSE,)), [])
        from unibot import schedule
        self.assertNotIn(self.COURSE, schedule._COURSE_EVENTS_CACHE)  # a failure is not remembered

    def test_a_timetable_course_is_not_asked_twice(self):
        from datetime import date
        from unibot import schedule
        slot = {"id": "s1", "type": "x", "title": "Lecture", "description": "", "start": "10:00", "end": "12:00",
                "weekday": 2, "location": "A1", "recurrence": {}, "course_id": self.COURSE}

        async def get_all(path, params=None, session=None, user_id=None):
            self.calls.append(path)
            return [], []

        with mock.patch.object(schedule, "studip_api_get_all", get_all), \
                mock.patch.object(schedule, "studip_api_user_id", mock.AsyncMock(return_value="u1")), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[slot])), \
                mock.patch.object(schedule, "student_courses", lambda uid: {self.COURSE: "Lecture"}):
            asyncio.run(schedule._get_calendar_events_api(date(2026, 11, 9), user_id=UID, session=object()))
        self.assertEqual(self.calls.count(f"/courses/{self.COURSE}/events"), 1)


class ReminderHeadingTests(unittest.TestCase):
    def setUp(self):
        activate_student(UID)

    def _reminder(self, **extra):
        """The reminder texts sent 45 minutes before a 10:00 event."""
        from tests.test_review_round_thirteen import _Frozen, _at, _class
        from unibot import schedule
        sent = []

        async def send_message(**kwargs):
            sent.append(kwargs["text"])

        bot = mock.MagicMock()
        bot.send_message = send_message
        _Frozen.frozen = _at("09:15")
        event = {**_class("Mentor programme", "10:00"), **extra}
        wet = {"is_wet": True, "emoji": "x", "description": "rain", "temp_min": 5, "temp_max": 9,
               "precip_chance": 80, "location_name": "Oldenburg"}
        with mock.patch.object(schedule, "datetime", _Frozen), \
                mock.patch.object(schedule, "get_calendar_events", mock.AsyncMock(return_value=[event])), \
                mock.patch.object(schedule, "get_user_session", lambda uid=None: None), \
                mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=wet)), \
                mock.patch.object(schedule, "load_reminders_cache", lambda user_id=None: {}), \
                mock.patch.object(schedule, "save_reminders_cache", lambda data, user_id=None: None):
            asyncio.run(schedule.check_calendar_reminders(bot, UID, silent=True))
        return [t for t in sent if "REMINDER" in t]

    def test_a_regular_lesson_says_lecture(self):
        for extra in ({}, {"lesson": True}):
            (text,) = self._reminder(**extra)
            self.assertIn("LECTURE REMINDER", text)

    def test_a_meeting_date_says_meeting(self):
        (text,) = self._reminder(lesson=False, categories="Meeting")
        self.assertIn("MEETING REMINDER", text)
        self.assertNotIn("LECTURE", text)

    def test_other_dates_say_appointment(self):
        for categories in ("", "Sonstiges", "Exkursion"):
            (text,) = self._reminder(lesson=False, categories=categories)
            self.assertIn("APPOINTMENT REMINDER", text)

    def test_the_headings(self):
        from unibot.schedule import reminder_heading
        self.assertEqual(reminder_heading({"lesson": True}), "LECTURE REMINDER")
        self.assertEqual(reminder_heading({"lesson": False, "categories": "Sitzung"}), "MEETING REMINDER")
        self.assertEqual(reminder_heading({"lesson": False}), "APPOINTMENT REMINDER")

    def test_dates_get_their_flag_from_where_they_come(self):
        from datetime import date
        from unibot import schedule

        async def get_all(path, params=None, session=None, user_id=None):
            if path == "/courses/tt1/events":
                return [{"id": "e1", "attributes": {"title": "Statistics", "start": "2026-11-10T10:00:00+01:00",
                                                     "end": "2026-11-10T12:00:00+01:00"}}], []
            if path == "/courses/mc/events":
                return [{"id": "e2", "attributes": {"title": "Mentor programme", "start": "2026-11-09T09:00:00+01:00",
                                                     "end": "2026-11-09T10:00:00+01:00", "categories": "Meeting"}}], []
            if path.startswith("/users/"):
                return [{"id": "p1", "attributes": {"title": "Dentist", "start": "2026-11-11T08:00:00+01:00",
                                                     "end": "2026-11-11T09:00:00+01:00"}}], []
            return [], []

        slot = {"id": "s1", "type": "x", "title": "Statistics", "description": "", "start": "10:00", "end": "12:00",
                "weekday": 2, "location": "A1", "recurrence": {}, "course_id": "tt1"}
        with mock.patch.object(schedule, "studip_api_get_all", get_all), \
                mock.patch.object(schedule, "studip_api_user_id", mock.AsyncMock(return_value="u1")), \
                mock.patch.object(schedule, "fetch_user_schedule_api", mock.AsyncMock(return_value=[slot])), \
                mock.patch.object(schedule, "student_courses", lambda uid: {"tt1": "Statistics", "mc": "Mentor"}):
            events = asyncio.run(schedule._get_calendar_events_api(date(2026, 11, 9), user_id=UID, session=object()))
        flags = {e["title"]: e["lesson"] for e in events}
        self.assertEqual(flags, {"Statistics": True, "Mentor programme": False, "Dentist": False})


if __name__ == "__main__":
    unittest.main()
