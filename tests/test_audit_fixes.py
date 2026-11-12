"""Behavioral regressions from the end-to-end bot audit."""

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from bs4 import BeautifulSoup
from telegram.error import BadRequest, NetworkError

from tests.testenv import use_temp_db

use_temp_db()

from unibot import course_registry as registry, monitoring, monitoring_fetch, schedule, state, watcher  # noqa: E402
from unibot.formatting import _telegram_length  # noqa: E402
from unibot.monitoring_diff import _diff_course_files, _file_was_updated  # noqa: E402
from studip_utils import TZ_BERLIN  # noqa: E402


class FileComparisonTests(unittest.TestCase):
    def test_different_ids_with_the_same_name_are_new_files(self):
        old = {"id:old": {"id": "old", "name": "notes.pdf", "size": "1 MB", "modified_ts": 100}}
        new = {"id:new": {"id": "new", "name": "notes.pdf", "size": "1 MB", "modified_ts": 101}}
        added, changed = _diff_course_files(new, old)
        self.assertEqual(added, list(new.values()))
        self.assertEqual(changed, [])

    def test_legacy_filename_keys_still_migrate(self):
        info = {"name": "notes.pdf", "size": "1 MB", "modified_ts": 100}
        self.assertEqual(_diff_course_files({"id:new": info}, {"notes.pdf": info}), ([], []))

    def test_precise_timestamp_detects_same_day_change(self):
        old = {"size": "1 MB", "modified_ts": 100, "modified_iso": "2026-11-12T09:00:00+01:00"}
        new = {**old, "modified_ts": 3700, "modified_iso": "2026-11-12T10:00:00+01:00"}
        self.assertTrue(_file_was_updated(old, new))
        self.assertFalse(_file_was_updated(old, old))
        self.assertFalse(_file_was_updated(new, old))

    def test_legacy_day_resolution_keeps_its_tolerance(self):
        self.assertFalse(_file_was_updated({"modified_ts": 100}, {"modified_ts": 3700}))


class ForumScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_keeps_course_identity(self):
        entry = monitoring_fetch._forum_thread_entry("A", "a", {"id": "t"}, {
            "data": [{"id": "p", "attributes": {"content-html": "Hello", "mkdate": "2026-11-12T10:00:00+01:00"}}],
        })
        self.assertEqual(entry["cid"], "a")

    async def test_scan_binds_legacy_entries_to_the_scanned_course(self):
        entry = {"key": "a:thread:t", "course": "A", "thread": "Thread", "author": "Ada",
                 "date": "2026-11-12", "body": "Hello", "is_new": True}
        fan_out = mock.AsyncMock()
        with mock.patch.object(monitoring, "_load_forum_cache", return_value={}), \
                mock.patch.object(monitoring.db, "exists", return_value=True), \
                mock.patch.object(monitoring.db, "load", return_value=["a"]), \
                mock.patch.object(monitoring.db, "save"), \
                mock.patch.object(registry, "scan_with_failover", mock.AsyncMock(return_value=([entry], None))), \
                mock.patch.object(registry, "record_scan"), \
                mock.patch.object(registry, "fan_out", fan_out), \
                mock.patch.object(monitoring, "_forum_alert_markup", return_value=None):
            await monitoring._check_new_forum_posts_parallel_unlocked(None, 901, True, [("A", "a", None)])
        self.assertEqual(fan_out.await_args.args[1], "a")

    async def test_missing_course_id_cannot_trigger_a_global_broadcast(self):
        with mock.patch.object(registry, "broadcast_targets") as targets:
            await registry.fan_out(None, None, "forum", "body", "title")
        targets.assert_not_called()


class QueueDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_morning_and_watcher_cannot_deliver_the_same_item_twice(self):
        queue = [{"title": "Update", "kind": "files", "cid": "a", "reason": "retry", "ts": 1}]
        started, release = asyncio.Event(), asyncio.Event()
        scope = SimpleNamespace(private=lambda kind: list(queue),
                                save_private=lambda kind, value: queue.__setitem__(slice(None), value))

        async def send(**kwargs):
            started.set()
            await release.wait()

        bot = SimpleNamespace(send_message=mock.AsyncMock(side_effect=send))
        with mock.patch.object(registry.db, "private_owner_ids", return_value=[901]), \
                mock.patch.object(registry.db, "get_user_scope", return_value=scope), \
                mock.patch.object(registry, "is_user_allowed", return_value=True), \
                mock.patch.object(registry, "lifecycle_state", return_value=registry.ACTIVE), \
                mock.patch.object(registry, "has_course_access", return_value=True), \
                mock.patch.object(registry, "_prune_queue", side_effect=lambda items, now: list(items)), \
                mock.patch.object(registry, "_queue_due", return_value=True):
            first = asyncio.create_task(registry.flush_alert_queues(bot))
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(registry.flush_alert_queues(bot, with_morning=True))
            await asyncio.sleep(0)
            self.assertEqual(bot.send_message.await_count, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(queue, [])


class SessionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pruning_requires_a_complete_watch_plan(self):
        for complete in (False, True):
            cache = {schedule.COURSE_SESSION_STATE_KEY: {"a": {}, "b": {"event": {}}}}
            with mock.patch.object(monitoring, "watched_courses", mock.AsyncMock(return_value=[("A", "a", None)])), \
                    mock.patch.object(registry, "scan_with_failover", mock.AsyncMock(return_value=(([], []), None))), \
                    mock.patch.object(schedule, "load_general_cache", side_effect=lambda cache=cache: dict(cache)), \
                    mock.patch.object(schedule, "save_general_cache", side_effect=lambda value, cache=cache: cache.update(value)), \
                    mock.patch.object(schedule, "_last_course_session_scan", 0), \
                    mock.patch.object(state, "course_watch_plan_complete", complete):
                await schedule.check_course_session_changes(None, datetime.now(TZ_BERLIN), force=True)
            self.assertEqual("b" in cache[schedule.COURSE_SESSION_STATE_KEY], not complete)


class HistoryMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_entry_does_not_prevent_the_next_delivery(self):
        bot = SimpleNamespace(send_message=mock.AsyncMock(side_effect=[NetworkError("offline"), None]))
        self.assertFalse(await monitoring._send_history_entry(bot, 901, "First"))
        self.assertTrue(await monitoring._send_history_entry(bot, 901, "Second"))
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "Second")

    async def test_invalid_html_falls_back_to_plain_text(self):
        bot = SimpleNamespace(send_message=mock.AsyncMock(side_effect=[BadRequest("Can't parse entities"), None]))
        self.assertTrue(await monitoring._send_history_entry(bot, 901, "<b>A &amp; B</b>"))
        self.assertEqual(bot.send_message.await_args.kwargs, {"chat_id": 901, "text": "A & B"})

    async def render(self, handler, cache):
        query = SimpleNamespace(from_user=SimpleNamespace(id=901), message=SimpleNamespace(chat_id=901),
                                answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock())
        bot = SimpleNamespace(send_message=mock.AsyncMock())
        with mock.patch.object(monitoring.db, "exists", return_value=True), \
                mock.patch.object(monitoring.db, "load", return_value=cache), \
                mock.patch.object(monitoring, "student_courses", return_value={"a": "A"}), \
                mock.patch.object(monitoring.asyncio, "sleep", mock.AsyncMock()):
            await handler(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot))
        messages = [call.kwargs["text"] for call in bot.send_message.await_args_list]
        self.assertTrue(all(_telegram_length(text) <= 4096 for text in messages))
        return messages

    async def test_long_announcement_and_null_date(self):
        body = "A & B " * 1500
        messages = await self.render(monitoring.show_last_announcements, {"history": [
            {"cid": "a", "dt": None, "body": body}, {"cid": "a", "dt": "2026-11-12", "body": "Recent"},
        ]})
        self.assertGreater(len(messages), 3)
        self.assertIn("Recent", "".join(messages))
        self.assertEqual("".join(messages).count("A &amp; B"), 1500)

    async def test_long_forum_conversation(self):
        messages = await self.render(monitoring.show_last_forum_posts, {
            "a:thread": {"cid": "a", "body": "<b>" + "Forum " * 1600 + "</b>", "date": "2026-11-12"},
        })
        self.assertGreater(len(messages), 3)
        plain = BeautifulSoup("".join(messages), "html.parser").get_text()
        self.assertEqual(plain.count("Forum "), 1601)  # includes the card heading


class WatcherSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_marker_requires_a_completed_cycle_without_failed_jobs(self):
        for broken in (False, True):
            async def sleep(seconds):
                if seconds == 180:
                    raise asyncio.CancelledError

            async def reminders(*args, broken=broken):
                if broken:
                    await watcher._guarded("Broken job", mock.AsyncMock(side_effect=RuntimeError("down")))

            jobs = {name: mock.AsyncMock() for name in (
                "_run_exam_date_checks", "_run_health_check", "_run_daily_backup",
                "_run_daily_digest", "_run_weekly_report", "_run_content_watchers",
            )}
            token = watcher._cycle_failures.set(None)
            try:
                with mock.patch.multiple(watcher, **jobs), \
                        mock.patch.object(watcher, "_run_reminder_checks", side_effect=reminders), \
                        mock.patch.object(watcher, "_admin_watcher_session", mock.AsyncMock(return_value=None)), \
                        mock.patch.object(watcher, "PRIMARY_ADMIN_ID", 901), \
                        mock.patch.object(watcher, "_should_skip_cycle", return_value=False), \
                        mock.patch.object(watcher.asyncio, "sleep", side_effect=sleep), \
                        mock.patch.object(state, "global_watcher_paused", False), \
                        mock.patch.object(state, "watcher_controller_running", False), \
                        mock.patch.object(state, "global_scan_count", 0), \
                        mock.patch.object(state, "last_full_check_time", None), \
                        self.assertLogs(level="INFO") as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await watcher.unified_watcher_controller(SimpleNamespace(bot=None))
                self.assertEqual(any("Watcher cycle healthy pid=" in line for line in logs.output), not broken)
            finally:
                watcher._cycle_failures.reset(token)

    async def test_deadline_jobs_run_while_indexing_is_blocked(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calendar, tasks = mock.AsyncMock(), mock.AsyncMock()

        async def slow(*args):
            entered.set()
            await release.wait()

        app = SimpleNamespace(bot=None)
        with mock.patch.object(state, "global_watcher_paused", False), \
                mock.patch.object(watcher, "_run_scheduled_jobs", side_effect=slow), \
                mock.patch.object(watcher, "_send_calendar_reminders", calendar), \
                mock.patch.object(watcher, "check_task_reminders", tasks):
            heavy = asyncio.create_task(watcher.scheduled_jobs_loop(app))
            await asyncio.wait_for(entered.wait(), 1)
            timely = asyncio.create_task(watcher.deadline_reminders_loop(app))
            try:
                for _ in range(20):
                    if calendar.await_count and tasks.await_count:
                        break
                    await asyncio.sleep(0.01)
                calendar.assert_awaited_once()
                tasks.assert_awaited_once()
                self.assertFalse(heavy.done())
            finally:
                heavy.cancel()
                timely.cancel()
                await asyncio.gather(heavy, timely, return_exceptions=True)

    async def test_guarded_job_timeout_is_recorded_for_health(self):
        token = watcher._cycle_failures.set([])
        try:
            with mock.patch.object(watcher, "JOB_TIMEOUT_SECONDS", 0.01):
                result = await watcher._guarded("stuck", asyncio.sleep, 60)
            self.assertIsNone(result)
            self.assertEqual(watcher._cycle_failures.get(), ["stuck"])
        finally:
            watcher._cycle_failures.reset(token)
