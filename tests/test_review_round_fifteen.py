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
from tests.testenv import use_temp_db  # noqa: E402
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
    def _run(self, error):
        from unibot import tasks
        removed = []
        task = {"id": "t1", "text": "Hand in", "due_time": "2020-01-01T10:00:00+01:00", "user_id": UID}
        bot = mock.AsyncMock()
        bot.send_message.side_effect = error
        with mock.patch.object(tasks, "students_with_tasks", lambda: [UID]), \
                mock.patch.object(tasks, "user_tasks", lambda uid: [task]), \
                mock.patch.object(tasks, "remove_user_tasks", lambda uid, ids: removed.append(set(ids))), \
                mock.patch.object(tasks, "remember_user_callback", lambda *a: None), \
                mock.patch.object(tasks, "record_task_outcome", lambda *a: None):
            asyncio.run(tasks.check_task_reminders(bot))
        return removed

    def test_an_unexpected_error_keeps_the_task_for_the_next_minute(self):
        self.assertEqual(self._run(RuntimeError("boom")), [])

    def test_a_blocked_chat_drops_the_task(self):
        from telegram.error import Forbidden
        self.assertEqual(self._run(Forbidden("bot was blocked by the user")), [{"t1"}])


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
        db.set_user_active(9952, False)
        targets = telegram_ui.broadcast_targets("c1", members={9951, 9952})
        self.assertIn(9951, targets)
        self.assertNotIn(9952, targets)


if __name__ == "__main__":
    unittest.main()
