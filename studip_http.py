"""Shared outbound HTTP policy for every University of Oldenburg host.

All users of the bot talk to Stud.IP, StuMS and the NetIQ SSO from the same
server, so the university sees one client, however many Telegram users there
are. This module keeps that client polite and predictable:

* `HostThrottle` is process-wide (shared by every user session). It caps the
  number of parallel requests per host, spaces background requests out,
  and pauses a host after it answers 429/503 (honouring Retry-After) or
  after repeated connection failures, instead of hammering it.
* `UniResponse` wraps an aiohttp response so callers always see the real
  university URL, even when the request travelled through the optional
  Cloudflare Worker proxy (relative links and form actions then resolve
  against the right host).

Everything here is dependency-free apart from aiohttp/yarl and holds no
per-user state, so it is safe to share between users.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import email.utils
import logging
import math
import os
import random
import time
from typing import Optional

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

logger = logging.getLogger("StudIPHttp")

UNI_HOSTS = frozenset({
    "studip.uni-oldenburg.de",
    "elearning.uni-oldenburg.de",
    "stums.uni-oldenburg.de",
    "idp.uni-oldenburg.de",
    "login.uni-oldenburg.de",
    "auth.uni-oldenburg.de",
})

# One stable browser identity for every request of the bot (background jobs,
# SSO handoffs and the /login proxy alike), so a session never changes its
# User-Agent half way. Override with STUDIP_USER_AGENT if needed.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def user_agent() -> str:
    return os.getenv("STUDIP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# How strictly requests are spaced out:
#   BACKGROUND  watchers, keep-alive, SSO handoffs: full spacing (default)
#   INTERACTIVE a student's button tap or command: a quarter of the spacing
#   UNSPACED    the /login browser proxy (a real browser loading a page) and
#               the time-critical fast enrolment: no spacing at all
# Concurrency limits and cooldowns after 429/503 apply to every level.
BACKGROUND, INTERACTIVE, UNSPACED = "background", "interactive", "unspaced"
_priority = contextvars.ContextVar("uni_http_priority", default=BACKGROUND)
_SPACING_FACTOR = {BACKGROUND: 1.0, INTERACTIVE: 0.25, UNSPACED: 0.0}


def set_request_priority(level: str) -> None:
    """Set the priority for the rest of the current task (e.g. one Telegram update)."""
    _priority.set(level if level in _SPACING_FACTOR else BACKGROUND)


@contextlib.contextmanager
def request_priority(level: str):
    token = _priority.set(level if level in _SPACING_FACTOR else BACKGROUND)
    try:
        yield
    finally:
        _priority.reset(token)


def interactive_requests():
    """Requests inside this block are sent without spacing (browser-like)."""
    return request_priority(UNSPACED)


class UniHostCooldown(RuntimeError):
    """The host asked us to back off (429/503) or keeps failing; the request
    was not sent. `quiet` lets background callers log it at debug level."""

    def __init__(self, host: str, remaining: float):
        super().__init__(f"{host} is cooling down after errors; retry in {int(remaining) + 1}s")
        self.host = host
        self.remaining = remaining
        self.quiet = True


class HostThrottle:
    """Process-wide politeness limits per university host."""

    # A cooldown shorter than this is simply waited out; a longer one makes
    # the request fail fast with UniHostCooldown.
    MAX_INLINE_WAIT = 20.0
    FAILURES_BEFORE_COOLDOWN = 4
    MAX_COOLDOWN = 15 * 60

    def __init__(self, max_concurrency: Optional[int] = None, min_interval: Optional[float] = None):
        concurrency = max_concurrency if max_concurrency is not None else _env_int("UNI_MAX_CONCURRENCY", 4)
        self.max_concurrency = max(1, concurrency)
        interval = min_interval if min_interval is not None else _env_float("UNI_MIN_REQUEST_INTERVAL", 0.35)
        try:
            interval = float(interval)
        except (TypeError, ValueError, OverflowError):
            interval = 0.35
        self.min_interval = max(0.0, interval) if math.isfinite(interval) else 0.35
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._loops: dict[str, object] = {}
        self._next_slot: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._spacing_lock: dict[str, asyncio.Lock] = {}
        # counters since start per host, shown by /healthcheck
        self.stats: dict[str, dict[str, int]] = {}

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._semaphores.get(host)
        if sem is None or self._loops.get(host) is not loop:
            # asyncio primitives are bound to one event loop (tests run many)
            sem = asyncio.Semaphore(self.max_concurrency)
            self._semaphores[host] = sem
            self._spacing_lock[host] = asyncio.Lock()
            self._loops[host] = loop
        return sem

    def cooldown_remaining(self, host: str) -> float:
        return max(0.0, self._cooldown_until.get(host, 0.0) - time.monotonic())

    @contextlib.asynccontextmanager
    async def slot(self, host: str):
        host = (host or "").lower()
        if host not in UNI_HOSTS:
            yield
            return
        remaining = self.cooldown_remaining(host)
        if remaining > self.MAX_INLINE_WAIT:
            raise UniHostCooldown(host, remaining)
        if remaining:
            await asyncio.sleep(remaining)
        sem = self._semaphore(host)
        async with sem:
            # Another in-flight request can receive 429/503 while this one
            # waits for the semaphore. Recheck after acquiring it so queued
            # requests honor the newer host-wide cooldown instead of
            # immediately sending a burst into the upstream.
            while True:
                remaining = self.cooldown_remaining(host)
                if remaining > self.MAX_INLINE_WAIT:
                    raise UniHostCooldown(host, remaining)
                if not remaining:
                    break
                await asyncio.sleep(remaining)
            interval = self.min_interval * _SPACING_FACTOR.get(_priority.get(), 1.0)
            if interval:
                async with self._spacing_lock[host]:
                    while True:
                        # A request already in flight may receive 429/503
                        # while this one waits for spacing. Recheck here too,
                        # immediately before reserving a send slot.
                        remaining = self.cooldown_remaining(host)
                        if remaining > self.MAX_INLINE_WAIT:
                            raise UniHostCooldown(host, remaining)
                        if remaining:
                            await asyncio.sleep(remaining)
                            continue
                        wait = self._next_slot.get(host, 0.0) - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                            continue
                        # small jitter so many users' jobs never line up exactly
                        self._next_slot[host] = (
                            time.monotonic() + interval * random.uniform(0.8, 1.3)
                        )
                        break
            yield

    def _set_cooldown(self, host: str, seconds: float, reason: str) -> None:
        seconds = min(max(seconds, 1.0), self.MAX_COOLDOWN)
        until = time.monotonic() + seconds
        if until > self._cooldown_until.get(host, 0.0):
            self._cooldown_until[host] = until
            logger.warning("Pausing requests to %s for %ss (%s)", host, int(seconds), reason)

    def report(self, host: str, status: Optional[int] = None, retry_after: Optional[float] = None,
               failed: bool = False) -> None:
        """Feed back the result of a request (status code or a connection failure)."""
        host = (host or "").lower()
        if host not in UNI_HOSTS:
            return
        counts = self.stats.setdefault(host, {"requests": 0, "ok": 0, "client_errors": 0,
                                               "throttled": 0, "server_errors": 0, "network_errors": 0})
        counts["requests"] += 1
        if failed:
            counts["network_errors"] += 1
        elif status in (429, 503):
            counts["throttled"] += 1
        elif status is not None and status >= 500:
            counts["server_errors"] += 1
        elif status is not None and status >= 400:
            counts["client_errors"] += 1
        else:
            counts["ok"] += 1
        if status in (429, 503):
            streak = self._failures.get(host, 0) + 1
            self._failures[host] = streak
            self._set_cooldown(host, retry_after if retry_after is not None
                               else 30.0 * (2 ** min(streak - 1, 5)),
                               f"HTTP {status}")
            return
        if failed or (status is not None and status >= 500):
            streak = self._failures.get(host, 0) + 1
            self._failures[host] = streak
            if streak >= self.FAILURES_BEFORE_COOLDOWN:
                self._set_cooldown(host, 30.0 * (2 ** min(streak - self.FAILURES_BEFORE_COOLDOWN, 5)),
                                   f"{streak} failed requests in a row")
            return
        self._failures[host] = 0


THROTTLE = HostThrottle()


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def to_timeout(timeout) -> Optional[aiohttp.ClientTimeout]:
    """Accept the plain numbers callers pass and turn them into ClientTimeout."""
    if timeout is None or isinstance(timeout, aiohttp.ClientTimeout):
        return timeout
    try:
        return aiohttp.ClientTimeout(total=float(timeout))
    except (TypeError, ValueError):
        return None


DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=90)


class UniResponse:
    """An aiohttp response that reports the real university URL.

    Delegates everything to the wrapped ClientResponse. `url` / `real_url`
    are the upstream URL (never the Cloudflare Worker's), and `headers` has
    its Location header translated back the same way.
    """

    def __init__(self, resp: aiohttp.ClientResponse, real_url: str, headers=None, history=()):
        self._resp = resp
        self.url = URL(real_url)
        self.real_url = self.url
        self.history = tuple(history)
        self._headers = headers

    @property
    def headers(self):
        return self._headers if self._headers is not None else self._resp.headers

    def __getattr__(self, name):
        return getattr(self._resp, name)

    def __await__(self):
        async def _self():
            return self
        return _self().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._resp.release()

    def release(self):
        return self._resp.release()

    def close(self):
        return self._resp.close()


def rewrite_headers(headers: CIMultiDictProxy, location: Optional[str]) -> CIMultiDictProxy:
    """Copy of `headers` with Location replaced (and proxy-only headers removed)."""
    out: CIMultiDict[str] = CIMultiDict()
    for k, v in headers.items():
        kl = k.lower()
        if kl == "location":
            continue
        if kl in ("x-upstream-set-cookie", "x-upstream-set-cookie-json"):
            continue
        out.add(k, v)
    if location:
        out["Location"] = location
    return CIMultiDictProxy(out)
