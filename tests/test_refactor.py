"""Tests for the extracted helpers, the SQLite store and the HTML/JSON parsers.

Parsers run against saved sample pages in tests/fixtures, so a change in how
the bot reads Stud.IP or the Mensa shows up here instead of as silently
missing notifications.

Run from the project root:
    .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
# Tests must never touch the real bot database
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def _import(name):
    cwd = os.getcwd()
    module = __import__(f"unibot.{name}", fromlist=[name])
    os.chdir(cwd)
    return module


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class FakeResponse:
    def __init__(self, status=200, body="", payload=None):
        self.status, self.body, self.payload = status, body, payload

    async def text(self):
        return self.body

    async def json(self):
        return json.loads(json.dumps(self.payload))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """aiohttp-like session answering from a {url: FakeResponse} table."""

    def __init__(self, routes):
        self.routes = routes

    async def get(self, url, **kwargs):
        response = self.routes[url]
        if isinstance(response, Exception):
            raise response
        return response


# ── SQLite document store ─────────────────────────────────────────────────────
class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = _import("db")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        if self.db.exists(self.path):
            self.db.delete(self.path)
        self.tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        self.assertFalse(self.db.exists(self.path))
        self.db.save(self.path, {"ü": [1, 2], "nested": {"ok": True}})
        self.assertTrue(self.db.exists(self.path))
        self.assertEqual(self.db.load(self.path), {"ü": [1, 2], "nested": {"ok": True}})
        self.assertFalse(os.path.exists(self.path))  # nothing written to disk

    def test_missing_document_raises_like_open(self):
        with self.assertRaises(FileNotFoundError):
            self.db.load(self.path)

    def test_legacy_file_is_imported_once(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"watch_prev": True}, fh)
        self.assertEqual(self.db.load(self.path), {"watch_prev": True})
        # later edits of the old file are ignored, the database wins
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"watch_prev": False}, fh)
        self.assertEqual(self.db.load(self.path), {"watch_prev": True})

    def test_invalid_legacy_file_raises_value_error(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            self.db.load(self.path)

    def test_failed_save_keeps_previous_value(self):
        self.db.save(self.path, {"a": 1})
        with self.assertRaises(TypeError):
            self.db.save(self.path, {"bad": object()})
        self.assertEqual(self.db.load(self.path), {"a": 1})

    def test_delete_removes_row_and_legacy_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"id": "x"}, fh)
        self.db.load(self.path)
        self.db.delete(self.path)
        self.assertFalse(self.db.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_survives_reconnect(self):
        self.db.save(self.path, [1, 2, 3])
        self.db.close()
        self.assertEqual(self.db.load(self.path), [1, 2, 3])

    def test_storage_uses_database(self):
        storage = _import("storage")
        with mock.patch.object(storage, "DEFAULT_SEMESTER_CACHE_PATH", self.path):
            # an explicit student: without one the owner is PRIMARY_ADMIN_ID,
            # which is unset where there is no .env (CI)
            storage.save_default_semester("sem1", "WS 26/27", user_id=4242)
            self.assertEqual(storage.load_default_semester(4242)["id"], "sem1")
            storage.clear_default_semester(4242)
            self.assertEqual(storage.load_default_semester(4242), {})
            self.assertFalse(self.db.exists(self.path))


# ── settings ─────────────────────────────────────────────────────────────────
class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = _import("config")

    def test_missing_token_stops_startup(self):
        with mock.patch.object(self.config, "TELEGRAM_TOKEN", ""):
            with self.assertRaises(SystemExit):
                self.config.validate_settings()

    def test_warnings(self):
        with mock.patch.multiple(self.config, TELEGRAM_TOKEN="1:x", ALLOWED_USER_IDS=set(),
                                 WEATHER_LATITUDE="north", WEATHER_LONGITUDE="200",
                                 CLOUDFLARE_TUNNEL="named", OPENROUTER_API_KEY=None, GWDG_API_KEY=""):
            warnings = " | ".join(self.config.validate_settings())
        for expected in ("ALLOWED_USER_IDS", "WEATHER_LATITUDE", "WEATHER_LONGITUDE", "CLOUDFLARE_TUNNEL", "AI features"):
            self.assertIn(expected, warnings)

    def test_valid_settings_have_no_warnings(self):
        with mock.patch.multiple(self.config, TELEGRAM_TOKEN="1:x", ALLOWED_USER_IDS={1},
                                 WEATHER_LATITUDE="53.1", WEATHER_LONGITUDE="8.2",
                                 CLOUDFLARE_TUNNEL="quick", OPENROUTER_API_KEY="k", GWDG_API_KEY=""):
            self.assertEqual(self.config.validate_settings(), [])

    def test_no_module_reads_the_environment_directly(self):
        import glob
        for path in glob.glob(os.path.join(ROOT, "unibot", "*.py")):
            if path.endswith("config.py"):
                continue
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("os.getenv(", fh.read(), path)


# ── Mensa menu parser ────────────────────────────────────────────────────────
class MensaMenuTests(unittest.TestCase):
    def setUp(self):
        self.mensa = _import("mensa")
        self.session = FakeSession({f"{self.mensa.BASE_URL}/plugins.php/mensawidget/menu/2/": FakeResponse(body=fixture("mensa_menu.html"))})

    def menu(self, **prefs):
        return asyncio.run(self.mensa.get_todays_menu_enhanced(self.session, "2/", **prefs))

    def test_dishes_categories_and_order(self):
        text, prev_link, next_link, day, dishes, header = self.menu()
        self.assertEqual([d["category"] for d in dishes][:3], ["🍴 COUNTER 1", "🍴 COUNTER 1", "🍕 PIZZA"])
        names = [d["name"] for d in dishes]
        self.assertIn("Pizza Margherita", names)  # unnamed table detected as pizza
        self.assertIn("Mystery Bowl", names)  # unknown category kept, sorted last
        self.assertEqual(dishes[-1]["category"], "🍴 Unknown Counter")
        self.assertIn("Mittwoch, 04.11.2026", header)
        self.assertTrue(prev_link.endswith("/previous") and next_link.endswith("/next"))
        self.assertIsInstance(day, date)

    def test_prices_and_limited_flag(self):
        dishes = {d["name"]: d for d in self.menu()[4]}
        self.assertEqual(dishes["Pork Schnitzel ⭐"]["price"], "4,20€")
        self.assertEqual(dishes["Tomato Soup"]["price"], "")  # "€" alone means no price
        self.assertTrue(dishes["Pork Schnitzel ⭐"]["is_limited"])
        self.assertTrue(dishes["Pizza Margherita"]["is_limited"] is False)

    def test_star_not_doubled(self):
        text = self.menu()[0]
        self.assertIn("<b>Pork Schnitzel ⭐</b>\n", text)

    def test_food_preferences_hide_dishes(self):
        text, *_rest, dishes, _h = self.menu(avoid_codes=["Sw"], avoid_keywords=["curry"])
        names = {d["name"] for d in dishes}
        self.assertNotIn("Pork Schnitzel ⭐", names)
        self.assertNotIn("Pork pudding", names)
        self.assertFalse(any("Curry" in n for n in names))
        self.assertIn("3 dish(es) hidden", text)
        self.assertNotIn("DESSERTS", text)  # category with only hidden dishes disappears

    def test_allergen_guide_lists_codes_on_the_page(self):
        text = self.menu()[0]
        guide = text.split("ALLERGEN GUIDE")[1]
        for code in ("Ei", "Mi", "Se", "Sl", "Sw"):
            self.assertIn(f"<b>{code}</b>", guide)
        self.assertNotIn("<b>Gl</b>", guide)  # unknown code not explained

    def test_closed_and_failed(self):
        session = FakeSession({f"{self.mensa.BASE_URL}/plugins.php/mensawidget/menu/2/": FakeResponse(body="<div></div>")})
        text, _p, _n, day, dishes, header = asyncio.run(self.mensa.get_todays_menu_enhanced(session))
        self.assertIn("Mensa might be closed", text)
        self.assertEqual((day, dishes, header), (None, [], None))
        broken = FakeSession({f"{self.mensa.BASE_URL}/plugins.php/mensawidget/menu/2/": RuntimeError("down")})
        self.assertIn("could not be loaded", asyncio.run(self.mensa.get_todays_menu_enhanced(broken))[0])

    def test_translate_food_codes_exact(self):
        t = self.mensa.translate_food_codes
        self.assertEqual(t("Sw,We,1"), "🍷,🌾,🎨")
        self.assertEqual(t("10,1,12"), "🔬,🎨,🍫")
        self.assertEqual(t("V+"), "🌿")
        self.assertEqual(t("Gl,Se"), "Gl,🌱")
        self.assertEqual(t(""), "")


# ── Stud.IP file list (HTML page) ────────────────────────────────────────────
class StudipFilesHtmlTests(unittest.TestCase):
    def test_files_and_folders_from_page(self):
        api = _import("studip_api")
        from unibot import state
        url = f"{api.BASE_URL}/dispatch.php/course/files?cid=c1"
        with mock.patch.object(state, "global_session", FakeSession({url: FakeResponse(body=fixture("studip_files.html"))})):
            items = asyncio.run(api._list_files_html("c1"))
        by_name = {i["name"]: i for i in items}
        self.assertEqual(by_name["Week 1"]["type"], "folder")
        self.assertTrue(by_name["Week 1"]["url"].startswith(api.BASE_URL))
        slides = by_name["Lecture 1 & Intro.pdf"]
        self.assertEqual(slides["type"], "file")
        self.assertEqual(slides["size"], "2.0 MB")
        self.assertEqual(slides["modified"], "04.11.2026 10:00")
        self.assertEqual(by_name["notes.txt"]["size"], "512 B")
        self.assertEqual(by_name["notes.txt"]["modified"], "-")  # bad timestamp ignored

    def test_fetch_failure_returns_empty(self):
        api = _import("studip_api")
        from unibot import state
        url = f"{api.BASE_URL}/dispatch.php/course/files?cid=c1"
        with mock.patch.object(state, "global_session", FakeSession({url: RuntimeError("down")})):
            self.assertEqual(asyncio.run(api._list_files_html("c1")), [])


# ── course schedule change alerts ────────────────────────────────────────────
class ScheduleChangeTests(unittest.TestCase):
    def setUp(self):
        self.schedule = _import("schedule")
        self.alert = self.schedule._session_change_alert

    def prev(self, **kw):
        base = {"is_cancelled": False, "location": "A01", "time": "10:00 - 12:00", "date": "Thu, 05.11.2026"}
        base.update(kw)
        return base

    def call(self, prev, cancelled=False, loc="A01", time="10:00 - 12:00", day="Thu, 05.11.2026"):
        return self.alert("Statistik <I>", day, time, loc, cancelled, prev)

    def test_first_sighting(self):
        self.assertIsNone(self.call(None))
        self.assertIn("CANCELLED", self.call(None, cancelled=True))

    def test_transitions(self):
        self.assertIn("CANCELLED", self.call(self.prev(), cancelled=True))
        self.assertIn("RESTORED", self.call(self.prev(is_cancelled=True)))
        room = self.call(self.prev(), loc="B02")
        self.assertIn("ROOM CHANGED", room)
        self.assertIn("<s>A01</s>", room)
        self.assertIn("RESCHEDULED", self.call(self.prev(), time="12:00 - 14:00"))
        self.assertIsNone(self.call(self.prev()))
        self.assertIsNone(self.call(self.prev(is_cancelled=True), cancelled=True))
        self.assertIsNone(self.call(self.prev(), loc=""))  # room just missing: no alert

    def test_titles_are_escaped(self):
        self.assertIn("Statistik &lt;I&gt;", self.call(None, cancelled=True))

    def test_timetable_diff(self):
        old = {"s1": {"title": "A", "weekday": 1, "weekday_name": "Monday", "start": "10:00", "end": "12:00", "location": "X"},
               "s2": {"title": "B", "weekday": 2, "weekday_name": "Tuesday", "start": "08:00", "end": "10:00", "location": ""}}
        new = {"s1": dict(old["s1"], start="11:00"),
               "s3": {"title": "C", "weekday": 3, "weekday_name": "Wednesday", "start": "09:00", "end": "11:00", "location": "Y"}}
        alerts = self.schedule._diff_timetable(old, new)
        self.assertEqual([a.split("\n")[0] for a in alerts], [
            "🔄 <b>TIMETABLE SCHEDULE UPDATED</b>",
            "📅 <b>NEW COURSE ADDED TO TIMETABLE</b>",
            "ℹ️ <b>COURSE REMOVED FROM TIMETABLE</b>",
        ])
        self.assertEqual(self.schedule._diff_timetable(old, old), [])

    def test_event_state_pruned_after_21_days(self):
        now = datetime(2026, 11, 4, 10, 0, tzinfo=self.schedule.TZ_BERLIN)
        state = {"old": {"ts": int((now - timedelta(days=22)).timestamp())}, "recent": {"ts": int(now.timestamp())}}
        _alerts, updated = self.schedule._diff_course_events([], state, now)
        self.assertTrue(updated)
        self.assertEqual(list(state), ["recent"])


# ── new file detection ───────────────────────────────────────────────────────
class FileWatcherTests(unittest.TestCase):
    def setUp(self):
        self.mon = _import("monitoring")

    def test_update_rules(self):
        same = {"size": "1 MB", "modified_ts": 1000}
        self.assertFalse(self.mon._file_was_updated(same, dict(same)))
        self.assertTrue(self.mon._file_was_updated(same, {"size": "2 MB", "modified_ts": 1000}))
        self.assertFalse(self.mon._file_was_updated(same, {"size": "1 MB", "modified_ts": 1000 + 3600}))
        self.assertTrue(self.mon._file_was_updated(same, {"size": "1 MB", "modified_ts": 1000 + 2 * 86400}))
        self.assertFalse(self.mon._file_was_updated(same, {"size": "1 MB", "modified_ts": None}))

    def test_diff(self):
        old = {"a.pdf": {"size": "1 MB", "modified_ts": 0}}
        cur = {"a.pdf": {"name": "a.pdf", "size": "2 MB", "modified_ts": 0}, "b.pdf": {"name": "b.pdf", "size": "1 MB"}}
        new_files, changed = self.mon._diff_course_files(cur, old)
        self.assertEqual([f["name"] for f in new_files], ["b.pdf"])
        self.assertEqual([f["name"] for f in changed], ["a.pdf"])

    def test_alert_escapes_names_and_offers_summary(self):
        it = {"type": "new", "course": "R&D <Lab>", "name": "Notes & <draft>.pdf", "modified": "04.11.2026", "size": "1 MB"}
        text, markup = self.mon._file_alert(it, "sid1")
        self.assertIn("R&amp;D &lt;Lab&gt;", text)
        self.assertIn("Notes &amp; &lt;draft&gt;.pdf", text)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, ["file|sid1", "file_audio_sum|sid1"])
        _text, markup = self.mon._file_alert(dict(it, name="data.csv"), "sid2")
        self.assertEqual(len(markup.inline_keyboard), 1)


# ── forum posts (JSON:API) ───────────────────────────────────────────────────
class ForumTests(unittest.TestCase):
    def setUp(self):
        self.mon = _import("monitoring")
        self.data = json.loads(fixture("studip_forum_postings.json"))

    def test_thread_entry(self):
        disc = {"id": "d1", "attributes": {"title": "Exam & dates"}, "meta": {"unread-postings-count": 1}}
        entry = self.mon._forum_thread_entry("Statistik", "c1", disc, self.data)
        self.assertEqual(entry["key"], "c1:Exam & dates:d1")
        self.assertTrue(entry["is_new"])
        self.assertEqual(entry["author"], "Student B")
        self.assertIn("Dr. Müller &lt;TA&gt;", entry["body"])  # author names escaped
        self.assertIn("Conversation History", entry["body"])
        self.assertEqual(entry["last_message_plain"], "Thanks!")
        self.assertEqual(entry["date"], "2026-11-04 11:30")

    def test_long_threads_keep_the_last_15(self):
        posts = {"data": [{"attributes": {"content-html": f"<p>m{i}</p>", "mkdate": ""}} for i in range(20)]}
        entry = self.mon._forum_thread_entry("C", "c", {"id": "d", "attributes": {"title": "T"}}, posts)
        self.assertEqual(entry["body"].count("👤"), 15)
        self.assertIn("older messages omitted", entry["body"])
        self.assertFalse(entry["is_new"])

    def test_empty_thread(self):
        self.assertIsNone(self.mon._forum_thread_entry("C", "c", {"id": "d"}, {"data": []}))


# ── morning summary sections ─────────────────────────────────────────────────
class MorningSummaryTests(unittest.TestCase):
    def setUp(self):
        self.morning = _import("morning")

    def test_exam_countdown_labels(self):
        today = date(2026, 11, 4)
        exams = [("Statistik", datetime(2026, 11, 4, 9)), ("Mikro & Makro", datetime(2026, 11, 5, 9)), ("Ökonometrie", datetime(2026, 11, 14, 9))]
        text = self.morning._format_exam_countdown(exams, today)
        self.assertIn("<b>Statistik</b> today", text)
        self.assertIn("<b>Mikro &amp; Makro</b> tomorrow", text)
        self.assertIn("in 10 days (14.11.2026)", text)
        self.assertEqual(self.morning._format_exam_countdown([], today), "")

    def test_calendar_failure_does_not_break_the_summary(self):
        # It used to leave `events` undefined and crash with NameError.
        with mock.patch.object(self.morning, "get_calendar_events", side_effect=RuntimeError("down")):
            events, today_events = asyncio.run(self.morning._fetch_today_events(object(), date(2026, 11, 4)))
        self.assertEqual((events, today_events), ([], []))
        self.assertEqual(self.morning._format_study_windows_section(events, date(2026, 11, 4)).count("Study"), 1)

    def test_tasks_are_sorted(self):
        tz = self.morning.TZ_BERLIN
        today = date(2026, 11, 4)
        tasks = [{"user_id": 1, "text": "late", "due_time": datetime(2026, 11, 4, 18, tzinfo=tz).isoformat()},
                 {"user_id": 1, "text": "early", "due_time": datetime(2026, 11, 4, 8, tzinfo=tz).isoformat()},
                 {"user_id": 2, "text": "someone else's", "due_time": datetime(2026, 11, 4, 9, tzinfo=tz).isoformat()},
                 {"user_id": 1, "text": "tomorrow", "due_time": datetime(2026, 11, 5, 8, tzinfo=tz).isoformat()}]
        with mock.patch.object(self.morning, "user_tasks", lambda uid: [t for t in tasks if t.get("user_id") == uid]):
            loaded = self.morning._load_today_tasks(today, user_id=1)
            self.assertEqual(self.morning._load_today_tasks(today), [])  # no user: nobody's tasks
        self.assertEqual([t["text"] for t in loaded], ["early", "late"])
        section = self.morning._format_tasks_section(loaded)
        self.assertLess(section.index("early"), section.index("late"))


# ── watcher cycle ────────────────────────────────────────────────────────────
class _StopWatcher(BaseException):
    """Breaks out of the endless watcher loop (it only catches Exception)."""


class WatcherCycleTests(unittest.TestCase):
    def test_one_cycle_runs_every_check_and_survives_failures(self):
        watcher = _import("watcher")
        calls = []

        def job(name, fails=False, result=None):
            async def run(*args, **kwargs):
                calls.append(name)
                if fails:
                    raise RuntimeError(f"{name} broke")
                return result
            return run

        async def fake_sleep(seconds):
            if seconds == 180:  # end of the first full cycle
                raise _StopWatcher

        cache = {}
        sent = []

        async def broadcast(bot, text, **kwargs):
            sent.append(text)

        patches = {
            "ALLOWED_USER_IDS": {1},
            "PRIMARY_ADMIN_ID": 1,
            "login_studip": job("login", result=object()),
            "run_morning_summary_schedule": job("morning"),
            "get_pref": lambda uid, key: False,
            "check_calendar_reminders": job("calendar"),
            # one ACTIVE, signed-in student, whatever other tests left in the database
            "_get_active_user_ids": lambda: [1],
            "has_stored_login": lambda session: True,
            # the StuMS checks never run at night: a fixed slot keeps the test
            # independent of the clock (and of accounts other tests left)
            "_stums_users": lambda: [],
            "_personal_due": lambda uid, now: "test-slot",
            "_mark_personal_slot": lambda uid, slot: None,
            "adopt_legacy_exam_cache": lambda uid: None,
            "check_course_session_changes": job("schedule_changes", fails=True),
            "check_personal_timetable_changes": job("timetable"),
            "check_task_reminders": job("tasks"),
            "check_exam_reminders": job("exam_registrations", fails=True),
            "check_grade_reminders": job("grades"),
            "check_tuition_reminders": job("tuition"),
            "check_exam_date_reminders": job("exam_dates"),
            "check_file_exam_reminders": job("file_exams"),
            "scan_course_files_for_exams": job("scan", result=[]),
            "notify_new_file_exam_events": job("notify"),
            "index_course_files": job("index"),
            "run_health_checks": job("health", result={"studip": (False, "down <now>")}),
            "check_new_announcements_parallel": job("announcements"),
            "check_new_files_parallel": job("files"),
            "check_new_forum_posts_parallel": job("forum"),
            "watched_courses": mock.AsyncMock(return_value={}),  # no Stud.IP course lists in a unit test
            "broadcast": broadcast,
            "load_general_cache": lambda: dict(cache),
            "save_general_cache": lambda data: cache.update(data),
            "update_general_cache": lambda data: cache.update(data),
            "load_exam_cache": lambda: {},
        }
        app = mock.MagicMock()
        from unibot import state
        with mock.patch.multiple(watcher, **patches), \
                mock.patch.object(watcher.asyncio, "sleep", fake_sleep), \
                mock.patch.object(state, "global_session", None), \
                mock.patch.object(state, "global_watcher_paused", False), \
                mock.patch.object(watcher.stagger, "is_night", lambda now: False):  # any time of day
            with self.assertRaises(_StopWatcher):
                asyncio.run(watcher.unified_watcher_controller(app))

        for expected in ("morning", "calendar", "schedule_changes", "timetable", "tasks", "exam_registrations", "grades",
                         "tuition", "exam_dates", "file_exams", "health", "announcements", "forum"):
            self.assertIn(expected, calls)
        self.assertIn("files", calls)  # throttled to every 30 min, so due on the first cycle
        self.assertEqual(cache["health_state"], {"studip": False})
        self.assertIn("last_exam_date_check", cache)
        self.assertTrue(any("Health check failed" in t and "down &lt;now&gt;" in t for t in sent))

    def test_stums_checks_follow_the_students_staggered_slots(self):
        watcher = _import("watcher")
        from unibot import stagger
        cache, calls, exam_cache, sent_to = {}, [], {}, []

        def job(name):
            async def run(*args, **kwargs):
                calls.append(name)
                if name in ("registrations", "grades", "tuition"):
                    session, bot, send = args[:3]
                    await send(bot, "personal")
            return run

        class Bot:
            async def send_message(self, chat_id, **kwargs):
                sent_to.append(chat_id)

        patches = dict(
            load_general_cache=lambda: dict(cache), update_general_cache=lambda d: cache.update(d),
            load_exam_cache=lambda *a, **k: exam_cache, ALLOWED_USER_IDS={5}, PRIMARY_ADMIN_ID=5, _stums_users=lambda: [],
            check_calendar_reminders=job("calendar"), check_course_session_changes=job("changes"),
            check_personal_timetable_changes=job("timetable"),
            check_task_reminders=job("tasks"), check_exam_reminders=job("registrations"),
            check_grade_reminders=job("grades"), check_tuition_reminders=job("tuition"),
        )
        day = datetime(2026, 11, 5, tzinfo=watcher.TZ_BERLIN).date()
        first, second = stagger.personal_slots(5, day, season=False)

        def cycle(at):
            calls.clear()
            asyncio.run(watcher._run_reminder_checks(Bot(), object(), at))
            return {c for c in calls if c in ("registrations", "grades", "tuition")}

        everything = {"registrations", "grades", "tuition"}
        with mock.patch.multiple(watcher, **patches), \
                mock.patch.object(watcher, "_send_calendar_reminders", job("calendar")):
            night = datetime(2026, 11, 5, 3, 0, tzinfo=watcher.TZ_BERLIN)
            self.assertEqual(cycle(night), set())  # personal checks sleep at night
            self.assertEqual(cycle(first - timedelta(minutes=1)), set())
            self.assertEqual(cycle(first + timedelta(minutes=1)), everything)
            self.assertEqual(cycle(first + timedelta(minutes=3)), set())  # that slot is done
            self.assertEqual(cycle(second + timedelta(minutes=1)), everything)
            self.assertEqual(cycle(second + timedelta(hours=2)), set())
            # an open registration switches to the four grade-season windows
            exam_cache["notified_open"] = {"MOD1:sitting": "2026-11-05"}
            late = stagger.personal_slots(5, day, season=True)[-1]
            self.assertEqual(cycle(late + timedelta(minutes=1)), everything)
        self.assertIn("tasks", calls)  # local checks still run every cycle
        self.assertEqual(set(sent_to), {5})  # personal alerts reach only that student

    def test_staggered_slots_differ_between_students(self):
        from unibot import stagger
        day = datetime(2026, 11, 5).date()
        quiet = [stagger.personal_slots(uid, day, season=False) for uid in range(1, 51)]
        self.assertTrue(all(len(s) == 2 for s in quiet))
        self.assertGreater(len({s[0].strftime("%H:%M") for s in quiet}), 35)
        for slots in quiet:
            self.assertTrue(9 <= slots[0].hour < 12 and 15 <= slots[1].hour <= 18)
        season = stagger.personal_slots(7, datetime(2026, 2, 10).date(), season=stagger.is_grade_season(
            datetime(2026, 2, 10).date()))
        self.assertEqual(len(season), 4)
        self.assertNotEqual(stagger.describe_slots(7, day, False),
                            stagger.describe_slots(7, day + timedelta(days=1), False))

    def test_once_per_period(self):
        watcher = _import("watcher")
        cache = {}
        runs = []

        async def work():
            runs.append(1)
            raise RuntimeError("still recorded")

        with mock.patch.multiple(watcher, load_general_cache=lambda: dict(cache),
                                 save_general_cache=lambda d: cache.update(d)):
            for _ in range(3):
                asyncio.run(watcher._run_once_per_period("k", "2026-11-04", "Job", "start", work))
            asyncio.run(watcher._run_once_per_period("k", "2026-11-05", "Job", "start", work))
        self.assertEqual(len(runs), 2)
        self.assertEqual(cache["k"], "2026-11-05")

    def test_is_due(self):
        watcher = _import("watcher")
        now = datetime(2026, 11, 4, 12, 0)
        for stored, expected in ((None, True), ("garbage", True), ((now - timedelta(minutes=10)).isoformat(), False),
                                 ((now - timedelta(hours=1)).isoformat(), True)):
            with mock.patch.object(watcher, "load_general_cache", return_value={"k": stored} if stored else {}):
                self.assertEqual(watcher._is_due("k", now, 1800), expected, stored)


# ── dispatch tables ──────────────────────────────────────────────────────────
class DispatchTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        from telegram.ext import ApplicationBuilder, CallbackQueryHandler
        from unibot import app as app_module
        os.chdir(cwd)
        application = ApplicationBuilder().token("123456:TEST-TOKEN").build()
        app_module.register_handlers(application)
        cls.patterns = {}
        for handlers in application.handlers.values():
            for h in handlers:
                if isinstance(h, CallbackQueryHandler):
                    cls.patterns.setdefault(h.callback.__name__, []).append(h.pattern)

    def routes(self, callback_data):
        """Names of the callbacks whose pattern matches this callback data."""
        return {name for name, pats in self.patterns.items() for p in pats if p is None or p.match(callback_data)}

    def test_every_voice_intent_has_a_handler(self):
        voice = _import("voice")
        self.assertEqual(set(voice.VOICE_INTENTS), set(voice._VOICE_INTENT_HANDLERS))

    def test_handlers_are_coroutines(self):
        import inspect
        menus, voice = _import("menus"), _import("voice")
        for table in (menus._SELECTION_HANDLERS, menus._STATUS_BUTTON_HANDLERS, voice._VOICE_INTENT_HANDLERS):
            for key, fn in table.items():
                self.assertTrue(inspect.iscoroutinefunction(fn), key)

    def test_status_buttons_reach_their_handler(self):
        menus = _import("menus")
        for key in menus._STATUS_BUTTON_HANDLERS:
            self.assertIn("handle_status_buttons", self.routes(key), key)

    def test_selection_buttons_reach_their_handler(self):
        menus = _import("menus")
        for key in menus._SELECTION_HANDLERS:
            self.assertIn("handle_selection", self.routes(f"{key}|x"), key)

    def test_background_jobs_stay_single_flight(self):
        exams, mon, schedule, study_plan = (_import(m) for m in ("exams", "monitoring", "schedule", "study_plan"))
        for fn in (exams.scan_course_files_for_exams, mon.check_new_files_parallel, schedule.check_calendar_reminders,
                   study_plan.index_course_files, study_plan.collect_course_topics, study_plan.collect_course_workload):
            self.assertTrue(hasattr(fn, "__wrapped__"), fn.__name__)


# ── StuMS login handoff ──────────────────────────────────────────────────────
class StumsHandoffTests(unittest.TestCase):
    """The handoff used to count any page on the StuMS host as a login, so an
    expired StuMS session failed on every watcher cycle without ever signing in
    again."""

    def setUp(self):
        import exam_reminder
        self.er = exam_reminder

    def session(self, planner_pages, has_stums_cookie=False):
        s = mock.MagicMock()
        s.explicitly_logged_out = False
        s.stums = __import__("studip_session").StumsState()
        s.sso_handoff = mock.AsyncMock(return_value="https://stums.uni-oldenburg.de/start")
        s.session.cookie_jar.filter_cookies.return_value = {"JSESSIONID": "x"} if has_stums_cookie else {}
        pages = iter(planner_pages)

        async def get(url, **kwargs):
            return FakeResponse(body=next(pages))

        s.get = get
        return s

    def test_warm_stums_cookie_needs_no_handoff(self):
        # the keep-alive keeps StuMS' own session warm: one request, no SSO round trip
        s = self.session(["<h1>Studienplaner</h1>"], has_stums_cookie=True)
        self.assertEqual(asyncio.run(self.er._establish_stums_session(s)), self.er.STUMS_START_URL)
        asyncio.run(self.er._establish_stums_session(s))  # remembered: no second request
        self.assertEqual(s.sso_handoff.await_count, 0)

    def test_valid_login_is_used_and_remembered(self):
        s = self.session(["<h1>Studienplaner</h1>"])
        self.assertEqual(asyncio.run(self.er._establish_stums_session(s)), "https://stums.uni-oldenburg.de/start")
        asyncio.run(self.er._establish_stums_session(s))  # within the trust window: no second handoff or check
        self.assertEqual(s.sso_handoff.await_count, 1)
        s.session.cookie_jar.clear_domain.assert_not_called()

    def test_expired_session_signs_in_again(self):
        s = self.session(["Login notwendig", "Login notwendig", "<h1>Studienplaner</h1>"], has_stums_cookie=True)
        asyncio.run(self.er._establish_stums_session(s))
        self.assertEqual(s.sso_handoff.await_count, 2)
        s.session.cookie_jar.clear_domain.assert_called_once_with("stums.uni-oldenburg.de")

    def test_gives_up_with_clear_message(self):
        s = self.session(["Login notwendig", "Login notwendig"])
        with self.assertRaisesRegex(RuntimeError, "still shows you as logged out"):
            asyncio.run(self.er._establish_stums_session(s))
        self.assertEqual(s.stums.verified_at, 0.0)

    def test_expired_sso_asks_for_a_sign_in(self):
        s = self.session([])
        s.sso_handoff = mock.AsyncMock(side_effect=RuntimeError("Interactive login required (no stored username)"))
        with self.assertRaisesRegex(self.er.StumsLoginUnavailable, "/login"):
            asyncio.run(self.er._establish_stums_session(s))
        self.assertGreater(s.stums.blocked_until, __import__("time").time())

    def test_stuck_at_university_login_asks_for_sign_in_and_pauses(self):
        s = self.session([])
        s.sso_handoff = mock.AsyncMock(side_effect=RuntimeError(
            "SSO flow did not complete; stuck at https://auth.uni-oldenburg.de/osp/a/TOP/auth/oauth2/grant?x=1"))
        with self.assertRaisesRegex(self.er.StumsLoginUnavailable, "university login"):
            asyncio.run(self.er._establish_stums_session(s))
        with self.er.background_stums(), self.assertRaises(self.er.StumsLoginUnavailable):
            asyncio.run(self.er._establish_stums_session(s))
        self.assertEqual(s.sso_handoff.await_count, 1)  # the watcher does not retry during the pause

    def test_any_failed_handoff_pauses_background_retries(self):
        s = self.session([])
        s.sso_handoff = mock.AsyncMock(side_effect=RuntimeError("SSO flow did not complete; stuck at https://x.example/"))
        with self.assertRaises(RuntimeError):
            asyncio.run(self.er._establish_stums_session(s))
        self.assertGreater(s.stums.blocked_until, __import__("time").time())

    def test_pauses_after_refusal_and_logs_once(self):
        s = self.session(["Login notwendig", "Login notwendig"])
        with self.assertRaises(self.er.StumsLoginUnavailable) as first:
            asyncio.run(self.er._establish_stums_session(s))
        self.assertFalse(first.exception.quiet)
        self.assertGreater(s.stums.blocked_until, __import__("time").time() + 25 * 60)
        with self.er.background_stums(), self.assertRaises(self.er.StumsLoginUnavailable) as second:
            asyncio.run(self.er._establish_stums_session(s))
        self.assertTrue(second.exception.quiet)
        self.assertEqual(s.sso_handoff.await_count, 2)  # background: no new attempt during the pause
        with mock.patch.object(self.er.logging, "error") as err, mock.patch.object(self.er.logging, "debug") as dbg:
            self.er._log_stums_failure("fetch failed", first.exception)
            self.er._log_stums_failure("fetch failed", second.exception)
        self.assertEqual((err.call_count, dbg.call_count), (1, 1))

    def test_user_actions_ignore_the_pause(self):
        s = self.session(["Login notwendig", "Login notwendig", "<h1>ok</h1>"])
        with self.assertRaises(self.er.StumsLoginUnavailable):
            asyncio.run(self.er._establish_stums_session(s))
        # a button press right after the refusal tries StuMS again at once
        asyncio.run(self.er._establish_stums_session(s))
        self.assertEqual(s.sso_handoff.await_count, 3)
        self.assertEqual(s.stums.blocked_until, 0.0)

    def test_background_checks_are_marked(self):
        seen = {}

        async def probe(*args, **kwargs):
            seen["background"] = self.er._stums_background.get()

        for name in ("check_exam_reminders", "check_grade_reminders", "check_tuition_reminders", "check_exam_date_reminders"):
            fn = getattr(self.er, name)
            self.assertTrue(hasattr(fn, "__wrapped__"), name)
            asyncio.run(self.er._background_job(probe)())
            self.assertTrue(seen.pop("background"))
        self.assertFalse(self.er._stums_background.get())

    def test_new_login_ends_the_pause(self):
        import studip_session
        sess = studip_session.StudIPSession("u", "p", "JBSWY3DPEHPK3PXP", cookie_file="/nonexistent")
        sess.stums.blocked_until = __import__("time").time() + 600
        sess.reset_login_backoff()
        self.assertEqual(sess.stums.blocked_until, 0.0)

    def test_forget_forces_a_new_check(self):
        s = self.session(["ok", "ok"])
        asyncio.run(self.er._establish_stums_session(s))
        self.er.forget_stums_login(s)
        asyncio.run(self.er._establish_stums_session(s))
        self.assertEqual(s.sso_handoff.await_count, 2)

    def test_handoff_errors_are_wrapped(self):
        s = self.session([])
        s.sso_handoff.side_effect = RuntimeError("SSO flow did not complete; stuck at https://login.example/x")
        with self.assertRaisesRegex(RuntimeError, "StuMS SSO handoff failed"):
            asyncio.run(self.er._establish_stums_session(s))


class LoginProxyAccessLogTests(unittest.TestCase):
    def test_query_strings_are_not_logged(self):
        import logging as logging_mod
        import studip_login_server
        logger = logging_mod.getLogger("test.access")
        access = studip_login_server._RedactedAccessLogger(logger, "")
        request = mock.MagicMock(remote="127.0.0.1", method="GET", path="/sso.php/oidc",
                                 path_qs="/sso.php/oidc?code=SECRET&state=abc")
        response = mock.MagicMock(status=302, body_length=0)
        with self.assertLogs("test.access", level="INFO") as logs:
            access.log(request, response, 0.01)
        line = logs.output[0]
        self.assertIn("/sso.php/oidc", line)
        self.assertNotIn("SECRET", line)
        self.assertNotIn("state", line)


if __name__ == "__main__":
    unittest.main()
