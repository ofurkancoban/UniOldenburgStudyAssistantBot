"""Fixes from an outside review: invite labels are escaped in the admin
panel, invite codes are recognised without dashes, long forum alerts keep
their newest message and valid HTML, and an alert Telegram can't parse is
still delivered as plain text."""

import asyncio
import os
import random
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from unibot import auth, course_registry, formatting, monitoring_alerts, system  # noqa: E402

_TAG = re.compile(r"<(/?)([a-z]+)[^>]*>")


def _assert_valid_html(case: unittest.TestCase, text: str) -> None:
    """Balanced tags, no half tag, no bare "&" (what Telegram's parser needs)."""
    stack = []
    for closing, name in _TAG.findall(text):
        if closing:
            case.assertTrue(stack and stack[-1] == name, text[-80:])
            stack.pop()
        else:
            stack.append(name)
    case.assertEqual(stack, [], text[-80:])
    case.assertEqual(text.count("<"), len(_TAG.findall(text)))
    case.assertIsNone(re.search(r"&(?!(amp|lt|gt|quot|#x27);)", text), text[-80:])


class TruncateHtmlTests(unittest.TestCase):
    def test_every_cut_point_keeps_valid_html(self):
        rng = random.Random(7)
        pieces = ["<b>bold</b>", "<i>it <b>both</b></i>", "&amp;", "&lt;tag&gt;", "plain words ",
                  '<a href="https://x.example/a?b=1&amp;c=2">link</a>', "<code>x &gt; 1</code>", "\n"]
        text = "".join(rng.choice(pieces) for _ in range(120))
        for limit in range(45, len(text) + 5):
            cut = formatting.truncate_html(text, limit)
            self.assertLessEqual(len(cut), max(limit, len(text) if limit >= len(text) else limit))
            _assert_valid_html(self, cut)

    def test_short_text_is_untouched(self):
        self.assertEqual(formatting.truncate_html("<b>a</b>", 100), "<b>a</b>")


class ForumAlertTests(unittest.TestCase):
    def _post(self, messages: int):
        from html import escape
        history = [f"👤 <b>Student {i}</b> <i>(today)</i>:\n{escape(('message & reply ' * 30)[:400])} #{i}"
                   for i in range(messages)]
        return {"course": "Stats", "thread": "Exam", "author": "A", "date": "now",
                "body": "💬 <b>Conversation History:</b>\n\n" + "\n\n".join(history)}

    def test_a_long_thread_drops_old_messages_and_keeps_the_newest(self):
        text = monitoring_alerts._forum_alert_text(self._post(15))
        self.assertLessEqual(len(text), formatting.TELEGRAM_HTML_LIMIT)
        self.assertIn("#14", text)      # the newest message is whole
        self.assertNotIn("#0", text)    # the oldest went first
        self.assertIn(monitoring_alerts.FORUM_OMITTED, text)
        _assert_valid_html(self, text)

    def test_a_short_thread_is_unchanged(self):
        post = self._post(2)
        text = monitoring_alerts._forum_alert_text(post)
        self.assertIn(post["body"], text)
        self.assertNotIn(monitoring_alerts.FORUM_OMITTED, text)

    def test_one_huge_message_is_cut_safely(self):
        post = {"course": "C", "thread": "T", "author": "A", "date": "d",
                "body": "📩 <b>Last message:</b>\n" + "a &amp; b " * 900}
        text = monitoring_alerts._forum_alert_text(post)
        self.assertLessEqual(len(text), formatting.TELEGRAM_HTML_LIMIT)
        _assert_valid_html(self, text)


class PlainFallbackTests(unittest.TestCase):
    def test_an_unparseable_alert_still_arrives_as_plain_text(self):
        from telegram.error import BadRequest
        sent = []

        async def send_message(**kwargs):
            if kwargs.get("parse_mode") == "HTML":
                raise BadRequest("Can't parse entities: unsupported start tag")
            sent.append(kwargs["text"])

        bot = mock.MagicMock()
        bot.send_message = send_message
        with mock.patch.object(course_registry, "delivery_format", return_value="full"), \
                mock.patch.object(course_registry, "_hold_reason", return_value=None), \
                mock.patch.object(course_registry, "get_pref", return_value="card"), \
                mock.patch.object(course_registry.asyncio, "sleep", mock.AsyncMock()):
            counts = asyncio.run(course_registry.fan_out(
                bot, "c1", "forum", "<b>Post</b> &amp; <oops>", "t", recipients={5}))
        self.assertEqual(counts["full"], 1)
        self.assertEqual(sent, ["Post & "])


class InviteLabelTests(unittest.TestCase):
    def test_labels_with_markup_dont_break_the_invite_list(self):
        query = mock.MagicMock()
        query.from_user.id = 1
        query.data = "admin_invites"
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        invites = [{"code": "UNI-ABC-123", "label": "CS <Group A> & Math", "is_used": False,
                    "expires_at": 9e12}]
        with mock.patch.object(system, "is_user_admin", return_value=True), \
                mock.patch.object(system.db, "list_invite_tokens", return_value=invites):
            asyncio.run(system.handle_admin_callbacks(mock.MagicMock(callback_query=query), mock.MagicMock()))
        text = query.edit_message_text.call_args.args[0]
        self.assertIn("CS &lt;Group A&gt; &amp; Math", text)
        self.assertNotIn("<Group A>", text)


class InviteCodeShapeTests(unittest.TestCase):
    def test_codes_without_dashes_or_with_spaces_are_tried(self):
        from telegram.ext import ApplicationHandlerStop
        auth._reset_invite_failures()
        self.addCleanup(auth._reset_invite_failures)
        for written in ("uniabc123", "UNI abc 123", "/start UNIABC123"):
            update = mock.MagicMock()
            update.effective_user.id = 9931
            update.effective_chat.type = "private"
            update.effective_message.text = written
            update.effective_message.reply_text = mock.AsyncMock()
            update.callback_query = None
            redeem = mock.MagicMock(return_value=(False, "Invalid invite code."))
            with mock.patch.object(auth, "is_user_allowed", return_value=False), \
                    mock.patch.object(auth, "redeem_invite_token", redeem):
                with self.assertRaises(ApplicationHandlerStop):
                    asyncio.run(auth._authorization_gate(update, mock.MagicMock()))
            self.assertEqual(auth.invite_code_in(redeem.call_args.args[0]), "UNIABC123", written)

    def test_ordinary_words_are_not_codes(self):
        for text in ("University", "uni", "UNI-XYZ-123", "uniabc1234", "hello"):
            self.assertIsNone(auth.invite_code_in(text), text)


if __name__ == "__main__":
    unittest.main()
