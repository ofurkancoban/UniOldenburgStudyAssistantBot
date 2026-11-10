"""Fixes from the tenth review: a student's course listing stays in their
own map (and a picked semester's listing isn't recorded), changing the
default semester rebuilds the watcher's course list for its own student
only, a rate-limited reminder is kept, and the setup question isn't sent
twice in a row."""

import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from telegram.error import RetryAfter  # noqa: E402

from studip_utils import TZ_BERLIN  # noqa: E402
from unibot import auth, enrollment, onboarding, settings_menu, state, storage, studip_api, tasks  # noqa: E402

WATCHER, STUDENT = 9401, 9402


class _StateGuard(unittest.TestCase):
    def setUp(self):
        saved = (dict(state.courses_map), {k: dict(v) for k, v in state.user_courses_map.items()},
                 state.global_session, state._courses_map_built_with_prev)

        def restore():
            state.courses_map = saved[0]
            state.user_courses_map.clear()
            state.user_courses_map.update(saved[1])
            state.global_session, state._courses_map_built_with_prev = saved[2], saved[3]

        self.addCleanup(restore)
        state.courses_map = {"w1": "Watched"}
        state.user_courses_map.clear()
        state.global_session = types.SimpleNamespace(user_id=WATCHER)
        state._courses_map_built_with_prev = False


class CourseListingOwnerTests(_StateGuard):
    def _list(self, **kw):
        api = mock.AsyncMock(return_value=[("Statistics", "c1")])
        with mock.patch.object(studip_api, "_list_courses_api", api):
            asyncio.run(studip_api.list_courses(**kw))
        return api

    def test_student_listing_goes_to_their_own_map(self):
        with auth.acting_as(STUDENT):
            api = self._list()
        self.assertEqual(api.call_args.kwargs["user_id"], STUDENT)
        self.assertEqual(state.user_courses_map.get(STUDENT), {"c1": "Statistics"})
        self.assertEqual(state.courses_map, {"w1": "Watched"})

    def test_session_owner_wins(self):
        self._list(session=types.SimpleNamespace(user_id=STUDENT))
        self.assertEqual(state.user_courses_map.get(STUDENT), {"c1": "Statistics"})
        self.assertEqual(state.courses_map, {"w1": "Watched"})

    def test_picked_semester_is_not_recorded(self):
        with auth.acting_as(STUDENT):
            self._list(semester_id="old")
        self.assertNotIn(STUDENT, state.user_courses_map)
        self.assertEqual(state.courses_map, {"w1": "Watched"})

    def test_watcher_student_browsing_old_term_keeps_the_watched_list(self):
        with auth.acting_as(WATCHER):
            self._list(semester_id="old")
        self.assertEqual(state.courses_map, {"w1": "Watched"})

    def test_watcher_student_current_listing_feeds_the_shared_map(self):
        with auth.acting_as(WATCHER):
            self._list()
        self.assertEqual(state.courses_map, {"w1": "Watched", "c1": "Statistics"})

    def test_background_listing_without_student_feeds_the_shared_map(self):
        with auth.acting_as(None):
            self._list()
        self.assertEqual(state.courses_map, {"w1": "Watched", "c1": "Statistics"})


def _query(data, uid):
    q = mock.AsyncMock()
    q.data = data
    q.from_user = types.SimpleNamespace(id=uid)
    return types.SimpleNamespace(callback_query=q)


