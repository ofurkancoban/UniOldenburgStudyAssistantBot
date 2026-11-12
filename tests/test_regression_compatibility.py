"""Compatibility at persistent Telegram and background-job boundaries."""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from tests.testenv import use_temp_db

use_temp_db()

from unibot import auth, db, study_plan, watcher  # noqa: E402


class StudyBlockCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, data, *, recorded=True):
        query = SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=901), answer=mock.AsyncMock(),
            message=SimpleNamespace(reply_markup=None),
            edit_message_reply_markup=mock.AsyncMock(),
        )
        record = mock.Mock()
        with auth.acting_as(901), \
                mock.patch.object(study_plan, "is_user_allowed", return_value=True), \
                mock.patch.object(study_plan, "study_block_log", return_value=(
                    [{"ts": 123, "status": "sent"}] if recorded else [])), \
                mock.patch.object(study_plan, "record_study_block", record):
            await study_plan.handle_study_block_feedback(SimpleNamespace(callback_query=query), None)
        return record, query

    async def test_old_card_can_answer_its_owners_recorded_block(self):
        record, _ = await self.press("sp_block|123|done")
        record.assert_called_once_with(901, 123, "done")

    async def test_new_card_can_answer_its_owners_recorded_block(self):
        record, _ = await self.press("sp_block|901|123|done")
        record.assert_called_once_with(901, 123, "done")

    async def test_another_owners_card_is_rejected(self):
        record, query = await self.press("sp_block|902|123|done")
        record.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_legacy_card_cannot_create_an_unowned_block(self):
        record, _ = await self.press("sp_block|123|done", recorded=False)
        record.assert_not_called()


class WatcherRetryCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_naive_clock_and_legacy_retry_timestamp(self):
        cache = {"job_retry_after": "2026-11-12T12:00:00"}
        work = mock.AsyncMock()
        with mock.patch.object(watcher, "load_general_cache", side_effect=lambda: dict(cache)), \
                mock.patch.object(watcher, "save_general_cache", side_effect=lambda value: cache.update(value)), \
                mock.patch.object(watcher, "berlin_now", return_value=datetime(2026, 11, 12, 11)):
            await watcher._run_once_per_period("job", "today", "Job", "", work)
            work.assert_not_awaited()
            with mock.patch.object(watcher, "berlin_now", return_value=datetime(2026, 11, 12, 13)):
                await watcher._run_once_per_period("job", "today", "Job", "", work)
            work.assert_awaited_once()
            self.assertEqual(cache["job"], "today")

    async def test_missing_and_revoked_contexts_remain_rejected(self):
        with db.private_write_context(None):
            self.assertFalse(db.private_context_is_current(901))
        with auth.acting_as(901):
            self.assertTrue(db.private_context_is_current(901))
            db.begin_private_deletion(901)
            try:
                self.assertFalse(db.private_context_is_current(901))
            finally:
                db.finish_private_deletion(901)
