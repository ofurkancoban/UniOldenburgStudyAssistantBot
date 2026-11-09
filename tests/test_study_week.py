"""Calendar feed, study buddy and the Sunday report (unibot.study_week)."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import schedule, state, storage, study_week  # noqa: E402

MON = date(2026, 11, 2)
WED = date(2026, 11, 4)
SUN = date(2026, 11, 8)


def _at(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ_BERLIN)


def _event(start, end, title="Lecture", location="A05 1-161", cid="c1", day=WED):
    return {"date_key": day, "start_dt": _at(day, start), "end_dt": _at(day, end), "title": title,
            "location": location, "course_id": cid, "is_cancelled": False}


def _query(data, user_id):
    query = mock.MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.message.reply_text = mock.AsyncMock()
    query.message.reply_document = mock.AsyncMock()
    return query


class IcsTests(unittest.TestCase):
    def _entries(self):
        events = [_event("09:00", "11:30", "Statistics"), _event("14:00", "16:00", "Econometrics")]
        plan = schedule.plan_study_day(events, WED)
        return plan["windows"] + [plan["lunch"]]

    def test_study_blocks_and_lunch_are_busy_events(self):
        ics = study_week.build_ics(self._entries(), 42, now=_at(WED, "07:00"))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 4)
        self.assertEqual(ics.count("TRANSP:OPAQUE"), 4)
        self.assertIn("DTSTART:20261104T110000Z", ics)  # 12:00 Berlin (CET) is 11:00 UTC
        self.assertIn("SUMMARY:🥗 Lunch at Mensa Uhlhornsweg", ics)
        self.assertIn("SUMMARY:📚 Study block: Deep work", ics)
        self.assertIn("LOCATION:BIS library\\, 2nd floor quiet room", ics)

    def test_lines_are_folded_and_text_escaped(self):
        entry = {**self._entries()[0], "description": "a; b, c\nd " + "x" * 200}
        ics = study_week.build_ics([entry], 42)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        unfolded = ics.replace("\r\n ", "")
        self.assertIn("DESCRIPTION:a\\; b\\, c\\nd ", unfolded)

    def test_uids_are_stable_and_do_not_reveal_the_user(self):
        a = study_week.build_ics(self._entries(), 42, now=_at(WED, "07:00"))
        b = study_week.build_ics(self._entries(), 42, now=_at(WED, "08:00"))
        uids = [line for line in a.split("\r\n") if line.startswith("UID:")]
        self.assertEqual(uids, [line for line in b.split("\r\n") if line.startswith("UID:")])
        self.assertNotEqual(uids, [line for line in study_week.build_ics(self._entries(), 43).split("\r\n")
                                   if line.startswith("UID:")])


class CalendarFeedTests(unittest.TestCase):
    UID = 8181

    def _get(self, token):
        from aiohttp.test_utils import make_mocked_request
        request = make_mocked_request("GET", f"/cal/{token}", match_info={"token": token})
        return asyncio.run(study_week.feed_handler(request))

    def test_feed_needs_the_secret_token(self):
        token = storage.calendar_token(self.UID)
        with mock.patch.object(study_week, "is_user_allowed", lambda uid: True), \
                mock.patch.object(study_week, "calendar_for", mock.AsyncMock(return_value="BEGIN:VCALENDAR\r\n")):
            ok = self._get(f"{token}.ics")
            wrong = self._get("not-the-token.ics")
        self.assertEqual((ok.status, ok.content_type), (200, "text/calendar"))
        self.assertEqual(wrong.status, 404)
        with mock.patch.object(study_week, "is_user_allowed", lambda uid: False):
            self.assertEqual(self._get(f"{token}.ics").status, 404)  # access revoked

    def test_a_new_link_retires_the_old_one(self):
        old = storage.calendar_token(self.UID)
        new = storage.reset_calendar_token(self.UID)
        self.assertNotEqual(old, new)
        self.assertIsNone(storage.user_for_calendar_token(old))
        self.assertEqual(storage.user_for_calendar_token(new), self.UID)

    def test_feed_shows_this_and_next_week_from_today(self):
        weeks = []

        async def entries(uid, week_start, session=None):
            weeks.append(week_start)
            return [{"kind": "lunch", "mensa": "Mensa Wechloy", "start_dt": _at(week_start, "12:00"),
                     "end_dt": _at(week_start, "12:45")}]
        with mock.patch.object(study_week, "plan_entries", entries):
            ics = asyncio.run(study_week.calendar_for(self.UID, today=WED))
        self.assertEqual(weeks, [MON, MON + timedelta(days=7)])
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)  # this Monday's lunch is already over

    def test_route_is_served_before_the_proxy_and_the_token_is_not_logged(self):
        import studip_login_server
        server = studip_login_server.LoginServer(mock.AsyncMock())
        study_week.register_calendar_feed(server)
        self.assertEqual(server.extra_routes, [("GET", "/cal/{token}", study_week.feed_handler)])
        logger = mock.MagicMock()
        access = studip_login_server._RedactedAccessLogger(logger, "")
        request = mock.MagicMock(remote="1.2.3.4", method="GET", path="/cal/s3cr3t.ics")
        access.log(request, mock.MagicMock(status=200, body_length=10), 0.1)
        self.assertNotIn("s3cr3t", str(logger.info.call_args))
        self.assertIn("/cal/***", str(logger.info.call_args))

    def test_sync_view(self):
        server = mock.MagicMock()
        server.base_url.return_value = "https://bot.example.org"
        with mock.patch.object(state, "login_server", server), \
                mock.patch.object(study_week, "CLOUDFLARE_TUNNEL", "quick"):
            text, markup = study_week.calendar_sync_view(self.UID)
        token = storage.calendar_token(self.UID)
        self.assertIn(f"webcal://bot.example.org/cal/{token}.ics", text)
        self.assertIn("changes when the bot restarts", text)
        google = markup.inline_keyboard[1][0].url
        self.assertTrue(google.startswith("https://calendar.google.com/calendar/render?cid=webcal%3A%2F%2F"))
        server.base_url.return_value = "http://192.168.1.5:8899"  # not reachable from a phone's calendar app
        with mock.patch.object(state, "login_server", server):
            text, markup = study_week.calendar_sync_view(self.UID)
        self.assertNotIn("/cal/", text)
        self.assertEqual([r[0].callback_data for r in markup.inline_keyboard], ["cal_sync|file"])

    def test_file_button_sends_the_ics(self):
        query = _query("cal_sync|file", self.UID)
        seen = []
        query.message.reply_document = mock.AsyncMock(side_effect=lambda **kw: seen.append(kw["document"].getvalue()))
        with mock.patch.object(study_week, "is_user_allowed", lambda uid: True), \
                mock.patch.object(study_week, "calendar_for", mock.AsyncMock(return_value="BEGIN:VCALENDAR\r\n")):
            asyncio.run(study_week.handle_calendar_sync(mock.MagicMock(callback_query=query), None))
        kwargs = query.message.reply_document.await_args.kwargs
        self.assertEqual(kwargs["filename"], "study-blocks.ics")
        self.assertEqual(seen, [b"BEGIN:VCALENDAR\r\n"])


def _window(day, start, end, campus="haarentor", spot="BIS library, 2nd floor quiet room"):
    return {"start_dt": _at(day, start), "end_dt": _at(day, end), "campus": campus, "study_spot": spot,
            "duration_minutes": int((_at(day, end) - _at(day, start)).total_seconds() // 60)}


class StudyBuddyTests(unittest.TestCase):
    A, B, C = 9101, 9102, 9103

    def setUp(self):
        for uid in (self.A, self.B, self.C):
            storage.set_study_buddy_enabled(uid, True)
        courses = {self.A: {"c1": "Econometrics", "c2": "Statistics"}, self.B: {"c1": "Econometrics"},
                   self.C: {"c9": "Biology"}}
        patcher = mock.patch.object(state, "user_courses_map", courses)
        patcher.start()
        self.addCleanup(patcher.stop)
        allowed = mock.patch.object(study_week, "is_user_allowed", lambda uid: True)
        allowed.start()
        self.addCleanup(allowed.stop)

    def test_overlaps_of_an_hour_or_more(self):
        mine = [_window(WED, "14:00", "16:30"), _window(WED, "09:00", "10:00"),
                {"kind": "lunch", "start_dt": _at(WED, "12:00"), "end_dt": _at(WED, "12:45")}]
        theirs = [_window(WED, "13:30", "16:00"), _window(WED, "09:30", "11:00", campus="wechloy", spot="W3")]
        windows = study_week.common_windows(mine, theirs)
        self.assertEqual([(w["start_dt"].strftime("%H:%M"), w["end_dt"].strftime("%H:%M"), w["minutes"]) for w in windows],
                         [("14:00", "16:00", 120)])  # 09:30-10:00 is too short, the lunch never counts
        self.assertEqual(windows[0]["spot"], "BIS library, 2nd floor quiet room")
        other_campus = study_week.common_windows([_window(WED, "14:00", "16:00")],
                                                 [_window(WED, "14:00", "16:00", campus="wechloy")])
        self.assertIsNone(other_campus[0]["spot"])

    def test_only_opted_in_classmates_with_a_shared_course(self):
        self.assertEqual(study_week.buddy_candidates(self.A), [(self.B, ["Econometrics"])])  # C shares nothing
        storage.set_study_buddy_enabled(self.B, False)
        self.assertEqual(study_week.buddy_candidates(self.A), [])  # both have to opt in
        storage.set_study_buddy_enabled(self.B, True)
        storage.set_study_buddy_enabled(self.A, False)
        self.assertEqual(study_week.buddy_candidates(self.A), [])

    def test_matches_and_message(self):
        plans = {self.A: [_window(WED, "14:00", "16:30")], self.B: [_window(WED, "13:30", "16:00")]}

        async def entries(uid, week_start, session=None):
            return plans.get(uid, [])
        with mock.patch.object(study_week, "plan_entries", entries), \
                mock.patch.object(study_week, "get_user_name", lambda uid: "Ahmet Yilmaz"):
            matches = asyncio.run(study_week.study_buddy_matches(self.A, MON))
            later = asyncio.run(study_week.study_buddy_matches(self.A, MON, after=_at(WED, "17:00")))
        self.assertEqual(later, [])
        lines = study_week.format_buddy_lines(matches)
        self.assertEqual(lines[0], "👥 <b>Common free time with Ahmet</b> (Econometrics):")
        self.assertEqual(lines[1], "   • Wed 14:00–16:00 (2h) · 📍 BIS library, 2nd floor quiet room")

    def test_view_explains_the_privacy_and_toggles(self):
        storage.set_study_buddy_enabled(self.A, False)
        query = _query("buddy|toggle", self.A)
        with mock.patch.object(study_week, "plan_entries", mock.AsyncMock(return_value=[])):
            asyncio.run(study_week.handle_study_buddy(mock.MagicMock(callback_query=query), None))
        self.assertTrue(storage.get_study_buddy_enabled(self.A))
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("never your timetable", text)
        self.assertIn("Status: ✅ On", text)


class StreakTests(unittest.TestCase):
    def _o(self, due, done=None, text="PS"):
        return {"due": due.isoformat(), "done": int(done.timestamp()) if done else None, "text": text}

    def test_weeks_in_a_row_with_every_deadline_on_time(self):
        now = _at(SUN, "18:00")
        outcomes = [
            self._o(_at(MON - timedelta(days=21), "23:59"), _at(MON - timedelta(days=22), "20:00")),  # 4 weeks ago, on time
            self._o(_at(MON - timedelta(days=12), "12:00")),                                          # 2 weeks ago, missed
            self._o(_at(MON - timedelta(days=5), "23:59"), _at(MON - timedelta(days=5), "23:30")),    # last week, on time
            self._o(_at(WED, "12:00"), _at(WED, "12:40")),                                            # done on the reminder
            self._o(_at(MON + timedelta(days=8), "12:00")),                                           # next week, still open
        ]
        self.assertEqual(study_week.deadline_streak(outcomes, now), (2, []))

    def test_a_missed_or_late_deadline_this_week_breaks_it(self):
        now = _at(SUN, "18:00")
        late = [self._o(_at(WED, "12:00"), _at(WED, "15:00"), "Lab report")]
        self.assertEqual(study_week.deadline_streak(late, now), (0, ["Lab report"]))
        missed = [self._o(_at(WED, "12:00"), None, "Quiz")]
        self.assertEqual(study_week.deadline_streak(missed, now), (0, ["Quiz"]))
        not_yet = [self._o(_at(SUN, "17:30"), None, "Quiz")]  # inside the grace hour
        self.assertEqual(study_week.deadline_streak(not_yet, now), (0, []))

    def test_tasks_log_their_outcome(self):
        from unibot import tasks
        uid = 9301
        task = {"id": "t-1", "user_id": uid, "text": "Problem Set 3", "due_time": _at(WED, "23:59").isoformat()}
        query = _query("task_done|t-1", uid)
        saved = []
        with mock.patch.object(tasks, "user_tasks", lambda u: [task] if u == uid else []), \
                mock.patch.object(tasks, "save_user_tasks", lambda u, t: saved.append(t)), \
                mock.patch.object(tasks, "is_user_allowed", lambda uid: True):
            stranger = _query("task_done|t-1", 9999)
            asyncio.run(tasks.handle_task_buttons(mock.MagicMock(callback_query=stranger), None))
            self.assertEqual((saved, storage.task_outcomes(uid)), ([], []))  # not their task
            asyncio.run(tasks.handle_task_buttons(mock.MagicMock(callback_query=query), None))
        self.assertEqual(saved, [[]])
        (outcome,) = storage.task_outcomes(uid)
        self.assertEqual((outcome["due"], outcome["text"]), (task["due_time"], "Problem Set 3"))
        self.assertIsNotNone(outcome["done"])
        storage.record_task_outcome(uid, {**task, "due_time": _at(SUN, "10:00").isoformat()}, 1)  # a snooze keeps the first due
        self.assertEqual(storage.task_outcomes(uid)[0]["due"], task["due_time"])


class WeeklyReportTests(unittest.TestCase):
    UID = 9401

    def test_report_lines(self):
        windows = [_window(WED, "10:00", "14:00"), _window(WED, "15:00", "19:00"),
                   {"kind": "lunch", "start_dt": _at(WED, "12:00"), "end_dt": _at(WED, "12:45"), "duration_minutes": 45}]
        for start, minutes, status in (("10:00", 240, "done"), ("15:00", 240, "done"), ("17:00", 120, "skipped")):
            ts = int(_at(WED, start).timestamp())
            storage.record_study_block(self.UID, ts, "sent", "Econometrics", minutes)
            storage.record_study_block(self.UID, ts, status)
        outcomes = [{"due": _at(WED - timedelta(days=7), "12:00").isoformat(), "done": 1, "text": "PS"},
                    {"due": _at(WED - timedelta(days=14), "12:00").isoformat(), "done": 1, "text": "PS"},
                    {"due": _at(WED - timedelta(days=21), "12:00").isoformat(), "done": 1, "text": "PS"}]

        async def entries(uid, week_start, session=None):
            return windows if week_start == MON else []
        with mock.patch.object(study_week, "plan_entries", entries), \
                mock.patch.object(study_week, "task_outcomes", lambda uid: outcomes), \
                mock.patch.object(study_week, "study_buddy_matches", mock.AsyncMock(return_value=[])):
            text = asyncio.run(study_week.weekly_report(self.UID, _at(SUN, "18:00")))
        self.assertIn("📊 <b>Weekly report</b> (02.11–08.11)", text)
        self.assertIn("Free time between classes this week: <b>8h</b>", text)
        self.assertIn("<b>8h</b> of it went into planned study blocks (efficiency: 100%) 🎯", text)
        self.assertIn("Study blocks: 2 of 3 done", text)
        self.assertIn("🔥 3 weeks in a row with every deadline done on time! (3-week streak)", text)

    def test_watcher_sends_on_sunday_evening_once(self):
        from unibot import watcher
        sent = []
        bot = mock.MagicMock()

        async def send_message(chat_id, text, parse_mode=None):
            sent.append(chat_id)
        bot.send_message = send_message
        accounts = [mock.MagicMock(user_id=1), mock.MagicMock(user_id=2)]
        cache: dict = {}
        with mock.patch.object(watcher, "signed_in_accounts", lambda: accounts), \
                mock.patch.object(watcher, "weekly_report", mock.AsyncMock(return_value="report")), \
                mock.patch.object(watcher, "load_general_cache", lambda: cache), \
                mock.patch.object(watcher, "save_general_cache", lambda c: None):
            asyncio.run(watcher._run_weekly_report(bot, _at(SUN, "17:59")))
            asyncio.run(watcher._run_weekly_report(bot, _at(SUN - timedelta(days=1), "19:00")))
            asyncio.run(watcher._run_weekly_report(bot, _at(SUN, "18:05")))
            asyncio.run(watcher._run_weekly_report(bot, _at(SUN, "21:00")))
        self.assertEqual(sent, [1, 2])
        self.assertEqual(cache["last_weekly_report"], "2026-W45")


if __name__ == "__main__":
    unittest.main()
