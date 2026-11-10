"""Fixes from the thirteenth review (schedule): a cancelled class gets no
"starting in 30 minutes" reminder, and the weather heads-up goes with the
first class that is actually held."""

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
from unibot import schedule  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
