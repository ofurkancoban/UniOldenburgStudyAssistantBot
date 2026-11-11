"""Telegram UI tests: every inline button reaches a handler, the access gate,
the exam register/deregister flow and the fast-enroll wizard.

Telegram objects are stand-ins (MagicMock/AsyncMock); nothing talks to
Telegram, Stud.IP or StuMS.
"""

import ast
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

from telegram.ext import ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler  # noqa: E402

from unibot import app as app_module  # noqa: E402
from unibot import auth, enrollment, exams  # noqa: E402

# Files whose InlineKeyboardButton(callback_data=...) values are scanned.
BUTTON_SOURCES = ["exam_reminder.py"] + [
    os.path.join("unibot", f) for f in sorted(os.listdir(os.path.join(ROOT, "unibot"))) if f.endswith(".py")
]


PLACEHOLDER = "1"


def _callback_samples() -> list[tuple[str, int, str]]:
    """(file, line, sample callback_data) for every literal or f-string
    callback_data in the code. An f-string's placeholders become "1", so
    f"exam_do|{sid}" is checked as "exam_do|1" (a digit also satisfies
    patterns such as sp_plan|\\d+)."""
    samples = []
    for rel in BUTTON_SOURCES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "callback_data":
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                samples.append((rel, value.lineno, value.value))
            elif isinstance(value, ast.JoinedStr):
                text = "".join(part.value if isinstance(part, ast.Constant) else PLACEHOLDER for part in value.values)
                samples.append((rel, value.lineno, text))
    return samples


def _query(data: str, user_id: int):
    query = mock.MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.answer = mock.AsyncMock()
    query.edit_message_text = mock.AsyncMock()
    query.message.reply_text = mock.AsyncMock()
    update = mock.MagicMock()
    update.callback_query = query
    return update, query


class EveryButtonHasAHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        application = ApplicationBuilder().token("123456:TEST-TOKEN").build()
        app_module.register_handlers(application)
        os.chdir(cwd)
        cls.patterns = [
            h.pattern for handlers in application.handlers.values() for h in handlers
            if isinstance(h, CallbackQueryHandler)
        ]

    def test_the_only_catch_all_is_the_selection_router(self):
        # handle_selection has no pattern; it answers only its own table's
        # keys (anything else gets "Unknown action"), so the check below
        # counts a button as answered only through a pattern or that table
        self.assertEqual(self.patterns.count(None), 1)

    def _answered(self, data: str) -> bool:
        from unibot import menus
        if any(p is not None and p.match(data) for p in self.patterns):
            return True
        return data.split("|")[0] in menus._SELECTION_HANDLERS

    def test_every_button_reaches_a_handler(self):
        samples = _callback_samples()
        self.assertGreater(len(samples), 100)  # the scan really found the buttons
        # a fully dynamic value (f"{prefix}|{sid}") is checked where the prefix is chosen
        checked = [(rel, line, data) for rel, line, data in samples if not data.startswith(PLACEHOLDER)]
        dead = [f"{rel}:{line} {data!r}" for rel, line, data in checked if not self._answered(data)]
        self.assertEqual(dead, [], "buttons nothing answers when tapped")

    def test_callback_data_fits_telegram_limit(self):
        # Telegram rejects callback_data over 64 bytes; placeholders count as one char
        # here, so this only catches long fixed parts
        too_long = [f"{rel}:{line}" for rel, line, data in _callback_samples() if len(data.encode()) > 64]
        self.assertEqual(too_long, [])


class MainMenuButtonTests(unittest.TestCase):
    def test_main_menu_button_brings_back_the_keyboard(self):
        from unibot import menus
        update, query = _query("show_menu", 7)
        with mock.patch.object(menus, "is_user_allowed", lambda uid: True):
            asyncio.run(menus.handle_selection(update, mock.MagicMock()))
        self.assertEqual(query.message.reply_text.await_args.args[0], "🏠 Main menu")
        self.assertIsNotNone(query.message.reply_text.await_args.kwargs["reply_markup"].keyboard)


class AuthorizationGateTests(unittest.TestCase):
    def _update(self, user_id, text="hello"):
        update = mock.MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.type = "private"
        update.effective_message.text = text
        update.effective_message.reply_text = mock.AsyncMock()
        update.callback_query = None
        return update

    def test_allowed_user_passes_and_becomes_the_current_user(self):
        update = self._update(11)
        seen = []

        async def scenario():
            await auth._authorization_gate(update, mock.MagicMock())
            seen.append(auth.current_user_id())

        with mock.patch.object(auth, "is_user_allowed", lambda uid: uid == 11), \
                mock.patch.object(auth, "update_user_activity", lambda *a, **k: None):
            asyncio.run(scenario())
        self.assertEqual(seen, [11])

    def test_unknown_user_is_stopped_before_any_handler(self):
        update = self._update(99)
        with mock.patch.object(auth, "is_user_allowed", lambda uid: False):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(update, mock.MagicMock()))
        update.effective_message.reply_text.assert_awaited()  # told how to get access

    def test_wrong_invite_code_is_stopped(self):
        update = self._update(99, text="UNI-ABC-999")
        with mock.patch.object(auth, "is_user_allowed", lambda uid: False), \
                mock.patch.object(auth, "redeem_invite_token", lambda *a, **k: (False, "unknown code")):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(update, mock.MagicMock()))
        self.assertIn("Invalid Code", update.effective_message.reply_text.await_args.args[0])


