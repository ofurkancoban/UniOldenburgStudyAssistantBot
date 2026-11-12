"""The Stud.IP name from the watcher session belongs to that session's
student, not automatically to the primary admin, and the profile view
syncs a missing name instead of showing a placeholder."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import run_handler, use_temp_db  # noqa: E402
use_temp_db()

from unibot import db, menus, state, storage, studip_api  # noqa: E402

PRIMARY, OTHER_ADMIN = 7101, 7102


def _forget(*uids):
    for uid in uids:
        db.get_user_scope(uid).delete_private()
        storage._names_migrated.discard(uid)


class WatcherNameOwnerTests(unittest.TestCase):
    def setUp(self):
        _forget(PRIMARY, OTHER_ADMIN)
        self.addCleanup(_forget, PRIMARY, OTHER_ADMIN)
        patcher = mock.patch.object(storage, "PRIMARY_ADMIN_ID", PRIMARY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch_as_watcher(self, owner):
        watcher = mock.MagicMock(user_id=owner)
        reply = {"data": {"id": "s1", "attributes": {"formatted-name": "Second Admin", "username": "sa"}}}
        with mock.patch.object(state, "global_session", watcher), \
                mock.patch.object(state, "_studip_user_profile", None), \
                mock.patch.object(state, "_studip_api_user_id", None), \
                mock.patch.object(studip_api, "studip_api_get", mock.AsyncMock(return_value=reply)):
            asyncio.run(studip_api.get_studip_user_profile())

    def test_the_name_goes_to_the_watcher_sessions_own_student(self):
        self._fetch_as_watcher(OTHER_ADMIN)
        self.assertEqual(storage.get_user_name(OTHER_ADMIN), "Second Admin")
        self.assertIsNone(storage.get_user_name(PRIMARY))  # not the primary admin's name

    def test_the_primary_admins_own_session_still_names_them(self):
        self._fetch_as_watcher(PRIMARY)
        self.assertEqual(storage.get_user_name(PRIMARY), "Second Admin")

    def test_an_unowned_watcher_session_names_nobody(self):
        self._fetch_as_watcher(None)
        self.assertIsNone(storage.get_user_name(PRIMARY))
        self.assertIsNone(storage.get_user_name(OTHER_ADMIN))


class ProfileViewTests(unittest.TestCase):
    def setUp(self):
        _forget(OTHER_ADMIN)
        self.addCleanup(_forget, OTHER_ADMIN)

    def _open_profile(self, signed_in, profile):
        query = mock.MagicMock()
        query.from_user.id = OTHER_ADMIN
        query.data = "status_group|profile"
        query.answer = mock.AsyncMock()
        query.message.reply_text = mock.AsyncMock()
        with mock.patch.object(menus, "is_user_allowed", return_value=True), \
                mock.patch.object(menus, "has_stored_login", return_value=signed_in), \
                mock.patch.object(menus, "get_user_session", return_value=mock.MagicMock()), \
                mock.patch.object(menus, "get_studip_user_profile", mock.AsyncMock(return_value=profile)):
            run_handler(menus.handle_status_group, mock.MagicMock(callback_query=query), mock.MagicMock())
        return query.message.reply_text.await_args.args[0]

    def test_a_missing_name_is_fetched_and_kept(self):
        text = self._open_profile(True, {"display_name": "Ada Lovelace"})
        self.assertIn("Ada Lovelace", text)
        self.assertEqual(storage.get_user_name(OTHER_ADMIN), "Ada Lovelace")
        self.assertNotIn("Connected", text)

    def test_no_placeholder_when_the_name_is_unknown(self):
        self.assertIn("not synced from Stud.IP yet", self._open_profile(True, {}))
        self.assertIn("/login", self._open_profile(False, {}))


if __name__ == "__main__":
    unittest.main()
