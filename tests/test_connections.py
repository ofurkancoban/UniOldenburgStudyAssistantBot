"""Connection layer: worker routing, per-host cookies, throttling, per-user StuMS."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

# never touch the live log or database (set before unibot is imported)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
os.environ.setdefault("BOT_DB_FILE", os.path.join(tempfile.mkdtemp(prefix="unibot-tests-"), "bot_data.sqlite3"))

from aiohttp import web
from yarl import URL

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import studip_http  # noqa: E402
import studip_session  # noqa: E402

STUMS = "stums.uni-oldenburg.de"
LOGIN = "login.uni-oldenburg.de"


class FakeWorker:
    """Speaks the worker protocol (/<host>/<path>, X-Upstream-Set-Cookie) and
    plays StuMS + the NetIQ IdP behind it. Both hosts use a JSESSIONID, which
    is exactly what collided under the worker's hostname before."""

    def __init__(self, legacy=False, refuse=(), platform_error=None, upstream_status=None):
        self.refuse = set(refuse)  # hosts an outdated worker.js does not allow
        self.platform_error = platform_error  # Cloudflare's own error page (no proxy headers)
        self.upstream_status = upstream_status  # a genuine university error, marked by the worker
        self.legacy = legacy  # old worker: rewritten Set-Cookie, Location pointing at the worker
        self.seen = []
        self.base = ""

    def _cookie(self, resp, value):
        if self.legacy:
            stripped = ";".join(p for p in value.split(";") if not p.strip().lower().startswith("domain="))
            resp.headers.add("Set-Cookie", stripped)
        else:
            resp.headers.add("X-Upstream-Set-Cookie", value)

    def _location(self, url):
        if self.legacy:
            u = URL(url)
            return f"{self.base}/{u.host}{u.path_qs}"
        return url

    async def handle(self, request):
        host, _, rest = request.path.lstrip("/").partition("/")
        path = "/" + rest
        cookie = request.headers.get("Cookie", "")
        self.seen.append((host, path, cookie, request.headers.get("X-Proxy-Secret")))
        if host in self.refuse:
            return web.Response(status=403, text=f"Forbidden: host '{host}' is not allowed")
        if self.platform_error:
            return web.Response(status=self.platform_error, text="error code: 1027")
        if self.upstream_status:
            return web.Response(status=self.upstream_status, text="busy", headers={"X-Proxy-Upstream": "1"})
        if host == STUMS and path == "/qisserver/start":
            if "JSESSIONID=stums-auth" in cookie:
                return web.Response(text="Willkommen <a>Abmelden</a>")
            resp = web.Response(status=302)
            resp.headers["Location"] = self._location(f"https://{LOGIN}/nidp/auth?client=stums")
            self._cookie(resp, "JSESSIONID=stums-anon; Path=/qisserver; Secure; HttpOnly")
            return resp
        if host == LOGIN and path == "/nidp/auth":
            resp = web.Response(status=302)
            resp.headers["Location"] = self._location(f"https://{STUMS}/qisserver/callback?code=abc")
            self._cookie(resp, "JSESSIONID=idp-session; Path=/nidp; Secure")
            self._cookie(resp, "SSOTOKEN=warm; Domain=.uni-oldenburg.de; Path=/")
            return resp
        if host == STUMS and path == "/qisserver/callback":
            resp = web.Response(status=302)
            resp.headers["Location"] = "/qisserver/start"
            self._cookie(resp, "JSESSIONID=stums-auth; Path=/qisserver; Secure; HttpOnly")
            return resp
        if host == STUMS and path == "/qisserver/form":
            return web.Response(text="<form action='/qisserver/submit'></form>")
        return web.Response(status=404, text=f"unknown {host}{path}")


def _run_with_worker(worker, scenario):
    async def main():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", worker.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        worker.base = f"http://127.0.0.1:{port}"
        env = {"CLOUDFLARE_WORKER_URL": worker.base, "CLOUDFLARE_WORKER_SECRET": "s3cret"}
        sess = studip_session.StudIPSession("", "", "", cookie_file=None)
        try:
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(studip_session.studip_oauth, "oauth_enabled", return_value=False), \
                    mock.patch.object(studip_http, "THROTTLE", studip_http.HostThrottle(min_interval=0)), \
                    mock.patch.object(studip_session, "THROTTLE", studip_http.HostThrottle(min_interval=0)):
                return await scenario(sess)
        finally:
            await sess.close()
            await runner.cleanup()
    return asyncio.run(main())


class WorkerRoutingTests(unittest.TestCase):
    def _sso_roundtrip(self, legacy):
        worker = FakeWorker(legacy=legacy)

        async def scenario(sess):
            async with await sess.get(f"https://{STUMS}/qisserver/start") as r:
                body = await r.text()
                final = str(r.url)
            jar_hosts = {c["domain"] for c in sess.session.cookie_jar}
            stums_cookies = sess.session.cookie_jar.filter_cookies(URL(f"https://{STUMS}/qisserver/x"))
            return body, final, jar_hosts, {k: m.value for k, m in stums_cookies.items()}

        body, final, jar_hosts, stums_cookies = _run_with_worker(worker, scenario)
        self.assertIn("Abmelden", body)
        # the response reports the university URL, never the worker's
        self.assertEqual(final, f"https://{STUMS}/qisserver/start")
        self.assertNotIn("127.0.0.1", jar_hosts)
        self.assertEqual(stums_cookies["JSESSIONID"], "stums-auth")
        hops = [(h, p) for h, p, _c, _s in worker.seen]
        self.assertEqual(hops, [(STUMS, "/qisserver/start"), (LOGIN, "/nidp/auth"),
                                (STUMS, "/qisserver/callback"), (STUMS, "/qisserver/start")])
        idp_cookie = worker.seen[1][2]
        self.assertNotIn("stums-anon", idp_cookie)  # StuMS' JSESSIONID never reaches the IdP
        final_cookie = worker.seen[3][2]
        self.assertIn("JSESSIONID=stums-auth", final_cookie)
        self.assertNotIn("idp-session", final_cookie)  # nor the IdP's JSESSIONID StuMS
        self.assertTrue(all(secret == "s3cret" for *_x, secret in worker.seen))
        return final_cookie

    def test_cookies_stay_per_host_through_the_worker(self):
        final_cookie = self._sso_roundtrip(legacy=False)
        self.assertIn("SSOTOKEN=warm", final_cookie)  # Domain=.uni-oldenburg.de cookie shared as it should

    def test_old_worker_deployment_still_works(self):
        self._sso_roundtrip(legacy=True)

    def test_relative_form_actions_resolve_against_the_real_host(self):
        worker = FakeWorker()

        async def scenario(sess):
            async with await sess.get(f"https://{STUMS}/qisserver/form") as r:
                return str(r.url)

        page_url = _run_with_worker(worker, scenario)
        from urllib.parse import urljoin
        self.assertEqual(urljoin(page_url, "/qisserver/submit"), f"https://{STUMS}/qisserver/submit")