class ExamButtonTests(unittest.TestCase):
    OWNER, OTHER = 101, 202

    def setUp(self):
        exams.exam_action_cache.clear()
        exams.exam_action_cache["s1"] = {"unit_id": "777", "action_type": "anmelden", "title": "Statistics",
                                         "user_id": self.OWNER}
        self.addCleanup(exams.exam_action_cache.clear)
        self.sessions = {uid: mock.MagicMock(name=f"session-{uid}") for uid in (self.OWNER, self.OTHER)}
        self.submit = mock.AsyncMock(return_value={"success": True, "status": "angemeldet", "title": "Statistics"})
        patches = [
            mock.patch.object(exams, "is_user_allowed", lambda uid: True),
            mock.patch.object(exams, "get_user_session", lambda uid=None: self.sessions[uid]),
            mock.patch.object(exams, "submit_exam_action", self.submit),
            mock.patch.object(exams, "_show_typing", mock.AsyncMock()),
            mock.patch.object(exams, "_start_loading_animation", lambda *a, **k: (None, None)),
            mock.patch.object(exams, "_stop_loading_animation", mock.AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tap(self, data, user_id):
        update, query = _query(data, user_id)
        asyncio.run(exams.handle_exam_buttons(update, mock.MagicMock()))
        return query

    def test_ask_shows_a_confirmation_first(self):
        query = self.tap("exam_ask|s1", self.OWNER)
        text = query.message.reply_text.await_args.args[0]
        self.assertIn("register for", text)
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual([b.callback_data for b in markup.inline_keyboard[0]], ["exam_do|s1", "exam_cancel"])
        self.submit.assert_not_awaited()

    def test_confirm_registers_with_the_tapping_students_session(self):
        query = self.tap("exam_do|s1", self.OWNER)
        self.submit.assert_awaited_once_with(self.sessions[self.OWNER], "777", "anmelden")
        self.assertIn("✅ Registered: Statistics", query.message.reply_text.await_args.args[0])
        self.assertNotIn("s1", exams.exam_action_cache)  # one action per button

    def test_another_students_tap_does_nothing(self):
        query = self.tap("exam_do|s1", self.OTHER)
        self.submit.assert_not_awaited()
        self.assertIn("expired", query.message.reply_text.await_args.args[0])
        self.assertIn("s1", exams.exam_action_cache)  # still usable by its owner

    def test_expired_button(self):
        query = self.tap("exam_do|gone", self.OWNER)
        self.submit.assert_not_awaited()
        self.assertIn("expired", query.message.reply_text.await_args.args[0])

    def test_failure_is_reported_not_raised(self):
        self.submit.side_effect = RuntimeError("StuMS down")
        query = self.tap("exam_do|s1", self.OWNER)
        self.assertIn("Failed: StuMS down", query.message.reply_text.await_args.args[0])

    def test_mute_only_touches_the_tapping_students_cache(self):
        exams.exam_action_cache["s1"]["code"] = "stat101"
        saved = {}
        update, query = _query("examopen_mute_confirm|s1", self.OWNER)
        with mock.patch.object(exams, "load_exam_cache", lambda user_id=None: {"muted_codes": []}), \
                mock.patch.object(exams, "save_exam_cache", lambda data, user_id=None: saved.update({user_id: data})):
            asyncio.run(exams.handle_examopen_mute_confirm(update, mock.MagicMock()))
        self.assertEqual(saved, {self.OWNER: {"muted_codes": ["stat101"]}})


class FastEnrollWizardTests(unittest.TestCase):
    def _reply(self, context, step, text):
        update = mock.MagicMock()
        update.effective_chat.id = 555
        update.message.reply_text = mock.AsyncMock()
        handled = asyncio.run(enrollment._handle_fastenroll_wizard_reply(update, context, step, text))
        return handled, update.message.reply_text.await_args.args[0]

    def test_time_then_link_schedules_the_job(self):
        context = mock.MagicMock()
        context.user_data = {"pending_step": "fastenroll_time", "fastenroll_wizard": {"target_date_str": "10.11.2026"}}
        schedule = mock.AsyncMock(return_value="⏰ scheduled")
        with mock.patch.object(enrollment, "schedule_fastenroll_job", schedule), \
                mock.patch.object(enrollment, "get_main_keyboard", lambda: None):
            handled, _ = self._reply(context, "fastenroll_time", "08:30:00")
            self.assertTrue(handled)
            self.assertEqual(context.user_data["pending_step"], "fastenroll_link")
            _, answer = self._reply(context, "fastenroll_link", "https://elearning.uni-oldenburg.de/x?cid=" + "a" * 32)
        schedule.assert_awaited_once_with(555, "a" * 32, "08:30:00", "10.11.2026", context)
        self.assertEqual(answer, "⏰ scheduled")
        self.assertNotIn("pending_step", context.user_data)  # wizard finished
        self.assertNotIn("fastenroll_wizard", context.user_data)

    def test_bad_time_asks_again(self):
        context = mock.MagicMock()
        context.user_data = {"pending_step": "fastenroll_time"}
        with mock.patch.object(enrollment, "get_main_keyboard", lambda: None):
            handled, answer = self._reply(context, "fastenroll_time", "half past eight")
        self.assertTrue(handled)
        self.assertIn("HH:MM", answer)
        self.assertEqual(context.user_data["pending_step"], "fastenroll_time")

    def test_unrelated_step_is_left_alone(self):
        context = mock.MagicMock()
        context.user_data = {}
        update = mock.MagicMock()
        handled = asyncio.run(enrollment._handle_fastenroll_wizard_reply(update, context, "task_text", "hi"))
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
