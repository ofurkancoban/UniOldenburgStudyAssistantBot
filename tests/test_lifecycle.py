"""Rate limiting, the Telegram safety layer, invite codes and the user
lifecycle (ACTIVE, NEEDS_REAUTH, INACTIVE)."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

from telegram.error import RetryAfter  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

from unibot import auth, db  # noqa: E402


def _update(uid, text=None, callback=None):
    update = mock.MagicMock()
    update.effective_user.id = uid
    update.effective_user.first_name = "Ada"
    update.effective_chat.type = "private"
    if callback is not None:
        update.callback_query.data = callback
        update.callback_query.answer = mock.AsyncMock()
    else:
        update.callback_query = None
    update.effective_message.text = text
    update.effective_message.reply_text = mock.AsyncMock()
    return update


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        auth._rate_hits.clear()
        auth._rate_cooldown_until.clear()

    def test_two_per_second(self):
        self.assertEqual([auth.rate_limit(1, t) for t in (0.0, 0.3, 0.6)], ["ok", "ok", "warn"])
        self.assertEqual(auth.rate_limit(1, 1.0), "drop")          # cooling down
        self.assertEqual(auth.rate_limit(1, 0.6 + auth.RATE_COOLDOWN_SECONDS + 0.1), "ok")

    def test_twenty_per_minute(self):
        verdicts = [auth.rate_limit(2, i * 0.6) for i in range(21)]  # under 2/s, but 21 in 12 s
        self.assertEqual(verdicts[:20], ["ok"] * 20)
        self.assertEqual(verdicts[20], "warn")

    def test_other_users_are_not_affected(self):
        for t in (0.0, 0.1, 0.2):
            auth.rate_limit(3, t)
        self.assertEqual(auth.rate_limit(4, 0.2), "ok")

    def test_gate_warns_once_then_drops(self):
        with mock.patch.object(auth, "is_user_allowed", lambda uid: True), \
                mock.patch.object(auth, "update_user_activity", lambda *a, **k: None), \
                mock.patch.object(auth, "lifecycle_state", lambda uid: auth.ACTIVE):
            for _ in range(auth.RATE_PER_SECOND):
                asyncio.run(auth._authorization_gate(_update(5, callback="x"), None))
            spam = _update(5, callback="x")
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(spam, None))
            spam.callback_query.answer.assert_awaited_with(auth.RATE_WARNING, show_alert=True)
            again = _update(5, callback="x")
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(again, None))
            again.callback_query.answer.assert_awaited_with()  # silent while cooling down


class TelegramSafetyTests(unittest.TestCase):
    def test_flood_wait_is_waited_out_and_retried(self):
        from unibot.telegram_ui import TelegramSafetyLimiter
        calls = []

        async def send(**kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RetryAfter(timedelta(seconds=1))
            return True

        limiter = TelegramSafetyLimiter()
        slept = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            slept.append(seconds)
            await real_sleep(0)

        with mock.patch("unibot.telegram_ui.asyncio.sleep", fake_sleep):
            result = asyncio.run(limiter.process_request(send, (), {"text": "hi"}, "sendMessage", {}, None))
        self.assertTrue(result)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(s >= 1.5 for s in slept))

    def test_calls_are_spaced(self):
        from unibot.telegram_ui import TelegramSafetyLimiter
        limiter = TelegramSafetyLimiter()

        async def ok():
            return True

        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            for _ in range(5):
                await limiter.process_request(ok, (), {}, "sendMessage", {}, None)
            return loop.time() - start

        self.assertGreaterEqual(asyncio.run(run()), 4 * TelegramSafetyLimiter.MIN_INTERVAL * 0.9)


class InviteTests(unittest.TestCase):
    def test_quota_code_lets_that_many_students_in(self):
        code = db.create_invite_token(created_by=1, label="Group A", expires_days=1, max_uses=3)
        results = [db.redeem_invite_token(code, uid)[0] for uid in (701, 702, 703, 704)]
        self.assertEqual(results, [True, True, True, False])
        token = next(t for t in db.list_invite_tokens() if t["code"] == code)
        self.assertEqual((token["use_count"], token["max_uses"], token["is_used"]), (3, 3, True))

    def test_deep_link_form_and_expiry_and_revoke(self):
        code = db.create_invite_token(created_by=1)
        self.assertTrue(db.redeem_invite_token(code.replace("-", "").lower(), 711)[0])  # inv_uniabc123
        expired = db.create_invite_token(created_by=1, expires_days=-1)
        self.assertEqual(db.redeem_invite_token(expired, 712), (False, "This invite code has expired."))
        revoked = db.create_invite_token(created_by=1, max_uses=5)
        self.assertTrue(db.revoke_invite_token(revoked))
        self.assertFalse(db.redeem_invite_token(revoked, 713)[0])
        self.assertFalse(db.revoke_invite_token("UNI-000-000"))
        with self.assertRaises(ValueError):
            db.create_invite_token(created_by=1, max_uses=0)

    def test_invite_arguments(self):
        from unibot.system import parse_invite_args
        self.assertEqual(parse_invite_args([]), {"action": "create", "uses": 1, "days": 7.0, "label": None})
        self.assertEqual(parse_invite_args(["create", "5", "14", "Econ", "group"]),
                         {"action": "create", "uses": 5, "days": 14.0, "label": "Econ group"})
        self.assertEqual(parse_invite_args(["Ada"])["label"], "Ada")  # the old /invite <label>
        self.assertEqual(parse_invite_args(["list"]), {"action": "list"})
        self.assertEqual(parse_invite_args(["revoke", "UNI-1"]), {"action": "revoke", "code": "UNI-1"})
        for bad in (["create", "0"], ["create", "500"], ["create", "2", "999"], ["revoke"]):
            with self.assertRaises(ValueError):
                parse_invite_args(bad)

    def test_strangers_get_the_invitation_notice(self):
        update = _update(799, text="hello")
        with mock.patch.object(auth, "is_user_allowed", lambda uid: False):
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(auth._authorization_gate(update, None))
        self.assertIn("private invitation", update.effective_message.reply_text.call_args.args[0])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        auth._renewal_failures.clear()
        auth._reauth_prompted_at.clear()

    def tearDown(self):
        # the test DB is shared: leave no one waiting for a sign-in prompt
        for uid in db.users_in_lifecycle(auth.NEEDS_REAUTH) + db.users_in_lifecycle(auth.INACTIVE):
            db.set_lifecycle(uid, auth.ACTIVE)

    def test_failed_renewals_then_a_login(self):
        uid = 801
        db.set_lifecycle(uid, auth.ACTIVE)
        auth._on_login_state(uid, False)
        self.assertEqual(auth.lifecycle_state(uid), auth.ACTIVE)        # one failure can be a network blip
        auth._on_login_state(uid, False)
        self.assertEqual(auth.lifecycle_state(uid), auth.NEEDS_REAUTH)
        auth._on_login_state(uid, True)
        self.assertEqual(auth.lifecycle_state(uid), auth.ACTIVE)
        auth._on_login_state(uid, False)                                # the count started over
        self.assertEqual(auth.lifecycle_state(uid), auth.ACTIVE)

    def test_session_reports_reach_the_lifecycle(self):
        import studip_session
        self.assertIn(auth._on_login_state, studip_session.LOGIN_STATE_LISTENERS)
        uid = 802
        db.set_lifecycle(uid, auth.ACTIVE)
        studip_session._report_login_state(uid, False)
        studip_session._report_login_state(uid, False)
        self.assertEqual(auth.lifecycle_state(uid), auth.NEEDS_REAUTH)
        studip_session._report_login_state(None, True)                  # no user: ignored

    def test_inactive_stays_inactive_until_start(self):
        uid = 803
        db.set_lifecycle(uid, auth.INACTIVE, "stopped with /stop")
        auth._on_login_state(uid, False)
        auth._on_login_state(uid, False)
        self.assertEqual(auth.lifecycle_state(uid), auth.INACTIVE)      # not moved by session noise
        with mock.patch.object(auth, "is_user_allowed", lambda u: True), \
                mock.patch.object(auth, "update_user_activity", lambda *a, **k: None):
            asyncio.run(auth._authorization_gate(_update(uid, text="/help"), None))
            self.assertEqual(auth.lifecycle_state(uid), auth.INACTIVE)
            asyncio.run(auth._authorization_gate(_update(uid, text="/start"), None))
        self.assertEqual(auth.lifecycle_state(uid), auth.ACTIVE)

    def test_background_work_only_for_active_students(self):
        accounts = [mock.MagicMock(user_id=811, signed_in=True), mock.MagicMock(user_id=812, signed_in=True),
                    mock.MagicMock(user_id=813, signed_in=True), mock.MagicMock(user_id=814, signed_in=False)]
        db.set_lifecycle(811, auth.ACTIVE)
        db.set_lifecycle(812, auth.NEEDS_REAUTH)
        db.set_lifecycle(813, auth.INACTIVE)
        with mock.patch.object(auth, "accounts", lambda: accounts):
            self.assertEqual([a.user_id for a in auth.signed_in_accounts()], [811])

    def test_expired_student_is_asked_once_on_interaction(self):
        uid = 821
        db.set_lifecycle(uid, auth.NEEDS_REAUTH)
        prompts = []

        async def prompt(target, user_id):
            prompts.append(user_id)

        with mock.patch.object(auth, "is_user_allowed", lambda u: True), \
                mock.patch.object(auth, "update_user_activity", lambda *a, **k: None), \
                mock.patch.object(auth, "_send_login_prompt", prompt):
            first = _update(uid, text="/today")
            asyncio.run(auth._authorization_gate(first, None))
            asyncio.run(auth._authorization_gate(_update(uid, text="/tasks"), None))
        self.assertEqual(prompts, [uid])
        self.assertIn("session has expired", first.effective_message.reply_text.call_args_list[0].args[0])

    def test_watcher_tells_each_expired_student_once(self):
        from unibot import watcher
        cache = {}
        told = []

        async def notice(bot, uid):
            told.append(uid)

        for uid in db.users_in_lifecycle(auth.NEEDS_REAUTH):
            db.set_lifecycle(uid, auth.ACTIVE)
        db.set_lifecycle(831, auth.NEEDS_REAUTH)
        with mock.patch.object(watcher, "load_general_cache", lambda: dict(cache)), \
                mock.patch.object(watcher, "update_general_cache", lambda d: cache.update(d)), \
                mock.patch.object(watcher, "send_reauth_notice", notice), \
                mock.patch.object(watcher.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(watcher._notify_reauth_needed(None))
            asyncio.run(watcher._notify_reauth_needed(None))
            time.sleep(0.01)
            db.set_lifecycle(831, auth.ACTIVE)
            db.set_lifecycle(831, auth.NEEDS_REAUTH)                     # expired again later
            asyncio.run(watcher._notify_reauth_needed(None))
        self.assertEqual(told, [831, 831])

    def test_stop_and_delete_make_a_student_inactive(self):
        from unibot import privacy
        update = _update(841, text="/stop")
        update.message = update.effective_message
        with mock.patch.object(privacy, "is_user_allowed", lambda u: True):
            asyncio.run(privacy.stop_command(update, None))
        self.assertEqual(auth.lifecycle_state(841), auth.INACTIVE)
        db.set_lifecycle(842, auth.ACTIVE)

        async def logout(user_id=None):
            return None

        with mock.patch.object(privacy, "_perform_logout", logout):
            asyncio.run(privacy.delete_personal_data(842))
        self.assertEqual(auth.lifecycle_state(842), auth.INACTIVE)

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(ValueError):
            db.set_lifecycle(851, "graduated")


if __name__ == "__main__":
    unittest.main()
