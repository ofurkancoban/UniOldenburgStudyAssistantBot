"""Yesterday's pinned morning summary is unpinned before today's arrives."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import activate_student, run_as, use_temp_db  # noqa: E402
use_temp_db()

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import morning, storage  # noqa: E402

NOW = datetime(2026, 11, 18, 0, 5, tzinfo=TZ_BERLIN)


class _Bot:
    def __init__(self, fail_unpin=False, unpin_error=None, fail_unpin_all=False):
        self.calls = []
        self.fail_unpin_all = fail_unpin_all
        self.fail_unpin = fail_unpin
        self.unpin_error = unpin_error

    async def unpin_chat_message(self, chat_id, message_id):
        self.calls.append(("unpin", chat_id, message_id))
        if self.unpin_error is not None:
            raise self.unpin_error
        if self.fail_unpin:
            raise RuntimeError("message to unpin not found")

    async def unpin_all_chat_messages(self, chat_id):
        self.calls.append(("unpin_all", chat_id))
        if self.fail_unpin_all:
            raise RuntimeError("not enough rights")

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send", chat_id))
        return types.SimpleNamespace(message_id=500)

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.calls.append(("pin", chat_id, message_id))


def _record(uid):
    return storage.personal_record(uid, morning.PINNED_SUMMARY_RECORD)


class StaleSummaryCleanupTests(unittest.TestCase):
    def _cleanup(self, bot, uid):
        return asyncio.run(morning.unpin_stale_morning_summaries(bot, [uid], NOW))

    def test_a_summary_pinned_yesterday_is_unpinned_when_the_new_day_starts(self):
        uid = 7751
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, {"id": 41, "date": "2026-11-17"})
        bot = _Bot()
        self._cleanup(bot, uid)
        self.assertEqual(bot.calls, [("unpin", uid, 41)])
        self.assertIsNone(_record(uid))

    def test_a_summary_pinned_today_stays(self):
        uid = 7752
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, {"id": 42, "date": "2026-11-18"})
        bot = _Bot()
        self._cleanup(bot, uid)
        self.assertEqual(bot.calls, [])
        self.assertEqual(_record(uid)["id"], 42)

    def test_an_old_style_record_without_a_day_is_left_for_the_next_delivery(self):
        uid = 7753
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, 43)
        bot = _Bot()
        self._cleanup(bot, uid)
        self.assertEqual(bot.calls, [])

    def test_a_failing_unpin_still_forgets_the_record_and_raises_nothing(self):
        uid = 7754
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, {"id": 44, "date": "2026-11-10"})
        self._cleanup(_Bot(fail_unpin=True), uid)
        self.assertIsNone(_record(uid))

    def test_a_passing_network_problem_keeps_the_record_for_the_next_try(self):
        from telegram.error import NetworkError
        uid = 7759
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, {"id": 49, "date": "2026-11-10"})
        self._cleanup(_Bot(unpin_error=NetworkError("timed out")), uid)
        self.assertEqual(_record(uid)["id"], 49)
        bot = _Bot()
        self._cleanup(bot, uid)  # the next cycle
        self.assertEqual(bot.calls, [("unpin", uid, 49)])
        self.assertIsNone(_record(uid))

    def test_only_the_students_own_pin_is_touched(self):
        a, b = 7755, 7756
        for uid in (a, b):
            activate_student(uid)
        storage.save_personal_record(a, morning.PINNED_SUMMARY_RECORD, {"id": 45, "date": "2026-11-17"})
        storage.save_personal_record(b, morning.PINNED_SUMMARY_RECORD, {"id": 46, "date": "2026-11-18"})
        bot = _Bot()
        asyncio.run(morning.unpin_stale_morning_summaries(bot, [a, b], NOW))
        self.assertEqual(bot.calls, [("unpin", a, 45)])
        self.assertEqual(_record(b)["id"], 46)


class DeliveryOrderTests(unittest.TestCase):
    def test_yesterdays_pin_is_removed_before_todays_summary_is_sent(self):
        uid = 7757
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, {"id": 47, "date": "2026-11-17"})
        bot = _Bot()
        with mock.patch.object(morning, "lifecycle_state", lambda u: morning.ACTIVE):
            delivered = asyncio.run(morning.deliver_morning_summary(bot, [uid], {"texts": {uid: "Good morning"}}))
        self.assertEqual(delivered, [uid])
        self.assertEqual([c[0] for c in bot.calls], ["unpin", "send", "unpin_all", "pin"])
        record = run_as(uid, _read(lambda: _record(uid)))
        self.assertEqual(record["id"], 500)
        self.assertRegex(record["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_failed_unpin_does_not_stop_the_summary(self):
        uid = 7758
        activate_student(uid)
        storage.save_personal_record(uid, morning.PINNED_SUMMARY_RECORD, 48)  # an old-style record
        bot = _Bot(fail_unpin=True)
        with mock.patch.object(morning, "lifecycle_state", lambda u: morning.ACTIVE):
            delivered = asyncio.run(morning.deliver_morning_summary(bot, [uid], {"texts": {uid: "Good morning"}}))
        self.assertEqual(delivered, [uid])
        self.assertEqual([c[0] for c in bot.calls], ["unpin", "send", "unpin_all", "pin"])

    def test_pins_cannot_pile_up_even_when_the_record_of_the_old_one_is_gone(self):
        uid = 7760
        activate_student(uid)  # no record at all: nothing is known about an earlier pin
        bot = _Bot()
        with mock.patch.object(morning, "lifecycle_state", lambda u: morning.ACTIVE):
            asyncio.run(morning.deliver_morning_summary(bot, [uid], {"texts": {uid: "Good morning"}}))
        self.assertEqual([c[0] for c in bot.calls], ["send", "unpin_all", "pin"])

    def test_a_failed_clear_still_pins_the_new_summary(self):
        uid = 7761
        activate_student(uid)
        bot = _Bot(fail_unpin_all=True)
        with mock.patch.object(morning, "lifecycle_state", lambda u: morning.ACTIVE):
            delivered = asyncio.run(morning.deliver_morning_summary(bot, [uid], {"texts": {uid: "Good morning"}}))
        self.assertEqual(delivered, [uid])
        self.assertEqual([c[0] for c in bot.calls], ["send", "unpin_all", "pin"])


def _read(function):
    async def runner():
        return function()
    return runner()


if __name__ == "__main__":
    unittest.main()
