"""/menu reads the menu with the student's own signed-in session (like the
Today button), and a Stud.IP sign-in page is not reported as a closed Mensa."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import mensa, state  # noqa: E402

MENU_HTML = '<table class="default"><tr><th>Hauptgericht</th></tr></table>'
LOGIN_HTML = '<form action="/dispatch.php/login"><input name="loginname"></form>'


class _Page:
    def __init__(self, text):
        self._text = text
        self.status = 200

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session(text):
    sess = mock.MagicMock()
    sess.get = mock.AsyncMock(return_value=_Page(text))
    return sess


class MenuCommandSessionTests(unittest.TestCase):
    def test_menu_uses_the_students_own_session(self):
        own, watcher = _session(MENU_HTML), _session(LOGIN_HTML)
        update = mock.MagicMock()
        update.callback_query = None
        update.effective_user.id = 5
        update.effective_message.reply_text = mock.AsyncMock()
        fetch = mock.AsyncMock(return_value=("menu", None, None, None, [], None))
        with mock.patch.object(mensa, "is_user_allowed", return_value=True), \
                mock.patch.object(mensa, "login_studip", mock.AsyncMock(return_value=own)), \
                mock.patch.object(state, "global_session", watcher), \
                mock.patch.object(mensa, "get_todays_menu_enhanced", fetch):
            asyncio.run(mensa.menu_command(update, mock.MagicMock()))
        self.assertIs(fetch.await_args.args[0], own)

    def test_a_sign_in_page_is_not_a_closed_mensa(self):
        text, *_ = asyncio.run(mensa.get_todays_menu_enhanced(_session(LOGIN_HTML)))
        self.assertIn("/login", text)
        self.assertNotIn("might be closed", text)

    def test_an_empty_menu_page_still_says_closed(self):
        text, *_ = asyncio.run(mensa.get_todays_menu_enhanced(_session("<div>Keine Speisen</div>")))
        self.assertIn("might be closed", text)


if __name__ == "__main__":
    unittest.main()
