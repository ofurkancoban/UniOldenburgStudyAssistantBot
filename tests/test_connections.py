"""Connection layer: worker routing, per-host cookies, throttling, per-user StuMS."""

import asyncio
import os
import sys
import time
import unittest
from unittest import mock

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

    def __init__(self, legacy=False, refuse=()):
        self.refuse = set(refuse)  # hosts an outdated worker.js does not allow
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


class SsoChainTests(unittest.TestCase):
    """One university sign-in in the Mini App connects Stud.IP and StuMS."""

    def setUp(self):
        import studip_login_server as ls
        from aiohttp.test_utils import make_mocked_request
        self.ls = ls
        self.req = lambda method="GET", headers=None: make_mocked_request(method, "/x", headers=headers or {})

    def target(self, sess, studip=False, stums=False, **kw):
        return self.ls.LoginServer._chain_target(self.req(**kw), sess, studip, stums)

    def test_studip_login_chains_to_stums_once(self):
        sess = {"idp_seen": True}
        self.assertEqual(self.target(sess, studip=True), self.ls.STUMS_SSO_ENTRY_PATH)
        sess["chained"] = True
        self.assertIsNone(self.target(sess, studip=True))

    def test_stums_login_chains_to_studip(self):
        self.assertEqual(self.target({"idp_seen": True}, stums=True), self.ls.STUDIP_SSO_LOGIN_PATH)

    def test_no_chain_when_other_service_already_seen(self):
        self.assertIsNone(self.target({"idp_seen": True, "stums_seen": True}, studip=True))
        self.assertIsNone(self.target({"idp_seen": True, "studip_seen": True}, stums=True))

    def test_no_chain_without_a_fresh_sign_in_or_for_ajax(self):
        self.assertIsNone(self.target({}, studip=True))
        self.assertIsNone(self.target({"idp_seen": True}, studip=True, method="POST"))
        self.assertIsNone(self.target({"idp_seen": True}, studip=True,
                                      headers={"X-Requested-With": "XMLHttpRequest"}))
        self.assertIsNone(self.target({"idp_seen": True}, studip=True,
                                      headers={"Sec-Fetch-Dest": "empty"}))

    def test_pending_chain_from_entry_runs_on_the_next_studip_page(self):
        sess = {"pending_chain": self.ls.STUMS_SSO_ENTRY_PATH}
        self.assertEqual(self.target(sess, studip=True), self.ls.STUMS_SSO_ENTRY_PATH)
        self.assertNotIn("pending_chain", sess)

    def test_telegram_web_iframe_counts_as_page_load(self):
        self.assertEqual(self.target({"idp_seen": True}, studip=True,
                                     headers={"Sec-Fetch-Dest": "iframe"}), self.ls.STUMS_SSO_ENTRY_PATH)


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
