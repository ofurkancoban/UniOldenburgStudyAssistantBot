"""Behavioral regressions for membership, delivery and calendar boundaries."""
import asyncio
from contextlib import ExitStack
from datetime import date, datetime
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path.insert(0, str(Path.cwd()))
os.environ['BOT_LOG_FILE'] = '/tmp/studip_bot_tests.log'
from tests.testenv import activate_student, use_temp_db
use_temp_db()
from unibot import auth, course_registry, db, enrollment, menus, morning, schedule, security_events, state, storage, telegram_ui, whatsapp, watcher
from unibot.monitoring_diff import _snapshot_course_files, _diff_course_files
import studip_login_server
from unibot.app import UserUpdateProcessor
from studip_utils import TZ_BERLIN

A, B = 910101, 910102
CID = 'a' * 32
URL = 'https://elearning.uni-oldenburg.de/sendfile.php?file_id=resource1'

class ReauditRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.confirm_course_memberships(A, [CID], float("inf"))
        db.confirm_course_memberships(B, [CID], float("inf"))
        db._connection().execute("DELETE FROM outgoing_actions")
        activate_student(A, {CID: 'Course A'})
        activate_student(B, {CID: 'Course A'})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(state, 'user_courses_map', {A: {CID: 'Course A'}, B: {CID: 'Course A'}}))
        self.stack.enter_context(mock.patch.object(state, 'global_session', None))

    def test_role_change_invalidates_in_flight_user_context(self):
        db.set_user_role(A, 'admin')
        with auth.acting_as(A):
            self.assertTrue(db.private_context_is_current(A))
            db.set_user_role(A, 'user')
            self.assertFalse(db.private_context_is_current(A))

    def test_shared_private_request_guard_requires_matching_live_context(self):
        with mock.patch.object(auth, 'is_user_allowed', return_value=True):
            with auth.acting_as(A):
                self.assertTrue(auth.private_request_is_current(A))
                self.assertFalse(auth.private_request_is_current(B))
            self.assertFalse(auth.private_request_is_current(A))

    def test_security_events_log_only_keyed_user_tags(self):
        with mock.patch.dict(os.environ, {'SECURITY_AUDIT_KEY': 'test-only-audit-key-0123456789abcdef'}), \
                self.assertLogs('security.audit', level='INFO') as captured:
            security_events.log_security_event(
                'admin_role_changed', actor_id=910101, subject_id=910102,
            )
        line = captured.output[0]
        self.assertIn('event=admin_role_changed', line)
        self.assertNotIn('910101', line)
        self.assertNotIn('910102', line)
        self.assertIn('actor=', line)
        self.assertIn('subject=', line)

    async def test_admin_callback_stops_after_role_revocation_during_answer(self):
        from unibot import system

        allowed = {'admin': True}
        answer_entered = asyncio.Event()
        release_answer = asyncio.Event()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=A), data='admin_users',
            answer=mock.AsyncMock(), edit_message_text=mock.AsyncMock(),
        )

        async def pause_answer(*args, **kwargs):
            answer_entered.set()
            await release_answer.wait()

        query.answer.side_effect = pause_answer
        update = SimpleNamespace(callback_query=query)
        with mock.patch.object(system, 'is_user_admin', side_effect=lambda uid: allowed['admin']), \
                mock.patch.object(system, 'is_user_allowed', return_value=True):
            task = asyncio.create_task(system.handle_admin_callbacks(update, SimpleNamespace(user_data={})))
            await answer_entered.wait()
            allowed['admin'] = False
            release_answer.set()
            await task

        query.edit_message_text.assert_not_awaited()

    async def test_broadcast_stops_for_later_recipients_after_sender_revocation(self):
        allowed = {'admin': True}
        sent = []

        class FakeBot:
            async def send_message(self, chat_id, **kwargs):
                sent.append(chat_id)
                allowed['admin'] = False

        with mock.patch.object(telegram_ui, 'broadcast_targets', return_value=[A, B]), \
                mock.patch.object(telegram_ui, '_broadcast_sender_is_admin',
                                  side_effect=lambda uid: allowed['admin']):
            result = await telegram_ui.broadcast(
                FakeBot(), 'notice', parse_mode='text', sender_user_id=999001,
            )

        self.assertEqual(len(sent), 1)
        self.assertTrue(result['sender_revoked'])

    def test_same_file_is_not_new_after_api_html_transition(self):
        f = {'type': 'file', 'name': 'slides.pdf', 'url': URL, 'size': '1 MB', 'modified_iso': '2026-11-04T10:00:00+01:00'}
        api = _snapshot_course_files([{**f, 'id': 'resource1'}])
        html = _snapshot_course_files([dict(f)])
        self.assertEqual(_diff_course_files(html, api), ([], []))
        self.assertEqual(_diff_course_files(api, html), ([], []))

    async def download(self, uid, info):
        q = mock.AsyncMock()
        q.message = mock.AsyncMock()
        q.message.text_html = None
        response = mock.AsyncMock()
        response.status = 200
        response.__aenter__.return_value = response
        session = SimpleNamespace(get=mock.AsyncMock(return_value=response))
        sent = mock.AsyncMock()
        with mock.patch.dict(menus.link_cache, {'audit-shared': info}), \
                mock.patch.object(menus, 'get_user_session', return_value=session), \
                mock.patch.object(menus, 'get_fresh_file_url', mock.AsyncMock(return_value=URL)), \
                mock.patch.object(menus, '_show_typing', mock.AsyncMock()), \
                mock.patch.object(menus, '_start_loading_animation', return_value=(None, None)), \
                mock.patch.object(menus, '_stop_loading_animation', mock.AsyncMock()), \
                mock.patch.object(menus, 'read_response_limited', mock.AsyncMock(return_value=b'%PDF audit')), \
                mock.patch.object(menus, '_send_file_bytes', sent):
            with auth.acting_as(uid):
                await menus._selection_file(None, None, q, ['file', 'audit-shared'], uid)
            return dict(menus.link_cache['audit-shared']), sent.await_count

    async def test_shared_file_refresh_keeps_other_enrolled_students(self):
        updated, sent = await self.download(A, {'shared': True, 'cid': CID, 'name': 'slides.pdf', 'course': 'Course A'})
        self.assertEqual(sent, 1)
        self.assertNotIn('user_id', updated)
        self.assertTrue(storage.link_cache_owned_by(updated, B))
        self.assertTrue(storage.has_course_access(B, CID))

    async def test_nonmember_cannot_download_shared_cached_file(self):
        state.user_courses_map[B] = {}
        db.get_user_scope(B).sync_courses({})
        self.assertFalse(storage.has_course_access(B, CID))
        _, sent = await self.download(B, {'shared': True, 'cid': CID, 'name': 'slides.pdf', 'course': 'Course A', 'url': URL})
        self.assertEqual(sent, 0)

    async def test_successful_withdrawal_revokes_course_authorization(self):
        q = mock.AsyncMock()
        with mock.patch.object(enrollment, 'login_studip', mock.AsyncMock(return_value=object())), \
                mock.patch.object(enrollment, 'decline_course', mock.AsyncMock(return_value=(True, 'done'))), \
                mock.patch.object(enrollment, '_show_typing', mock.AsyncMock()), \
                mock.patch.object(enrollment, '_start_loading_animation', return_value=(None, None)), \
                mock.patch.object(enrollment, '_stop_loading_animation', mock.AsyncMock()), \
                mock.patch.object(enrollment, '_delete_status_message', mock.AsyncMock()):
            with auth.acting_as(A):
                await enrollment._submit_course_action(q, A, CID, 'deenroll')
        self.assertFalse(storage.has_course_access(A, CID))
        self.assertNotIn(A, telegram_ui.broadcast_targets(CID))

    async def test_withdrawn_session_is_removed_from_course_scanner_candidates(self):
        session_a = SimpleNamespace(user_id=A)
        session_b = SimpleNamespace(user_id=B)
        course_registry.set_candidates(CID, [(A, session_a), (B, session_b)])
        storage.withdraw_course_access(A, CID)
        with mock.patch.object(course_registry, 'has_stored_login', return_value=True):
            self.assertIs(course_registry.pick_scanner(CID), session_b)

    async def test_scanner_discards_response_if_membership_is_revoked_in_flight(self):
        session_a = SimpleNamespace(user_id=A)
        session_b = SimpleNamespace(user_id=B)
        course_registry.set_candidates(CID, [(A, session_a), (B, session_b)])
        seen = []

        async def fetch(session):
            seen.append(session)
            if session is session_a:
                storage.withdraw_course_access(A, CID)
                return 'stale course response'
            return 'authorized response'

        with mock.patch.object(course_registry, 'has_stored_login', return_value=True):
            result, used = await course_registry.scan_with_failover(CID, session_a, fetch)
        self.assertEqual(result, 'authorized response')
        self.assertIs(used, session_b)
        self.assertEqual(seen, [session_a, session_b])

    async def test_empty_calendar_has_free_day_study_reminders(self):
        fixed = datetime(2026, 11, 4, 9, 2, tzinfo=TZ_BERLIN)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return fixed
        bot = mock.AsyncMock()
        self.assertGreater(len(schedule.find_study_windows_for_day([], fixed.date(), user_id=A)), 0)
        with mock.patch.object(schedule, 'datetime', Clock), \
                mock.patch.object(schedule, 'get_calendar_events', mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, 'get_user_session', return_value=object()):
            await schedule.check_calendar_reminders(bot, A, silent=True)
        bot.send_message.assert_awaited_once()

    async def test_failed_extra_course_calendar_is_reported_as_incomplete(self):
        async def get_all(path, *args, **kwargs):
            if path.startswith('/courses/'):
                raise RuntimeError('simulated course calendar outage')
            return [], []
        with mock.patch.object(schedule, 'studip_api_user_id', mock.AsyncMock(return_value='own')), \
                mock.patch.object(schedule, 'fetch_user_schedule_api', mock.AsyncMock(return_value=[])), \
                mock.patch.object(schedule, 'student_courses', return_value={CID: 'Single appointment course'}), \
                mock.patch.object(schedule, 'studip_api_get_all', side_effect=get_all), \
                mock.patch.dict(schedule._COURSE_EVENTS_CACHE, {}, clear=True):
            with auth.acting_as(A):
                result = await morning._fetch_today_events(object(), date(2026, 11, 4), user_id=A)
        self.assertEqual(result, ([], [], False))

    async def test_whatsapp_send_stops_after_admin_revocation(self):
        allowed = [True]
        async def answer(*args, **kwargs): allowed[0] = False
        q = SimpleNamespace(from_user=SimpleNamespace(id=A), answer=mock.AsyncMock(side_effect=answer),
                edit_message_reply_markup=mock.AsyncMock(),
                message=SimpleNamespace(text='Private announcement', caption=None, chat_id=A, message_id=1))
        send = mock.AsyncMock(return_value=(True, 'sent'))
        with mock.patch.object(whatsapp, 'is_user_admin', side_effect=lambda uid: allowed[0]), \
                mock.patch.object(whatsapp, '_send_text_to_whatsapp', send):
            await whatsapp.forward_to_whatsapp(SimpleNamespace(callback_query=q), SimpleNamespace(bot=mock.AsyncMock()))
        self.assertFalse(allowed[0])
        send.assert_not_awaited()

    async def test_successful_whatsapp_forward_is_deduplicated_across_restart(self):
        response = mock.AsyncMock(status=200)
        post_context = mock.AsyncMock()
        post_context.__aenter__.return_value = response
        session = SimpleNamespace(post=mock.MagicMock(return_value=post_context))
        context = mock.AsyncMock()
        context.__aenter__.return_value = session
        with mock.patch.object(whatsapp, 'get_wa_group_id', return_value='audit@g.us'), \
                mock.patch.object(whatsapp, 'shared_http', return_value=context), \
                mock.patch.object(whatsapp, 'is_user_admin', return_value=True), auth.acting_as(A):
            first = await whatsapp._send_text_to_whatsapp('same text', dedupe_key=(A, 1))
            db.close()
            second = await whatsapp._send_text_to_whatsapp('same text', dedupe_key=(A, 1))
        self.assertTrue(first[0] and second[0])
        self.assertEqual(session.post.call_count, 1)

    async def test_broadcast_excludes_recipient_blocked_after_audience_snapshot(self):
        async def send(**kwargs):
            if kwargs['chat_id'] == A:
                db.set_user_active(B, False)
        bot = SimpleNamespace(send_message=mock.AsyncMock(side_effect=send))
        with mock.patch.object(telegram_ui, 'broadcast_targets', return_value=[A, B]), \
                mock.patch.object(telegram_ui.asyncio, 'sleep', mock.AsyncMock()):
            await telegram_ui.broadcast(bot, 'Course content', cid=CID)
        self.assertFalse(db.get_user(B)['is_active'])
        self.assertEqual([c.kwargs['chat_id'] for c in bot.send_message.await_args_list], [A])

    async def test_explicit_broadcast_audience_must_still_be_authorized(self):
        self.assertEqual(telegram_ui.broadcast_targets(members=[A, 919199]), {A})

    async def test_calendar_error_is_reported_to_deadline_health(self):
        token = watcher._cycle_failures.set([])
        try:
            with mock.patch.object(watcher, '_get_active_user_ids', return_value=[A]), \
                    mock.patch.object(watcher, 'get_user_session', return_value=object()), \
                    mock.patch.object(watcher, 'has_stored_login', return_value=True), \
                    mock.patch.object(schedule, 'get_user_session', return_value=object()), \
                    mock.patch.object(schedule, 'get_calendar_events', mock.AsyncMock(side_effect=RuntimeError('calendar down'))), \
                    self.assertLogs(level='ERROR') as logs:
                await watcher._guarded('Calendar reminder check', watcher._send_calendar_reminders, mock.AsyncMock())
            self.assertTrue(any('Error in check_calendar_reminders' in line for line in logs.output))
            self.assertTrue(watcher._cycle_failures.get())
        finally:
            watcher._cycle_failures.reset(token)

    async def test_updates_are_serial_per_user_but_parallel_between_users(self):
        processor = UserUpdateProcessor()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        other_started = asyncio.Event()
        order = []

        async def first():
            order.append('first-start')
            first_started.set()
            await release_first.wait()
            order.append('first-end')

        async def second():
            order.append('second-start')
            second_started.set()

        async def other():
            other_started.set()

        task1 = asyncio.create_task(processor.do_process_update(SimpleNamespace(effective_user=SimpleNamespace(id=A)), first()))
        await first_started.wait()
        task2 = asyncio.create_task(processor.do_process_update(SimpleNamespace(effective_user=SimpleNamespace(id=A)), second()))
        task3 = asyncio.create_task(processor.do_process_update(SimpleNamespace(effective_user=SimpleNamespace(id=B)), other()))
        await asyncio.wait_for(other_started.wait(), 1)
        self.assertFalse(second_started.is_set())
        release_first.set()
        await asyncio.gather(task1, task2, task3)
        self.assertLess(order.index('first-end'), order.index('second-start'))

    async def test_whatsapp_uncertain_post_is_not_retried(self):
        response = mock.AsyncMock(status=500)
        response.json.return_value = {'error': 'simulated failure'}
        post_context = mock.AsyncMock()
        post_context.__aenter__.return_value = response
        session = SimpleNamespace(post=mock.MagicMock(return_value=post_context))
        context = mock.AsyncMock()
        context.__aenter__.return_value = session
        with mock.patch.object(whatsapp, 'get_wa_group_id', return_value='uncertain@g.us'), \
                mock.patch.object(whatsapp, 'shared_http', return_value=context), \
                mock.patch.object(whatsapp, 'is_user_admin', return_value=True), auth.acting_as(A):
            first = await whatsapp._send_text_to_whatsapp('uncertain', dedupe_key=(A, 99))
            second = await whatsapp._send_text_to_whatsapp('uncertain', dedupe_key=(A, 99))
        self.assertFalse(first[0] or second[0])
        self.assertEqual(session.post.call_count, 1)

    async def test_proxy_binary_response_streams_without_buffering(self):
        server = studip_login_server.LoginServer(bot_token='1:x')
        request = SimpleNamespace(method='GET', transport=None)
        chunks = [b'one', b'two']
        content = SimpleNamespace()
        async def iterate(size):
            for chunk in chunks:
                yield chunk
        content.iter_chunked = iterate
        upstream = SimpleNamespace(status=200, headers={'Content-Type': 'application/octet-stream'}, content=content)
        output = []
        prepared = mock.AsyncMock()
        with mock.patch.object(server, '_proxy_session_is_current', return_value=True), \
                mock.patch.object(studip_login_server.web, 'StreamResponse') as stream_factory:
            response = SimpleNamespace(prepare=prepared, write=mock.AsyncMock(side_effect=lambda c: output.append(c)),
                    write_eof=mock.AsyncMock(), force_close=mock.Mock())
            stream_factory.return_value = response
            result = await server._stream_proxy_response(request, A, upstream)
        self.assertIs(result, response)
        self.assertEqual(output, chunks)
        prepared.assert_awaited_once_with(request)
        response.write_eof.assert_awaited_once()

    async def test_html_proxy_body_has_a_hard_size_limit(self):
        response = SimpleNamespace(content_length=None)
        async def oversized(size):
            yield b'x' * (studip_login_server.PROXY_HTML_MAX_BYTES + 1)
        response.content = SimpleNamespace(iter_chunked=oversized)
        with self.assertRaises(studip_login_server.web.HTTPBadGateway):
            await studip_login_server._read_proxy_html(response)

    def test_inline_html_download_is_forced_to_non_executable_attachment(self):
        headers = studip_login_server._proxy_download_headers({
            'Content-Type': 'text/html',
            'Content-Disposition': 'inline; filename="payload.html"',
            'Content-Security-Policy': "default-src 'self'",
        })
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertEqual(headers['Content-Disposition'], 'attachment')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertIn('frame-ancestors', headers['Content-Security-Policy'])
        self.assertNotIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertTrue(studip_login_server._is_proxy_file_download(
            '/sendfile.php', 200, {'Content-Type': 'text/html'}
        ))
        self.assertFalse(studip_login_server._is_proxy_file_download(
            '/sendfile.php', 302, {'Location': '/dispatch.php'}
        ))

if __name__ == '__main__': unittest.main(verbosity=2)
