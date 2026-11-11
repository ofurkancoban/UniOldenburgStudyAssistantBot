"""Fixes from the thirteenth review (schedule): a cancelled class gets no
"starting in 30 minutes" reminder, the weather heads-up goes with the first
class that is actually held, two students may fast-enroll in the same
course, one unreadable task time doesn't break the Upcoming view, a course
whose file listing failed keeps its search index, and the progress buttons
say when the student's session expired."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
import studip_vault  # noqa: E402
from unibot import db  # noqa: E402
from unibot import mensa, menus, onboarding, schedule, settings_menu, study_plan  # noqa: E402
from unibot import preferences as prefs  # noqa: E402

UID = 9801
DAY = date(2026, 11, 4)


def _at(hm):
    h, m = map(int, hm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ_BERLIN)


def _class(title, start, cancelled=False):
    s = _at(start)
    return {"title": f"Vorlesung: {title}", "time": f"{start} - {s + timedelta(hours=2):%H:%M}", "location": "A1",
            "date_key": DAY, "start_dt": s, "end_dt": s + timedelta(hours=2), "is_cancelled": cancelled}


class _Frozen(datetime):
    frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


class CancelledClassReminderTests(unittest.TestCase):
    def _run(self, events, at):
        sent = []

        async def send_message(**kwargs):
            sent.append(kwargs["text"])

        bot = mock.MagicMock()
        bot.send_message = send_message
        _Frozen.frozen = _at(at)
        wet = {"is_wet": True, "emoji": "🌧️", "description": "rain", "temp_min": 5, "temp_max": 9,
               "precip_chance": 80, "location_name": "Oldenburg"}
        with mock.patch.object(schedule, "datetime", _Frozen), \
                mock.patch.object(schedule, "get_calendar_events", mock.AsyncMock(return_value=events)), \
                mock.patch.object(schedule, "get_user_session", lambda uid=None: None), \
                mock.patch.object(schedule, "get_weather_snapshot", mock.AsyncMock(return_value=wet)), \
                mock.patch.object(schedule, "load_reminders_cache", lambda user_id=None: {}), \
                mock.patch.object(schedule, "save_reminders_cache", lambda data, user_id=None: None):
            asyncio.run(schedule.check_calendar_reminders(bot, UID, silent=True))
        return [t for t in sent if "LECTURE REMINDER" in t]

    def test_no_reminder_for_a_cancelled_class(self):
        self.assertEqual(self._run([_class("Statistics", "10:00", cancelled=True)], "09:15"), [])

    def test_weather_goes_with_the_first_held_class(self):
        events = [_class("Statistics", "08:00", cancelled=True), _class("Econometrics", "10:00")]
        # wet weather widens the window: 40-50 minutes before the first held class
        (reminder,) = self._run(events, "09:15")
        self.assertIn("Econometrics", reminder)
        self.assertIn("umbrella", reminder)



class FastEnrollPerStudentTests(unittest.TestCase):
    def test_two_students_may_schedule_the_same_course(self):
        sem = "a" * 32
        saved = []

        async def run(*args):
            return None

        with mock.patch("fast_enroll.load_pending", return_value={"1": {sem: {}}}), \
                mock.patch.object(schedule, "save_pending_job", lambda *a: saved.append(a[0])), \
                mock.patch("unibot.tasks._run_fastenroll_task", run), \
                mock.patch.dict(schedule.fastenroll_tasks, {}, clear=True):
            reply = asyncio.run(schedule.schedule_fastenroll_job(2, sem, "08:00:00", None, None))
        self.assertIn("scheduled", reply)
        self.assertEqual(saved, [2])


class UpcomingDashboardTests(unittest.TestCase):
    def test_unreadable_and_naive_task_times(self):
        tasks = [{"id": "a", "text": "Broken", "due_time": "nope"},
                 {"id": "b", "text": "Hand in", "due_time": "2030-01-05T15:00:00"},
                 {"id": "c", "text": "Buy pens"}]
        with mock.patch.object(schedule, "user_tasks", lambda uid: tasks), \
                mock.patch.object(schedule, "get_open_exam_registrations", mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, "get_registered_exam_schedule", mock.AsyncMock(return_value=[
                    {"title": "Statistics", "date": "2030-01-10", "start_datetime": None}])):
            text = asyncio.run(schedule.build_upcoming_dashboard(object(), UID))
        self.assertIn("Hand in", text)
        self.assertIn("Statistics", text)
        self.assertIn("• Broken", text)  # kept as an undated to-do
        self.assertIn("• Buy pens", text)
        self.assertLess(text.index("Hand in"), text.index("Statistics"))



class CourseIndexKeptOnFailureTests(unittest.TestCase):
    def test_failed_listing_keeps_the_courses_index(self):
        index = {"files": {"c1|old.pdf": {"course": "A", "file": "old.pdf", "size": "1", "modified_ts": 1, "chunks": []},
                           "c2|gone.pdf": {"course": "B", "file": "gone.pdf", "size": "1", "modified_ts": 1, "chunks": []}}}
        saved = {}

        async def listing(cid, session=None):
            if cid == "c1":
                raise RuntimeError("Stud.IP timeout")
            return []

        with mock.patch.object(study_plan, "GWDG_API_KEY", "k"), \
                mock.patch.object(study_plan, "watched_courses", mock.AsyncMock(return_value=[("A", "c1", object()), ("B", "c2", object())])), \
                mock.patch.object(study_plan, "_list_course_files", listing), \
                mock.patch.object(study_plan, "_load_course_index_async", mock.AsyncMock(return_value=index)), \
                mock.patch.object(study_plan, "save_course_index", lambda data: saved.update(data)):
            stats = asyncio.run(study_plan.index_course_files(None))
        self.assertIn("c1|old.pdf", saved["files"])  # not read this run: kept, not re-embedded tomorrow
        self.assertNotIn("c2|gone.pdf", saved["files"])  # read and really gone
        self.assertEqual(stats["removed"], 1)


class ExpiredSessionButtonTests(unittest.TestCase):
    def test_progress_and_track_buttons_say_so(self):
        for handler, loader in ((study_plan.handle_study_progress_menu, "_load_progress_rows"),
                                (study_plan.handle_study_track_menu, "_load_track_menu_progress")):
            query = mock.AsyncMock()
            query.data = "sp_progress"
            update = types.SimpleNamespace(callback_query=query, effective_user=types.SimpleNamespace(id=UID))
            with mock.patch.object(study_plan, loader, mock.AsyncMock(side_effect=RuntimeError("Sign in again"))):
                asyncio.run(handler(update, None))
            self.assertIn("Sign in again", query.message.reply_text.call_args.args[0])



class OutfitIsForAdminsTests(unittest.TestCase):
    def test_a_student_cannot_flip_the_outfit_switch(self):
        uid = 9802
        prefs.set_pref(uid, "outfit", True)
        with mock.patch.object(settings_menu, "is_user_admin", return_value=False):
            settings_menu.apply_action(uid, ["settings", "flip", "outfit"])
        with mock.patch.object(onboarding, "is_user_admin", return_value=False), self.assertRaises(ValueError):
            onboarding.apply_answer(uid, "outfit", "off", [])
        self.assertTrue(prefs.get_pref(uid, "outfit"))



class FilesBrowserTests(unittest.TestCase):
    def test_course_button_shows_an_ampersand_as_it_is(self):
        query = mock.AsyncMock()
        asyncio.run(menus._send_courses_menu(query, [("Research & Methods", "c1")]))
        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "📘 Research & Methods")

    def test_file_card_comes_back_after_a_failed_fresh_link(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        card_markup = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download", callback_data="file|s1")]])
        query = mock.AsyncMock()
        query.message.text_html = "<b>📁 File</b>"
        query.message.reply_markup = card_markup
        with mock.patch.dict(menus.link_cache, {"s1": {"cid": "c1", "name": "slides.pdf"}}), \
                mock.patch.object(menus, "get_user_session", lambda uid=None: object()), \
                mock.patch.object(menus, "get_fresh_file_url", mock.AsyncMock(return_value=None)), \
                mock.patch.object(menus, "_show_typing", mock.AsyncMock()), \
                mock.patch.object(menus, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(menus, "_stop_loading_animation", mock.AsyncMock()):
            asyncio.run(menus._selection_file(None, None, query, ["file", "s1"], UID))
        last = query.edit_message_text.call_args
        self.assertEqual(last.args[0], "<b>📁 File</b>")
        self.assertIs(last.kwargs["reply_markup"], card_markup)



class _Page:
    def __init__(self, html_text):
        self._html = html_text

    async def text(self):
        return self._html

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, html_text):
        self._html = html_text

    async def get(self, url):
        return _Page(self._html)


class MenuWithoutHeadingTests(unittest.TestCase):
    def test_a_table_without_a_heading_row_does_not_break_the_menu(self):
        page = ("<table class='default'><tr><td>Pizza Margherita<span class='attributes'>Gl</span></td>"
                "<td>3,50 &euro;</td></tr></table>"
                "<table class='default'><tr><th>Main Dishes</th></tr><tr><td>Lentil stew</td><td>2,90 &euro;</td></tr></table>")
        text, _p, _n, _d, items, _h = asyncio.run(mensa.get_todays_menu_enhanced(_Session(page)))
        self.assertEqual(sorted(i["name"] for i in items), ["Lentil stew", "Pizza Margherita"])
        self.assertIn("PIZZA", text)



class LegacyPrivateRecordTests(unittest.TestCase):
    def test_a_shared_key_record_is_rekeyed_on_read(self):
        uid = 9803
        with db._lock:
            db._connection().execute(
                "INSERT OR REPLACE INTO user_private_data (user_id, kind, encrypted_blob, updated_at) VALUES (?, ?, ?, ?)",
                (uid, "exam_cache", studip_vault.encrypt_payload({"known_grades": {"M1": "1.0"}}), 0))
        self.assertEqual(db.get_user_scope(uid).private("exam_cache"), {"known_grades": {"M1": "1.0"}})
        with db._lock:
            blob = db._connection().execute(
                "SELECT encrypted_blob FROM user_private_data WHERE user_id = ? AND kind = ?", (uid, "exam_cache")).fetchone()[0]
        self.assertTrue(blob.startswith(studip_vault.USER_MAGIC))  # now under the student's own key
        self.assertEqual(db.get_user_scope(uid).private("exam_cache"), {"known_grades": {"M1": "1.0"}})


class FileDateTests(unittest.TestCase):
    def test_a_written_out_date_is_not_minutes_ago(self):
        from unibot.studip_api import _parse_file_date_v2
        self.assertEqual(_parse_file_date_v2("12 May 2025", None), datetime(2025, 5, 12))
        self.assertEqual(_parse_file_date_v2("3 Nov 2025", None), datetime(2025, 11, 3))
        from studip_utils import berlin_now
        now = berlin_now()
        for text, minutes in (("vor 10 Minuten", 10), ("vor 2 Std.", 120), ("5 minutes ago", 5), ("1 hour ago", 60)):
            dt = _parse_file_date_v2(text, None)
            self.assertAlmostEqual((now - dt).total_seconds() / 60, minutes, delta=1, msg=text)

    def test_newest_pack_file_with_mixed_stamps(self):
        files = [("Uebungen", {"name": "Blatt 1.pdf", "url": "u1", "modified": "01.10.2025 10:00"}),
                 ("Uebungen", {"name": "Blatt 2.pdf", "url": "u2", "modified": "02.10.2025",
                               "modified_iso": "2025-10-02T09:00:00+02:00"})]
        (picked,) = study_plan.pick_study_pack_files(files, ("problem_set",))
        self.assertEqual(picked["name"], "Blatt 2.pdf")

    def test_file_alerts_with_mixed_stamps_are_sent(self):
        from unibot import monitoring_alerts
        from unibot.monitoring_diff import _file_events
        events = _file_events("Stats", "c1", [{"name": "a.pdf", "modified": "01.10.2025 10:00"},
                                              {"name": "b.pdf", "modified_iso": "2025-10-02T09:00:00+02:00"}], [])
        with mock.patch.object(monitoring_alerts, "course_members", lambda cid: set()):
            asyncio.run(monitoring_alerts._send_file_alerts(mock.AsyncMock(), events))


class ExamReminderEscapingTests(unittest.TestCase):
    def test_form_and_examiner_are_escaped(self):
        import exam_reminder
        sent = []

        async def broadcast(bot, text, **kwargs):
            sent.append(text)

        exam = {"title": "Statistics", "unit_id": "1", "exam_form": "Hausarbeit & <Praesentation>",
                "examiner": "Dr. A & B", "date_line": "Wednesday 11/4/26 No time defined",
                "date": "2026-11-04T00:00:00", "start_datetime": None}
        with mock.patch.object(exam_reminder, "get_registered_exam_schedule", mock.AsyncMock(return_value=[exam])), \
                mock.patch.object(exam_reminder, "berlin_now", lambda: datetime(2026, 11, 4, 8)), \
                mock.patch.object(exam_reminder, "load_exam_cache", lambda *a, **k: {}), \
                mock.patch.object(exam_reminder, "save_exam_cache", lambda *a, **k: None):
            asyncio.run(exam_reminder.check_exam_date_reminders(None, None, broadcast))
        (text,) = sent
        self.assertIn("Hausarbeit &amp; &lt;Praesentation&gt;", text)
        self.assertIn("Dr. A &amp; B", text)

    def test_registration_window_is_escaped(self):
        import exam_reminder
        sent = []

        async def broadcast(bot, text, **kwargs):
            sent.append(text)

        exam = {"code": "M1", "unit_id": "1", "title": "Statistics", "start_date": "2026-11-01T00:00:00",
                "end_date": None, "raw": "01.11. - 20.11. <late & early>"}
        with mock.patch.object(exam_reminder, "get_open_exam_registrations", mock.AsyncMock(return_value=[exam])), \
                mock.patch.object(exam_reminder, "load_exam_cache", lambda *a, **k: {}), \
                mock.patch.object(exam_reminder, "save_exam_cache", lambda *a, **k: None):
            asyncio.run(exam_reminder.check_exam_reminders(None, None, broadcast, {}))
        (text,) = sent
        self.assertIn("&lt;late &amp; early&gt;", text)


class MessageAlertTests(unittest.TestCase):
    def test_an_over_long_message_is_cut_and_the_next_one_still_goes_out(self):
        from unibot import monitoring
        messages = [{"id": "m1", "title": "Long", "sender": "Ada", "date": "01/11/26 10:00", "url": "u1", "body": "&" * 3300},
                    {"id": "m2", "title": "Short", "sender": "Ada", "date": "02/11/26 10:00", "url": "u2", "body": "hi"}]
        bot = mock.AsyncMock()
        with mock.patch.object(monitoring, "get_user_session", lambda uid=None: object()), \
                mock.patch.object(monitoring, "_message_owner", lambda uid=None: UID), \
                mock.patch.object(monitoring, "_fetch_messages_api", mock.AsyncMock(return_value=messages)), \
                mock.patch.object(monitoring, "stored_messages", lambda owner: []), \
                mock.patch.object(monitoring, "save_messages", lambda owner, msgs: None):
            self.assertTrue(asyncio.run(monitoring.check_new_messages(bot, UID, silent=True)))
        texts = [c.kwargs["text"] for c in bot.send_message.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertLessEqual(max(len(t) for t in texts), 4096)
        self.assertIn("Short", texts[1])


class StatusCommandTests(unittest.TestCase):
    def test_a_students_studip_name_is_escaped(self):
        from unibot import system

        class _Sess:
            async def is_logged_in(self):
                return True

        message = mock.AsyncMock()
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=UID), message=message)
        with mock.patch.object(system, "is_user_admin", return_value=False), \
                mock.patch.object(system, "get_user_session", lambda uid=None: _Sess()), \
                mock.patch.object(system, "get_studip_user_profile", mock.AsyncMock(return_value={"formatted_name": "Hans & <Gretel>"})), \
                mock.patch.object(system, "_login_public_url", lambda: "http://localhost"):
            asyncio.run(system.status_command(update, None))
        text = message.reply_text.call_args.args[0]
        self.assertIn("Hans &amp; &lt;Gretel&gt;", text)
        self.assertNotIn("<Gretel>", text)


class FastEnrollShutdownTests(unittest.TestCase):
    def test_a_cancelled_job_keeps_its_stored_entry_but_a_finished_one_clears_it(self):
        from unibot import tasks

        async def forever(*a, **k):
            await asyncio.sleep(3600)

        async def done(*a, **k):
            return True, "Enrolled"

        async def scenario(runner):
            cleared = []
            context = types.SimpleNamespace(bot=mock.AsyncMock())
            with mock.patch.object(tasks, "login_studip", mock.AsyncMock(return_value=object())), \
                    mock.patch.object(tasks, "run_fast_enroll", runner), \
                    mock.patch.object(tasks, "clear_pending_job", lambda *a: cleared.append(a)):
                task = asyncio.ensure_future(tasks._run_fastenroll_task(UID, "s1", "08:00:00", None, context))
                await asyncio.sleep(0.01)
                if runner is forever:
                    task.cancel()  # what a bot shutdown does to every task
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return cleared

        self.assertEqual(asyncio.run(scenario(forever)), [])  # restored at the next start
        self.assertEqual(asyncio.run(scenario(done)), [(UID, "s1")])

    def test_a_students_own_cancel_clears_the_stored_job(self):
        from unibot import enrollment
        cleared = []
        running = mock.MagicMock()
        running.done.return_value = False
        with mock.patch.dict(enrollment.fastenroll_tasks, {UID: {"s1": running}}, clear=True), \
                mock.patch.object(enrollment, "clear_pending_job", lambda *a: cleared.append(a)):
            asyncio.run(enrollment.cancel_fastenroll_job(UID, "s1"))
        running.cancel.assert_called_once()
        self.assertEqual(cleared, [(UID, "s1")])


class GwdgQuotaTests(unittest.TestCase):
    def test_a_duration_reset_header_still_pauses_gwdg(self):
        from unibot import ai, state
        self.assertEqual(ai._reset_seconds("3600"), 3600)
        self.assertEqual(ai._reset_seconds("1h30m"), 5400)
        self.assertEqual(ai._reset_seconds("45s"), 45)
        self.assertEqual(ai._reset_seconds("soon"), 3600)
        headers = {"x-ratelimit-remaining-requests-day": "0", "x-ratelimit-reset-requests-day": "2h"}
        posted = mock.AsyncMock(return_value=(429, None, headers))
        http = mock.MagicMock()
        http.return_value.__aenter__ = mock.AsyncMock(return_value=object())
        http.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch.object(ai, "GWDG_API_KEY", "k"), mock.patch.object(ai, "_post_gwdg_chat", posted), \
                mock.patch.object(ai, "shared_http", http), \
                mock.patch.object(state, "_gwdg_blocked_until", 0.0):
            import time as _time
            before = _time.monotonic()
            self.assertIsNone(asyncio.run(ai._call_gwdg_chat([{"role": "user", "content": "hi"}], "m")))
            self.assertGreaterEqual(state._gwdg_blocked_until, before + 7000)
        posted.assert_awaited_once()  # no second try with a used-up quota


class SignInResumesTests(unittest.TestCase):
    def test_a_stopped_student_who_signs_in_again_is_active(self):
        import studip_oauth
        import stums_client
        from unibot import auth, studip_api
        uid = 9804
        db.upsert_user(telegram_id=uid, role="user")
        db.set_lifecycle(uid, "inactive", "deleted their data")
        session = mock.MagicMock()
        session.session = None
        session.save_cookies = mock.AsyncMock()
        session.is_html_logged_in = mock.AsyncMock(return_value=True)
        with mock.patch.object(auth, "get_user_session", lambda user_id=None: session), \
                mock.patch.object(studip_oauth, "oauth_enabled", return_value=False), \
                mock.patch.object(studip_api, "get_studip_user_profile", mock.AsyncMock(return_value={})), \
                mock.patch.object(stums_client, "_establish_stums_session", mock.AsyncMock(return_value="r")), \
                mock.patch.object(auth.state, "_menu_app", None):
            asyncio.run(auth._on_proxy_session_authenticated(uid))
        self.assertEqual(auth.lifecycle_state(uid), auth.ACTIVE)


class AudioSummaryTests(unittest.TestCase):
    def test_an_expired_session_is_reported_on_the_status_message(self):
        from unibot import documents
        query = mock.AsyncMock()
        query.data = "file_audio_sum|s9"
        query.from_user = types.SimpleNamespace(id=UID)
        status = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock(return_value=status)
        with mock.patch.dict(documents.link_cache, {"s9": {"cid": "c1", "name": "slides.pdf"}}), \
                mock.patch.object(documents, "is_user_allowed", return_value=True), \
                mock.patch.object(documents, "login_studip", mock.AsyncMock(side_effect=RuntimeError("Failed to log in to Stud.IP."))), \
                mock.patch.object(documents, "_start_loading_animation", lambda *a: (None, None)), \
                mock.patch.object(documents, "_stop_loading_animation", mock.AsyncMock()):
            asyncio.run(documents.handle_file_audio_summary(types.SimpleNamespace(callback_query=query), None))
        self.assertIn("Failed to log in", status.edit_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