class WorkerRefusalTests(unittest.TestCase):
    def test_refused_host_falls_back_to_direct(self):
        worker = FakeWorker(refuse={LOGIN})
        direct_calls = []

        class DirectResp:
            status = 200
            headers = __import__("multidict").CIMultiDictProxy(__import__("multidict").CIMultiDict())

            async def text(self):
                return "direct ok"

            def release(self):
                pass

        async def scenario(sess):
            studip_session._WORKER_REFUSED.clear()

            async def direct_request(method, url, **kwargs):
                direct_calls.append(url)
                return DirectResp()

            await sess.ensure_session()
            sess.session.request = direct_request
            async with await sess.get(f"https://{LOGIN}/nidp/app") as r:
                first = await r.text()
            async with await sess.get(f"https://{LOGIN}/nidp/app") as r:
                await r.text()
            return first

        try:
            first = _run_with_worker(worker, scenario)
        finally:
            studip_session._WORKER_REFUSED.clear()
        self.assertEqual(first, "direct ok")
        self.assertEqual(direct_calls, [f"https://{LOGIN}/nidp/app"] * 2)
        # the worker was asked once; the second request went straight out
        self.assertEqual([h for h, *_ in worker.seen], [LOGIN])


class _DirectResp:
    status = 200

    def __init__(self):
        import multidict
        self.headers = multidict.CIMultiDictProxy(multidict.CIMultiDict())

    async def text(self):
        return "direct ok"

    def release(self):
        pass


class WorkerFailoverTests(unittest.TestCase):
    """While the worker itself is down, requests go directly to the university."""

    def setUp(self):
        studip_session._WORKER_HEALTH.update(failures=0, down_until=0.0)
        studip_session._WORKER_REFUSED.clear()
        self.addCleanup(studip_session._WORKER_HEALTH.update, failures=0, down_until=0.0)

    def _run(self, worker, method="GET"):
        direct_calls = []

        async def scenario(sess):
            async def direct_request(m, url, **kwargs):
                direct_calls.append((m, url))
                return _DirectResp()

            await sess.ensure_session()
            sess.session.request = direct_request
            results = []
            for _ in range(2):
                try:
                    async with await sess.request(method, f"https://{LOGIN}/nidp/app") as r:
                        results.append((r.status, await r.text()))
                except Exception as exc:  # noqa: BLE001 - the POST case expects a failure
                    results.append(exc.__class__.__name__)
            return results

        return _run_with_worker(worker, scenario), direct_calls

    def test_cloudflare_platform_error_switches_to_direct(self):
        worker = FakeWorker(platform_error=429)  # e.g. the free plan's daily limit
        results, direct_calls = self._run(worker)
        self.assertEqual(results, [(200, "direct ok")] * 2)
        self.assertEqual(len(direct_calls), 2)
        self.assertEqual(len(worker.seen), 1)  # the second request skipped the worker
        self.assertGreater(studip_session._WORKER_HEALTH["down_until"], time.time() + 60)

    def test_university_errors_through_the_worker_are_not_a_failover(self):
        worker = FakeWorker(upstream_status=503)
        results, direct_calls = self._run(worker)
        self.assertEqual(direct_calls, [])
        self.assertEqual(studip_session._WORKER_HEALTH["failures"], 0)

    def test_missing_worker_session_goes_direct(self):
        # e.g. CLOUDFLARE_WORKER_URL set after the session was opened
        worker = FakeWorker()
        direct_calls = []

        async def scenario(sess):
            async def direct_request(m, url, **kwargs):
                direct_calls.append((m, url))
                return _DirectResp()

            await sess.ensure_session()
            sess.session.request = direct_request
            if sess._proxy_http is not None:
                await sess._proxy_http.close()
            sess._proxy_http = None

            async def keep_closed():
                return sess.session

            sess.ensure_session = keep_closed  # it would reopen the worker session
            async with await sess.request("GET", f"https://{LOGIN}/nidp/app") as r:
                return r.status, await r.text()

        self.assertEqual(_run_with_worker(worker, scenario), (200, "direct ok"))
        self.assertEqual(len(direct_calls), 1)
        self.assertEqual(worker.seen, [])

    def test_post_is_never_resent_after_a_worker_error(self):
        worker = FakeWorker(platform_error=503)
        results, direct_calls = self._run(worker, method="POST")
        # the failed POST is not resent (it may have been delivered) ...
        self.assertEqual(results[0], "ClientConnectionError")
        # ... only the next, new POST goes directly while the worker is down
        self.assertEqual(results[1], (200, "direct ok"))
        self.assertEqual(direct_calls, [("POST", f"https://{LOGIN}/nidp/app")])
        self.assertEqual(len(worker.seen), 1)

    def test_unreachable_worker_switches_to_direct_and_recovers(self):
        async def scenario():
            env = {"CLOUDFLARE_WORKER_URL": "http://127.0.0.1:9", "CLOUDFLARE_WORKER_SECRET": "x"}
            sess = studip_session.StudIPSession("", "", "", cookie_file=None)
            calls = []

            async def direct_request(m, url, **kwargs):
                calls.append(url)
                return _DirectResp()

            try:
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(studip_session.studip_oauth, "oauth_enabled", return_value=False), \
                        mock.patch.object(studip_session, "THROTTLE", studip_http.HostThrottle(min_interval=0)):
                    await sess.ensure_session()
                    sess.session.request = direct_request
                    async with await sess.get(f"https://{LOGIN}/nidp/app") as r:
                        body = await r.text()
                    down = studip_session._WORKER_HEALTH["down_until"] > time.time()
                    # once the pause is over the worker is tried again
                    studip_session._WORKER_HEALTH["down_until"] = 0.0
                    self.assertTrue(sess._via_worker(LOGIN))
                    return body, down, calls
            finally:
                await sess.close()

        body, down, calls = asyncio.run(scenario())
        self.assertEqual(body, "direct ok")
        self.assertTrue(down)
        self.assertEqual(calls, [f"https://{LOGIN}/nidp/app"])