class WatchedListRebuildTests(_StateGuard):
    def _pick(self, uid, data="default_sem|w26"):
        with mock.patch.object(enrollment, "is_user_allowed", return_value=True), \
                mock.patch.object(enrollment, "login_studip", mock.AsyncMock()), \
                mock.patch.object(enrollment, "get_semester_options",
                                  mock.AsyncMock(return_value=[("w26", "Wintersemester 2026/2027")])):
            handler = enrollment.handle_default_semester_clear if data == "default_sem_clear" \
                else enrollment.handle_default_semester_pick
            asyncio.run(handler(_query(data, uid), None))

    def test_watcher_student_default_change_rebuilds(self):
        self._pick(WATCHER)
        self.assertIsNone(state._courses_map_built_with_prev)
        self.assertEqual(state.courses_map, {"w1": "Watched"})  # kept until the rebuild

    def test_watcher_student_clear_rebuilds(self):
        self._pick(WATCHER, "default_sem_clear")
        self.assertIsNone(state._courses_map_built_with_prev)

    def test_other_student_leaves_the_watcher_alone(self):
        self._pick(STUDENT)
        self._pick(STUDENT, "default_sem_clear")
        self.assertIs(state._courses_map_built_with_prev, False)

    def test_previous_semester_toggles(self):
        with mock.patch.object(enrollment, "is_user_allowed", return_value=True):
            asyncio.run(enrollment.handle_toggle_watch_prev_sem(_query("toggle_watch_prev_sem", STUDENT), None))
        self.assertEqual(state.courses_map, {"w1": "Watched"})
        self.assertIs(state._courses_map_built_with_prev, False)
        settings_menu.apply_action(WATCHER, ["settings", "flip", "watch_previous_semester"])
        self.assertEqual(state.courses_map, {"w1": "Watched"})
        self.assertIsNone(state._courses_map_built_with_prev)


class RateLimitedReminderTests(unittest.TestCase):
    def test_retry_after_keeps_the_task(self):
        uid = 9403
        self.addCleanup(storage.save_user_tasks, uid, [])
        past = (datetime.now(TZ_BERLIN) - timedelta(minutes=1)).isoformat()
        storage.save_user_tasks(uid, [{"id": "r1", "user_id": uid, "text": "x", "due_time": past}])

        class Bot:
            async def send_message(self, **kw):
                raise RetryAfter(30)

        asyncio.run(tasks.check_task_reminders(Bot()))
        self.assertEqual([t["id"] for t in storage.user_tasks(uid)], ["r1"])


class SetupOfferOnceTests(unittest.TestCase):
    UID = 9404

    def setUp(self):
        onboarding._setup_offered.pop(self.UID, None)
        self.addCleanup(onboarding._setup_offered.pop, self.UID, None)
        for p in (mock.patch.object(onboarding, "setup_done", return_value=False),
                  mock.patch.object(onboarding, "_semester_options",
                                    mock.AsyncMock(return_value=[("w26", "Wintersemester 2026/2027")]))):
            p.start()
            self.addCleanup(p.stop)

    def test_not_sent_twice_in_a_row(self):
        bot = mock.AsyncMock()
        self.assertTrue(asyncio.run(onboarding.offer_setup(bot, self.UID)))
        self.assertFalse(asyncio.run(onboarding.offer_setup(bot, self.UID)))
        self.assertEqual(bot.send_message.await_count, 1)

    def test_offered_again_later(self):
        bot = mock.AsyncMock()
        asyncio.run(onboarding.offer_setup(bot, self.UID))
        onboarding._setup_offered[self.UID] = time.time() - onboarding.SETUP_REOFFER_SECONDS - 1
        self.assertTrue(asyncio.run(onboarding.offer_setup(bot, self.UID)))

    def test_concurrent_offers_send_one(self):
        bot = mock.AsyncMock()

        async def both():
            return await asyncio.gather(onboarding.offer_setup(bot, self.UID), onboarding.offer_setup(bot, self.UID))

        self.assertEqual(sorted(asyncio.run(both())), [False, True])
        self.assertEqual(bot.send_message.await_count, 1)

    def test_failed_send_can_be_retried_at_once(self):
        bot = mock.AsyncMock()
        bot.send_message.side_effect = [Exception("blocked"), None]
        self.assertFalse(asyncio.run(onboarding.offer_setup(bot, self.UID)))
        self.assertTrue(asyncio.run(onboarding.offer_setup(bot, self.UID)))


if __name__ == "__main__":
    unittest.main()
