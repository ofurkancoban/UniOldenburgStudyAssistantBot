"""First-run setup: the semester question comes first (current term on top),
is asked once, its answer becomes the default Files opens, and the
preferences step follows."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

import fast_enroll  # noqa: E402
from unibot import menus, onboarding, state, storage  # noqa: E402

OPTIONS = [("s29", "Sommersemester 2029"), ("w28", "Wintersemester 2028/2029"), ("s28", "Sommersemester 2028"),
           ("w27", "Wintersemester 2027/2028"), ("s27", "Sommersemester 2027"),
           ("w26", "Wintersemester 2026/2027"), ("s26", "Sommersemester 2026"), ("w25", "Wintersemester 2025/2026")]


class Query:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = types.SimpleNamespace(id=uid)
        self.answers = []
        self.edited = None

    async def answer(self, text=None, show_alert=False):
        assert not self.answers, "answered twice"
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edited = (text, kw.get("reply_markup"))


def _labels(markup):
    return [row[0].text for row in markup.inline_keyboard]


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class SemesterStepTests(unittest.TestCase):
    def test_current_first_then_next_and_previous(self):
        current = fast_enroll.current_semester_id(OPTIONS, date(2026, 11, 10))
        _text, markup = onboarding.semester_step_view(OPTIONS, current)
        self.assertEqual(_labels(markup), ["❄️ Wintersemester 2026/2027 ⭐ current", "☀️ Sommersemester 2027",
                                           "☀️ Sommersemester 2026", "❄️ Wintersemester 2025/2026", "🗓️ Ask me each time"])
        self.assertEqual(_datas(markup)[0], "setup_sem|w26")

    def test_unknown_current_lists_the_first_ones(self):
        choices = onboarding.semester_choices(OPTIONS[:2], None)
        self.assertEqual([c[0] for c in choices], ["s29", "w28"])


class SemesterIconTests(unittest.TestCase):
    def test_sun_for_summer_snow_for_winter(self):
        self.assertEqual(fast_enroll.semester_display("Sommersemester 2026"), "☀️ Sommersemester 2026")
        self.assertEqual(fast_enroll.semester_display("Wintersemester 2026/2027"), "❄️ Wintersemester 2026/2027")
        self.assertEqual(fast_enroll.semester_display("SoSe 26"), "☀️ SoSe 26")
        self.assertEqual(fast_enroll.semester_display("WiSe 2026/27"), "❄️ WiSe 2026/27")

    def test_other_labels_unchanged(self):
        self.assertEqual(fast_enroll.semester_display("Not set"), "Not set")
        self.assertEqual(fast_enroll.semester_display(""), "")

    def test_semester_pickers_show_icons(self):
        from telegram import InlineKeyboardMarkup
        sent = {}

        async def reply(sender, text, reply_markup=None):
            sent["markup"] = reply_markup

        sender = mock.MagicMock(from_user=types.SimpleNamespace(id=9303))
        session = mock.MagicMock()
        session.is_logged_in = mock.AsyncMock(return_value=True)
        with mock.patch.object(menus, "get_user_session", return_value=session), \
                mock.patch.object(menus, "get_semester_options", mock.AsyncMock(return_value=OPTIONS[5:7])), \
                mock.patch.object(menus, "_reply_or_edit", reply):
            asyncio.run(menus._show_semester_picker(sender, "files_sem", "Pick"))
        self.assertIsInstance(sent["markup"], InlineKeyboardMarkup)
        self.assertEqual(_labels(sent["markup"]), ["❄️ Wintersemester 2026/2027", "☀️ Sommersemester 2026"])
        self.assertEqual(_datas(sent["markup"]), ["files_sem|w26", "files_sem|s26"])


class SetupFlowTests(unittest.TestCase):
    UID = 9301

    def setUp(self):
        storage.clear_default_semester(self.UID)
        storage.save_personal_record(self.UID, storage.SETUP_RECORD, None)
        onboarding._setup_offered.pop(self.UID, None)
        self.allowed = mock.patch.object(onboarding, "is_user_allowed", return_value=True)
        self.allowed.start()
        self.addCleanup(self.allowed.stop)
        self.options = mock.patch.object(onboarding, "_semester_options", mock.AsyncMock(return_value=OPTIONS))
        self.options.start()
        self.addCleanup(self.options.stop)

    def _offer(self):
        bot = mock.AsyncMock()
        with mock.patch.object(onboarding, "current_semester_id", lambda o: "w26"):
            asked = asyncio.run(onboarding.offer_setup(bot, self.UID))
        return asked, bot

    def test_offered_once_and_saved_as_default(self):
        asked, bot = self._offer()
        self.assertTrue(asked)
        self.assertIn("Which semester", bot.send_message.call_args.kwargs["text"])

        query = Query("setup_sem|w26", self.UID)
        asyncio.run(onboarding.handle_setup_semester(types.SimpleNamespace(callback_query=query), None))
        self.assertEqual(storage.load_default_semester(self.UID), {"id": "w26", "label": "Wintersemester 2026/2027"})  # stored as Stud.IP names it
        text, markup = query.edited
        self.assertIn("(2/2)", text)
        self.assertIn("setup_done", _datas(markup))
        self.assertIn("settings|acad", _datas(markup))

        asked, bot = self._offer()
        self.assertFalse(asked)
        bot.send_message.assert_not_called()

    def test_ask_each_time_keeps_no_default_but_is_not_asked_again(self):
        query = Query("setup_sem|ask", self.UID)
        asyncio.run(onboarding.handle_setup_semester(types.SimpleNamespace(callback_query=query), None))
        self.assertEqual(storage.load_default_semester(self.UID), {})
        self.assertFalse(self._offer()[0])

    def test_an_existing_default_counts_as_done(self):
        storage.save_default_semester("s26", "Sommersemester 2026", user_id=self.UID)
        self.assertFalse(self._offer()[0])

    def test_unknown_semester_is_refused(self):
        query = Query("setup_sem|gone", self.UID)
        asyncio.run(onboarding.handle_setup_semester(types.SimpleNamespace(callback_query=query), None))
        self.assertTrue(query.answers[0][1])
        self.assertEqual(storage.load_default_semester(self.UID), {})

    def test_files_open_the_picked_semester(self):
        storage.save_default_semester("w26", "Wintersemester 2026/2027", user_id=self.UID)
        render = mock.AsyncMock()
        picker = mock.AsyncMock()
        with mock.patch.object(menus, "_render_files_courses_for_semester", render), \
                mock.patch.object(menus, "_show_semester_picker", picker), \
                mock.patch("unibot.auth.current_user_id", return_value=self.UID):
            asyncio.run(menus.handle_files_browse(mock.MagicMock()))
        render.assert_awaited_once()
        self.assertEqual(render.call_args.args[1], "w26")
        picker.assert_not_awaited()


class SignInHookTests(unittest.TestCase):
    def test_registered_on_startup(self):
        saved = state.on_signed_in
        self.addCleanup(setattr, state, "on_signed_in", saved)
        onboarding.register(None)
        self.assertIs(state.on_signed_in, onboarding.offer_setup)

    def test_start_offers_setup_only_when_signed_in(self):
        update = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=9302), message=mock.AsyncMock())
        context = types.SimpleNamespace(bot=mock.AsyncMock())
        hook = mock.AsyncMock()
        with mock.patch.object(state, "on_signed_in", hook), \
                mock.patch.object(menus, "is_user_allowed", return_value=True), \
                mock.patch.object(menus, "get_user_session", return_value=object()):
            with mock.patch.object(menus, "has_stored_login", return_value=False):
                asyncio.run(menus.start(update, context))
            hook.assert_not_awaited()
            with mock.patch.object(menus, "has_stored_login", return_value=True):
                asyncio.run(menus.start(update, context))
            hook.assert_awaited_once()
            self.assertEqual(hook.call_args.args[1], 9302)

if __name__ == "__main__":
    unittest.main()