class UnrouteTests(unittest.TestCase):
    def test_trailing_slash_and_query_survive(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": "https://w.example.dev"}):
            from studip_utils import unroute_proxy_url
            self.assertEqual(
                unroute_proxy_url("https://w.example.dev/auth.uni-oldenburg.de/osp/a/TOP/auth/app/?x=1"),
                "https://auth.uni-oldenburg.de/osp/a/TOP/auth/app/?x=1",
            )


class ThrottleTests(unittest.TestCase):
    def test_429_pauses_the_host_and_fails_fast(self):
        t = studip_http.HostThrottle(min_interval=0)
        t.report(STUMS, 429, retry_after=120)
        self.assertGreater(t.cooldown_remaining(STUMS), 100)

        async def go():
            async with t.slot(STUMS):
                pass

        with self.assertRaises(studip_http.UniHostCooldown):
            asyncio.run(go())
        # other hosts are unaffected
        async def other():
            async with t.slot(LOGIN):
                return True
        self.assertTrue(asyncio.run(other()))

    def test_repeated_failures_trigger_a_cooldown(self):
        t = studip_http.HostThrottle(min_interval=0)
        for _ in range(t.FAILURES_BEFORE_COOLDOWN - 1):
            t.report(STUMS, failed=True)
        self.assertEqual(t.cooldown_remaining(STUMS), 0.0)
        t.report(STUMS, failed=True)
        self.assertGreater(t.cooldown_remaining(STUMS), 0.0)
        t2 = studip_http.HostThrottle(min_interval=0)
        t2.report(STUMS, failed=True)
        t2.report(STUMS, 200)
        self.assertEqual(t2._failures[STUMS], 0)

    def test_background_requests_are_spaced_interactive_ones_not(self):
        t = studip_http.HostThrottle(min_interval=0.05)

        async def burst(n):
            start = time.monotonic()
            for _ in range(n):
                async with t.slot(STUMS):
                    pass
            return time.monotonic() - start

        self.assertGreater(asyncio.run(burst(4)), 0.12)

        async def interactive(n):
            with studip_http.interactive_requests():
                return await burst(n)
        self.assertLess(asyncio.run(interactive(4)), 0.05)

    def test_non_university_hosts_are_not_throttled(self):
        t = studip_http.HostThrottle(min_interval=10)
        t.report("api.telegram.org", 429, retry_after=500)

        async def go():
            async with t.slot("api.telegram.org"):
                return True
        self.assertTrue(asyncio.run(go()))

    def test_retry_after_parsing(self):
        self.assertEqual(studip_http.parse_retry_after("30"), 30.0)
        self.assertIsNone(studip_http.parse_retry_after("soon"))
        self.assertIsNone(studip_http.parse_retry_after(None))


class CookiePersistenceTests(unittest.TestCase):
    def test_domain_cookies_keep_their_scope_after_restore(self):
        async def go():
            sess = studip_session.StudIPSession("", "", "", cookie_file=None)
            await sess.ensure_session()
            try:
                sess._restore_cookie("SSOTOKEN", "v", "uni-oldenburg.de", "/", host_only=False)
                sess._restore_cookie("JSESSIONID", "s", STUMS, "/qisserver", host_only=True)
                jar = sess.session.cookie_jar
                stums = dict(jar.filter_cookies(URL(f"https://{STUMS}/qisserver/a")))
                login = dict(jar.filter_cookies(URL(f"https://{LOGIN}/nidp")))
                root = dict(jar.filter_cookies(URL(f"https://{STUMS}/")))
                return set(stums), set(login), set(root)
            finally:
                await sess.close()

        stums, login, root = asyncio.run(go())
        self.assertEqual(stums, {"SSOTOKEN", "JSESSIONID"})
        self.assertEqual(login, {"SSOTOKEN"})
        self.assertEqual(root, {"SSOTOKEN"})  # path-scoped cookie stays on its path


class PerUserStumsTests(unittest.TestCase):
    def test_exam_cache_is_per_user(self):
        import tempfile
        import exam_reminder as er
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, "exam_reminder_cache.json")
            with mock.patch.object(er, "EXAM_CACHE_PATH", legacy):
                er.save_exam_cache({"known_grades": {"A": 1}})
                er.adopt_legacy_exam_cache(11)  # the admin takes over the old cache
                with er.exam_cache_user(11):
                    self.assertEqual(er.load_exam_cache()["known_grades"], {"A": 1})
                with er.exam_cache_user(22):
                    self.assertEqual(er.load_exam_cache()["known_grades"], {})
                    er.save_exam_cache({"known_grades": {"B": 2}})
                self.assertEqual(er.load_exam_cache(user_id=22)["known_grades"], {"B": 2})
                self.assertEqual(er.load_exam_cache(user_id=11)["known_grades"], {"A": 1})

    def test_acting_user_selects_the_session(self):
        from unibot import auth, state
        with mock.patch.object(state, "user_sessions", {}), \
                mock.patch.object(state, "global_session", None), \
                mock.patch.object(auth, "is_user_admin", return_value=False):
            with auth.acting_as(555):
                sess = auth.get_user_session()
            self.assertEqual(sess.user_id, 555)
            bot_sess = auth.get_user_session()
            self.assertIsNone(bot_sess.user_id)
            self.assertIsNot(bot_sess, sess)

    def test_stums_alerts_go_only_to_their_owner(self):
        from tests.test_refactor import _import
        watcher = _import("watcher")
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        asyncio.run(watcher._user_sender(42)(bot, "grade", parse_mode="HTML"))
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 42)


if __name__ == "__main__":
    unittest.main()


class LoginPopupTests(unittest.TestCase):
    """The success popup closes by itself after one second and on a tap."""

    def test_popup_closes_after_one_second_without_waiting(self):
        import studip_login_server as ls
        snippet = ls._CONNECTED_BANNER_SNIPPET
        self.assertIn("setTimeout(function(){ _botCloseApp(0); }, 1000)", snippet)
        self.assertNotIn("_bot_chain", snippet)
        self.assertIn("Telegram.WebApp.close()", snippet)


class WatcherSessionTests(unittest.TestCase):
    """Admins never share a login, and the watcher survives an admin's logout."""

    def setUp(self):
        from unibot import auth, state
        self.auth, self.state = auth, state
        self.saved = (dict(state.user_sessions), state.global_session)
        state.user_sessions.clear()
        state.global_session = None
        self.patches = [
            mock.patch.object(auth, "is_user_admin", lambda uid: uid in (1, 2)),
            mock.patch.object(auth, "PRIMARY_ADMIN_ID", 1),
            mock.patch.object(auth, "ALLOWED_USER_IDS", {1, 2}),
            mock.patch.object(auth.db, "get_all_users", lambda: []),
            mock.patch.object(auth.db, "load_user_credential", lambda uid, kind: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.state.user_sessions.clear()
        self.state.user_sessions.update(self.saved[0])
        self.state.global_session = self.saved[1]

    def test_admin_session_never_uses_the_shared_legacy_file(self):
        with mock.patch("os.path.exists", lambda path: path == "studip_cookies.json"):
            self.state.global_session = object.__new__(studip_session.StudIPSession)
            self.state.global_session.user_id = 9  # already bound elsewhere
            sess = self.auth.get_user_session(2)
        self.assertEqual(sess.cookie_file, "studip_cookies_2.json")

    def test_watcher_moves_to_another_signed_in_admin(self):
        one, two = self.auth.get_user_session(1), self.auth.get_user_session(2)
        self.assertIs(self.auth.pick_watcher_session(), one)
        self.assertIs(self.auth.pick_watcher_session(exclude=1), two)
        two.explicitly_logged_out = True
        fallback = self.auth.pick_watcher_session(exclude=1)
        self.assertIsNone(fallback.user_id)
        self.assertIsNone(fallback.cookie_file)


class ExamOpenDedupTests(unittest.TestCase):
    """A registration that vanishes from one check is not re-announced."""

    def test_brief_absence_does_not_reannounce(self):
        import exam_reminder as er
        exam = {"code": "inf007_P", "start_date": None, "end_date": None, "unit_id": "u",
                "title": "Course", "raw": "window"}
        cache, sent, results = {}, [], [[exam], [], [exam]]

        async def fake_open(session, failed_codes=None):
            return results.pop(0)

        async def send(bot, text, reply_markup=None):
            sent.append(text)

        with mock.patch.object(er, "get_open_exam_registrations", fake_open), \
                mock.patch.object(er, "load_exam_cache", lambda *a, **k: cache), \
                mock.patch.object(er, "save_exam_cache", lambda data, *a, **k: cache.update(data)):
            for _ in range(3):
                asyncio.run(er.check_exam_reminders(object(), None, send, {}))
        self.assertEqual(len(sent), 1)


class ExamWindowParsingTests(unittest.TestCase):
    def test_window_dates_in_every_layout(self):
        import exam_reminder as er
        for body in (
            "Examination period: 1\nEnrollment feasible from 10/19/26, 12:00 AM until 11/20/26, 11:59 PM",
            "Examination period: 1\nEnrollment feasible from\n10/19/26, 12:00 AM\nuntil\n11/20/26, 11:59 PM",
            "Examination period: 1\nEnrollment possible from 19.10.2026, 00:00 until 20.11.2026, 23:59",
        ):
            (period,) = er._parse_examination_periods(body, now=er.datetime(2026, 11, 8, 12, 0))
            self.assertTrue(period["is_open"])
            self.assertEqual((period["start_date"], period["end_date"]),
                             ("2026-10-19T00:00:00", "2026-11-20T23:59:00"))

    def test_old_undated_key_is_not_reannounced(self):
        import exam_reminder as er
        exam = {"code": "inf007_P", "start_date": "2026-10-19T00:00:00", "end_date": "2026-11-20T23:59:00",
                "unit_id": "u", "title": "Course", "raw": "window"}
        cache = {"notified_open": {"inf007_P:None:None": "2026-11-08T20:21:09"}}
        sent = []

        async def fake_open(session, failed_codes=None):
            return [exam]

        async def send(bot, text, reply_markup=None):
            sent.append(text)

        with mock.patch.object(er, "get_open_exam_registrations", fake_open), \
                mock.patch.object(er, "load_exam_cache", lambda *a, **k: cache), \
                mock.patch.object(er, "save_exam_cache", lambda data, *a, **k: cache.update(data)), \
                mock.patch.object(er, "berlin_now", lambda: er.datetime(2026, 11, 8, 22, 0)):
            asyncio.run(er.check_exam_reminders(object(), None, send, {}))
        self.assertFalse([t for t in sent if "REGISTRATION OPEN" in t])
        self.assertIn("inf007_P:2026-10-19T00:00:00:2026-11-20T23:59:00", cache["notified_open"])


class ResourceHygieneTests(unittest.TestCase):
    def test_one_console_and_one_file_handler(self):
        import logging
        from logging.handlers import RotatingFileHandler
        import unibot.config  # noqa: F401 - configures logging on import
        handlers = logging.getLogger().handlers
        self.assertEqual(sum(isinstance(h, RotatingFileHandler) for h in handlers), 1)
        self.assertEqual(sum(type(h) is logging.StreamHandler for h in handlers), 1)

    def test_logout_and_shutdown_close_connections(self):
        from unibot import auth, state

        class Sess:
            def __init__(self):
                self.closed = 0
                self.explicitly_logged_out = False

            async def logout(self):
                self.explicitly_logged_out = True

            async def close(self):
                self.closed += 1

        saved = (dict(state.user_sessions), state.global_session)
        a, b, watcher = Sess(), Sess(), Sess()
        state.user_sessions.clear()
        state.user_sessions.update({1: a, 2: b})
        state.global_session = watcher
        try:
            with mock.patch.object(auth.db, "delete_user_credentials", lambda uid: None), \
                    mock.patch.object(auth.db, "update_user_studip", lambda uid, name: None), \
                    mock.patch.object(auth.studip_oauth, "oauth_enabled", return_value=False):
                asyncio.run(auth._perform_logout(user_id=1))
                self.assertEqual(a.closed, 1)
                asyncio.run(auth.close_all_sessions())
            self.assertEqual((b.closed, watcher.closed), (1, 1))
        finally:
            state.user_sessions.clear()
            state.user_sessions.update(saved[0])
            state.global_session = saved[1]


class DiagnosticsTests(unittest.TestCase):
    def test_counters_and_report(self):
        from unibot import diagnostics, state
        throttle = studip_http.HostThrottle(min_interval=0)
        for status in (200, 200, 429, 502):
            throttle.report(STUMS, status)
        throttle.report(STUMS, failed=True)
        self.assertEqual(throttle.stats[STUMS], {"requests": 5, "ok": 2, "client_errors": 0, "throttled": 1,
                                                 "server_errors": 1, "network_errors": 1})
        sess = studip_session.StudIPSession("", "", "", cookie_file=None, user_id=7)
        sess.stums.verified_at = time.time() - 120
        saved = (dict(state.user_sessions), state.global_session)
        state.user_sessions.clear()
        state.user_sessions[7] = sess
        state.global_session = sess
        try:
            with mock.patch.object(diagnostics, "THROTTLE", throttle), \
                    mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": ""}):
                text = "\n".join(diagnostics.connection_report())
        finally:
            state.user_sessions.clear()
            state.user_sessions.update(saved[0])
            state.global_session = saved[1]
        self.assertIn(f"{STUMS}: 5 req, 2 ok, 1 throttled, 1 5xx, 1 network", text)
        self.assertIn("cooldown", text)  # the 429 started one
        self.assertIn("not configured", text)
        self.assertIn("<code>7</code> 👁: active", text)
        self.assertIn("StuMS ✅ 2 min ago", text)


class DailyDigestTests(unittest.TestCase):
    def _log(self, lines):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_scan_groups_recent_problems(self):
        from datetime import datetime
        from unibot import diagnostics
        path = self._log([
            "2026-11-07 08:00:00 | ERROR | too old 1",
            "2026-11-08 10:00:00 | ERROR | ❌ Login error in watcher: timeout 12",
            "Traceback (most recent call last):",
            "2026-11-08 11:00:00 | ERROR | ❌ Login error in watcher: timeout 34",
            "2026-11-08 12:00:00 | WARNING | slow host",
            "2026-11-08 12:30:00 | INFO | all good",
            "2026-11-08 13:00:00 | CRITICAL | disk full",
        ])
        counts = diagnostics.scan_log_problems([path, path + ".missing"], datetime(2026, 11, 8, 0, 0))
        self.assertEqual(counts["ERROR"]["❌ Login error in watcher: timeout #"], 2)
        self.assertEqual(counts["ERROR"]["disk full"], 1)
        self.assertEqual(sum(counts["ERROR"].values()), 3)
        self.assertEqual(dict(counts["WARNING"]), {"slow host": 1})

    def test_digest_shows_deltas_since_the_last_one(self):
        from datetime import datetime
        from unibot import diagnostics, state
        throttle = studip_http.HostThrottle(min_interval=0)
        for _ in range(5):
            throttle.report(STUMS, 200)
        throttle.report(STUMS, 502)
        previous = {"hosts": {STUMS: {"requests": 2, "server_errors": 0}}, "failovers": 0}
        path = self._log(["2026-11-08 08:00:00 | WARNING | <b>x</b>"])
        saved = dict(state.user_sessions)
        state.user_sessions.clear()
        try:
            with mock.patch.object(diagnostics, "THROTTLE", throttle), \
                    mock.patch.dict(os.environ, {"CLOUDFLARE_WORKER_URL": ""}):
                text = "\n".join(diagnostics.daily_digest(datetime(2026, 11, 8, 9, 0), [path], previous))
                snapshot = diagnostics.traffic_snapshot()
        finally:
            state.user_sessions.update(saved)
        self.assertIn(f"{STUMS}: 4 req, 0 throttled, 1 5xx", text)
        self.assertIn("0 errors, 1 warnings", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)  # log text is escaped
        self.assertEqual(snapshot["hosts"][STUMS]["requests"], 6)

    def test_counter_reset_after_restart(self):
        from unibot import diagnostics
        self.assertEqual(diagnostics._delta({"requests": 3}, {"requests": 50}), {"requests": 3})

    def test_watcher_sends_once_a_day_to_admins_only(self):
        import asyncio
        from datetime import datetime
        from unibot import watcher
        sent = []

        async def fake_broadcast(bot, text, **kwargs):
            sent.append(kwargs.get("members"))

        cache: dict = {}
        with mock.patch.object(watcher, "broadcast", fake_broadcast), \
                mock.patch.object(watcher, "ADMIN_USER_IDS", {1}), \
                mock.patch.object(watcher, "load_general_cache", lambda: cache), \
                mock.patch.object(watcher, "save_general_cache", lambda c: cache.update(c)), \
                mock.patch.object(watcher, "update_general_cache", lambda d: cache.update(d)):
            early = datetime(2026, 11, 8, 8, 0, tzinfo=watcher.TZ_BERLIN)
            asyncio.run(watcher._run_daily_digest(None, early))
            self.assertEqual(sent, [])
            later = datetime(2026, 11, 8, 9, 5, tzinfo=watcher.TZ_BERLIN)
            asyncio.run(watcher._run_daily_digest(None, later))
            asyncio.run(watcher._run_daily_digest(None, later))
        self.assertEqual(sent, [{1}])
        self.assertEqual(cache["last_daily_digest"], "2026-11-08")
        self.assertIn("daily_digest_snapshot", cache)


class ExamWindowStateTests(unittest.TestCase):
    BODY = ("Examination period: 1\nEnrollment feasible from 10/20/26, 12:00 AM until 2/5/27, 11:59 PM\n"
            "Examination period: 2\nEnrollment feasible from 2/15/27, 12:00 AM until 3/14/27, 11:59 PM")

    def test_only_the_current_window_is_open(self):
        import exam_reminder as er
        main, resit = er._parse_examination_periods(self.BODY, now=er.datetime(2026, 11, 8, 12, 0))
        self.assertTrue(main["is_open"])
        self.assertFalse(resit["is_open"])  # opens in February
        main, resit = er._parse_examination_periods(self.BODY, now=er.datetime(2027, 2, 20, 12, 0))
        self.assertFalse(main["is_open"])
        self.assertTrue(resit["is_open"])

    def test_legacy_key_covers_every_sitting_of_the_unit(self):
        import exam_reminder as er
        sittings = [{"code": "wir874_P1", "start_date": "2026-10-28T00:00:00", "end_date": end,
                     "unit_id": "u", "title": "Course", "raw": "w"}
                    for end in ("2027-02-05T23:59:00", "2027-03-03T23:59:00")]
        cache = {"notified_open": {"wir874_P1:None:None": "2026-11-08T20:21:09"}}
        sent = []

        async def fake_open(session, failed_codes=None):
            return sittings

        async def send(bot, text, reply_markup=None):
            sent.append(text)

        with mock.patch.object(er, "get_open_exam_registrations", fake_open), \
                mock.patch.object(er, "load_exam_cache", lambda *a, **k: cache), \
                mock.patch.object(er, "save_exam_cache", lambda data, *a, **k: cache.update(data)), \
                mock.patch.object(er, "berlin_now", lambda: er.datetime(2026, 11, 8, 22, 0)):
            asyncio.run(er.check_exam_reminders(object(), None, send, {}))
        self.assertFalse([t for t in sent if "REGISTRATION OPEN" in t])
        self.assertEqual(sorted(cache["notified_open"]),
                         ["wir874_P1:2026-10-28T00:00:00:2027-02-05T23:59:00",
                          "wir874_P1:2026-10-28T00:00:00:2027-03-03T23:59:00"])


class NamedTunnelTests(unittest.TestCase):
    def test_fixed_url_token_in_env_and_reuse(self):
        import shutil
        import tempfile
        import studip_tunnel
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, "cloudflared")
        seen = os.path.join(tmp, "seen.txt")
        with open(fake, "w") as fh:  # records its arguments and token, then stays up
            # no exec: like the real binary, the process keeps "cloudflared" in its command line
            fh.write(f'#!/bin/sh\necho "$@ token=$TUNNEL_TOKEN" > {seen}\nsleep 30\n')
        os.chmod(fake, 0o755)

        async def scenario():
            with mock.patch.object(studip_tunnel, "STATE_DIR", tmp):
                tunnel = studip_tunnel.NamedTunnel(18999, "https://bot.example.com/", "tok123", binary=fake)
                url = await tunnel.start(timeout=10)
                await tunnel.stop()
                for _ in range(50):  # let the fake binary record its arguments
                    if os.path.exists(seen) and os.path.getsize(seen):
                        break
                    await asyncio.sleep(0.1)
                again = studip_tunnel.NamedTunnel(18999, "https://bot.example.com", "tok123", binary=fake)
                pid_before = tunnel._pid
                reused_pid, reused_url = again._read_existing()
                studip_tunnel._kill(pid_before)
                return url, pid_before, reused_pid, reused_url

        url, pid, reused_pid, reused_url = asyncio.run(scenario())
        self.assertEqual(url, "https://bot.example.com")
        self.assertEqual((reused_pid, reused_url), (pid, "https://bot.example.com"))
        for _ in range(50):
            if os.path.exists(seen) and os.path.getsize(seen):
                break
            time.sleep(0.1)
        with open(seen) as fh:
            recorded = fh.read()
        self.assertIn("tunnel --no-autoupdate run", recorded)
        self.assertIn("token=tok123", recorded)
        self.assertNotIn("tok123 ", recorded.split("token=")[0])  # not on the command line


class SqliteStoreTests(unittest.TestCase):
    def test_exam_cache_and_pending_jobs_live_in_sqlite(self):
        import json
        import exam_reminder as er
        import fast_enroll
        from unibot import db
        tmp = tempfile.mkdtemp()
        legacy = os.path.join(tmp, "exam_reminder_cache_42.json")
        with open(legacy, "w") as fh:
            json.dump({"notified_open": {"x:None:None": "t"}}, fh)
        with mock.patch.object(er, "exam_cache_path", lambda uid=None: legacy):
            self.assertEqual(er.load_exam_cache(42)["notified_open"], {"x:None:None": "t"})
            self.assertFalse(os.path.exists(legacy))  # imported once, then renamed
            self.assertTrue(os.path.exists(legacy + ".imported"))
            er.save_exam_cache({"notified_open": {}, "known_grades": {"a": 1}}, 42)
            self.assertEqual(er.load_exam_cache(42)["known_grades"], {"a": 1})
            self.assertFalse(os.path.exists(legacy))  # saves never write a file
        pending = os.path.join(tmp, "fastenroll_pending.json")
        with mock.patch.object(fast_enroll, "PENDING_FILE", pending):
            fast_enroll.save_pending_job(7, "sem1", "10:00", "2026-11-09")
            self.assertEqual(fast_enroll.list_pending_jobs(7)["sem1"]["target_time_str"], "10:00")
            self.assertFalse(os.path.exists(pending))
        self.assertTrue(db.exists(legacy) and db.exists(pending))


class CookieStorageTests(unittest.TestCase):
    def test_user_login_moves_from_file_into_the_database(self):
        from studip_vault import write_vault_file
        from unibot import db
        tmp = tempfile.mkdtemp()
        cookie_file = os.path.join(tmp, "studip_cookies_4242.json")
        write_vault_file(cookie_file, [{"key": "Studip_Session", "value": "abc", "domain": "elearning.uni-oldenburg.de",
                                        "path": "/", "host_only": True}])
        db.delete_user_credentials(4242)

        async def scenario():
            sess = studip_session.StudIPSession("", "", "", cookie_file=cookie_file, user_id=4242)
            try:
                await sess.ensure_session()
                return {c.key: c.value for c in sess.session.cookie_jar}
            finally:
                await sess.close()

        jar = asyncio.run(scenario())
        self.assertEqual(jar.get("Studip_Session"), "abc")
        stored = db.load_user_credential(4242, "cookies")
        self.assertEqual([c["value"] for c in stored if c["key"] == "Studip_Session"], ["abc"])
        self.assertFalse(os.path.exists(cookie_file))
        db.delete_user_credentials(4242)


class RetireImportedFilesTests(unittest.TestCase):
    def test_files_already_in_the_database_are_renamed(self):
        from unibot import db
        tmp = tempfile.mkdtemp()
        stale = os.path.join(tmp, "stale_cache.json")
        db.save(stale, {"live": True})
        with open(stale, "w") as fh:
            fh.write('{"live": false}')
        other = os.path.join(tmp, "unrelated.json")
        with open(other, "w") as fh:
            fh.write("{}")
        self.assertGreaterEqual(db.retire_imported_files(), 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(stale + ".imported"))
        self.assertTrue(os.path.exists(other))  # not a stored document: untouched
        self.assertEqual(db.load(stale), {"live": True})


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class StumsFixtureTests(unittest.TestCase):
    """Parsers against anonymised copies of the real HISinOne page structure."""

    def test_study_planner_units_and_actions(self):
        import exam_reminder as er
        html_text = _fixture("stums_study_planner.html")
        candidates = er._parse_candidate_units(html_text)
        self.assertEqual([(u["code"], u["unit_id"], u["period_id"]) for u in candidates],
                         [("inf007_P", "11111", "222")])  # only "Apply" units, deduplicated
        status = {u["code"]: u for u in er._parse_registration_status(html_text)}
        self.assertEqual(sorted(status), ["inf007_P", "wir874_P1", "wir901_P"])
        self.assertEqual(status["inf007_P"]["action_type"], "register")
        self.assertEqual(status["wir874_P1"]["action_type"], "deregister")
        self.assertIsNone(status["wir901_P"]["action_type"])
        self.assertEqual(status["inf007_P"]["action"]["params"], {"unitId": "11111", "periodId": "222"})
        self.assertEqual(status["wir874_P1"]["status"], "Registered")

    def test_detail_page_sittings(self):
        import exam_reminder as er
        from bs4 import BeautifulSoup
        body = BeautifulSoup(_fixture("stums_exam_detail.html"), "html.parser").get_text("\n", strip=True)
        main, resit = er._parse_examination_periods(body, now=er.datetime(2026, 11, 8, 12, 0))
        self.assertEqual((main["start_date"], main["end_date"]), ("2026-10-20T00:00:00", "2027-02-05T23:59:00"))
        self.assertTrue(main["is_open"])
        self.assertFalse(resit["is_open"])
        self.assertEqual(main["exam_date"], "2027-02-08")
        self.assertIn("10:00 - 12:00", main["exam_date_display"])
        self.assertEqual(main["group_label"], "group 1")
        self.assertEqual((main["room"], resit["room"]), ("A14 1-101", "A14 1-102"))

    def test_open_registrations_end_to_end(self):
        import exam_reminder as er
        pages = {"studyPlanner": _fixture("stums_study_planner.html"), "detailView": _fixture("stums_exam_detail.html")}
        requested = []

        class Resp:
            def __init__(self, text):
                self._text = text

            async def text(self):
                return self._text

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Session:
            async def get(self, url, **kwargs):
                requested.append(url)
                return Resp(pages["detailView"] if "detailView-flow" in url else pages["studyPlanner"])

        async def signed_in(session):
            return er.STUMS_START_URL

        import stums_client
        # The fetcher lives in the client layer, so its collaborators are patched there.
        with mock.patch.object(stums_client, "_establish_stums_session", signed_in), \
                mock.patch.object(stums_client, "berlin_now", lambda: er.datetime(2026, 11, 8, 12, 0)):
            open_exams = asyncio.run(er.get_open_exam_registrations(Session()))
        self.assertEqual([(e["code"], e["start_date"]) for e in open_exams],
                         [("inf007_P", "2026-10-20T00:00:00")])
        self.assertTrue(any("unitId=11111" in u for u in requested))


class CourseWatchPlanTests(unittest.TestCase):
    """Every user's courses are watched, each once, and alerts reach participants only."""

    def setUp(self):
        from unibot import monitoring, state
        self.monitoring, self.state = monitoring, state
        self.saved = (dict(state.user_sessions), state.global_session, dict(state.user_courses_map))
        self.addCleanup(self._restore)
        monitoring._course_list_fetched.clear()

    def _restore(self):
        self.state.user_sessions.clear()
        self.state.user_sessions.update(self.saved[0])
        self.state.global_session = self.saved[1]
        self.state.user_courses_map.clear()
        self.state.user_courses_map.update(self.saved[2])

    def test_plan_covers_every_users_courses_once(self):
        m, state = self.monitoring, self.state
        s1, s2 = object(), object()
        state.user_sessions.clear()
        state.user_sessions.update({1: s1, 2: s2})
        state.global_session = None
        state.user_courses_map.clear()
        listed = []

        async def fake_list(session=None, user_id=None, semester_id=None):
            listed.append(user_id)
            state.user_courses_map[user_id] = {1: {"c1": "Math", "c2": "Stats"}, 2: {"c2": "Stats", "c3": "AI"}}[user_id]

        from unibot import auth
        with mock.patch.object(m, "list_courses", fake_list), \
                mock.patch.object(auth, "has_stored_login", lambda s: True), \
                mock.patch.object(auth.db, "get_all_users", lambda: [{"telegram_id": 1}, {"telegram_id": 2}]), \
                mock.patch.object(auth, "ALLOWED_USER_IDS", {1}):
            plan = asyncio.run(m.watched_courses())
            again = asyncio.run(m.watched_courses())
        self.assertEqual(sorted((cid, sess is s1) for _n, cid, sess in plan),
                         [("c1", True), ("c2", True), ("c3", False)])
        self.assertEqual(state.course_members["c2"], {1, 2})
        self.assertEqual(state.course_members["c3"], {2})
        self.assertEqual(len(again), 3)
        self.assertEqual(sorted(listed), [1, 2])  # course lists are cached between cycles

    def test_course_alerts_reach_participants_only(self):
        from unibot import telegram_ui
        sent = []

        class Bot:
            async def send_message(self, chat_id, **kwargs):
                sent.append(chat_id)

        users = [{"telegram_id": 1}, {"telegram_id": 2}, {"telegram_id": 3}]
        with mock.patch.object(telegram_ui, "ALLOWED_USER_IDS", {1, 2}), \
                mock.patch.object(telegram_ui.db, "get_all_users", lambda: users), \
                mock.patch.object(telegram_ui.db, "exists", lambda key: False):
            asyncio.run(telegram_ui.broadcast(Bot(), "x", cid="c3", members={2}))
            self.assertEqual(sorted(sent), [2])  # admin 1 does not take c3
            sent.clear()
            asyncio.run(telegram_ui.broadcast(Bot(), "x", cid="c3"))
            self.assertEqual(sorted(sent), [1, 2])  # unknown participants: old rules


class AccountRegistryTests(unittest.TestCase):
    def test_all_sessions_and_signed_in_accounts(self):
        from unibot import auth, state
        saved = (dict(state.user_sessions), state.global_session)
        a, b = object(), object()
        state.user_sessions.clear()
        state.user_sessions.update({1: a, 2: b})
        state.global_session = a  # the watcher is also user 1
        try:
            self.assertEqual(auth.all_sessions(), [a, b])  # each session once, watcher first
            with mock.patch.object(auth, "has_stored_login", lambda s: s is b), \
                    mock.patch.object(auth.db, "get_all_users", lambda: [{"telegram_id": 2}, {"telegram_id": 3, "is_active": 0}]), \
                    mock.patch.object(auth, "ALLOWED_USER_IDS", {1}):
                (account,) = auth.signed_in_accounts()
                self.assertEqual((account.user_id, account.session), (2, b))
                self.assertFalse(account.is_watcher)
                self.assertTrue(auth.get_account(1).is_watcher)
        finally:
            state.user_sessions.clear()
            state.user_sessions.update(saved[0])
            state.global_session = saved[1]


class LayeringTests(unittest.TestCase):
    """stums_parsers is the pure parsing layer: no network, state or bot imports."""

    def test_parsers_import_nothing_stateful(self):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "stums_parsers.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        forbidden = {"aiohttp", "studip_session", "studip_http", "unibot", "exam_reminder", "telegram"}
        self.assertFalse(imported & forbidden, imported & forbidden)
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.Await))])

    def test_exam_reminder_reexports_the_parsers(self):
        import exam_reminder
        import stums_parsers
        self.assertIs(exam_reminder._parse_examination_periods, stums_parsers._parse_examination_periods)

    @staticmethod
    def _imports(rel_path: str) -> set:
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel_path), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
        return found

    def test_stums_client_does_not_notify(self):
        imported = self._imports("stums_client.py")
        self.assertFalse({m for m in imported if m.startswith(("telegram", "unibot", "exam_reminder"))})

    def test_monitoring_layers(self):
        # change detection is pure; neither lower layer reaches up to the watchers
        self.assertEqual(self._imports("unibot/monitoring_diff.py"), {"datetime", "unibot.studip_api"})
        for rel in ("unibot/monitoring_fetch.py", "unibot/monitoring_diff.py", "unibot/monitoring_alerts.py"):
            self.assertNotIn("unibot.monitoring", self._imports(rel), rel)
        self.assertNotIn("telegram", {m.split(".")[0] for m in self._imports("unibot/monitoring_fetch.py")})

    def test_split_modules_are_reexported(self):
        import exam_reminder
        import stums_client
        from unibot import monitoring, monitoring_alerts, monitoring_diff, monitoring_fetch
        self.assertIs(exam_reminder.get_grades, stums_client.get_grades)
        self.assertIs(exam_reminder._establish_stums_session, stums_client._establish_stums_session)
        self.assertIs(monitoring._forum_thread_entry, monitoring_fetch._forum_thread_entry)
        self.assertIs(monitoring._diff_course_files, monitoring_diff._diff_course_files)
        # one shared dict, so tasks.py and the alert buttons see the same entries
        self.assertIs(monitoring.task_suggestion_cache, monitoring_alerts.task_suggestion_cache)
