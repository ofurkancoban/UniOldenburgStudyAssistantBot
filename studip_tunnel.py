"""Cloudflare quick tunnel for the login page.

Runs `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<port>` in its
own detached process group and records its PID and log under `/tmp` so that
restarting the bot reuses the exact same running `https://...trycloudflare.com`
tunnel without rotating the hostname or invalidating Telegram's cached Menu
Button URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
from typing import Awaitable, Callable, Optional

URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

UrlCallback = Callable[[str], Optional[Awaitable[None]]]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class QuickTunnel:
    """Supervises a detached `cloudflared` quick tunnel and reuses it across restarts."""

    def __init__(self, port: int, binary: Optional[str] = None,
                 on_url_change: Optional[UrlCallback] = None):
        self.port = port
        self.binary = binary or os.getenv("CLOUDFLARED_BIN", "cloudflared")
        self.on_url_change = on_url_change
        self._url = ""
        self._pid: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._pid_file = f"/tmp/studip_cloudflared_{self.port}.pid"
        self._log_file = f"/tmp/studip_cloudflared_{self.port}.log"

    @property
    def url(self) -> str:
        return self._url

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _read_existing(self) -> tuple[Optional[int], str]:
        pid = None
        if os.path.exists(self._pid_file):
            try:
                with open(self._pid_file, "r", encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                pid = None
        if not pid or not _pid_alive(pid):
            return None, ""
        url = ""
        if os.path.exists(self._log_file):
            try:
                with open(self._log_file, "r", encoding="utf-8", errors="replace") as fh:
                    matches = URL_RE.findall(fh.read())
                    if matches:
                        url = matches[-1]
            except OSError:
                pass
        return pid, url

    async def start(self, timeout: float = 60.0) -> str:
        """Reuse an existing tunnel or start a new one, waiting until its URL is ready."""
        if not self.available():
            raise RuntimeError(
                f"'{self.binary}' not found. Install cloudflared or unset "
                f"CLOUDFLARE_TUNNEL=quick."
            )
        pid, url = self._read_existing()
        if pid and url:
            self._pid = pid
            self._url = url
            logging.info("🌐 Reusing active Cloudflare quick tunnel (PID %s): %s", pid, url)
            if self.on_url_change:
                res = self.on_url_change(url)
                if asyncio.iscoroutine(res):
                    await res

        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._supervise())

        deadline = asyncio.get_running_loop().time() + timeout
        while not self._url:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("cloudflared did not report a URL in time")
            await asyncio.sleep(0.25)
        return self._url

    async def stop(self) -> None:
        """Stop the local supervisor task while leaving the detached cloudflared
        process running so bot restarts keep the same public URL."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                pid, url = self._read_existing()
                if pid and url:
                    self._pid = pid
                    if url != self._url:
                        self._url = url
                        logging.info("🌐 Public login URL: %s", self._url)
                        if self.on_url_change:
                            res = self.on_url_change(self._url)
                            if asyncio.iscoroutine(res):
                                await res
                    while not self._stopping and _pid_alive(pid):
                        await asyncio.sleep(2.0)
                else:
                    await self._spawn_detached()
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.error(f"⚠️ cloudflared supervisor error: {exc}")
            if self._stopping:
                break
            logging.warning("🔄 cloudflared stopped - restarting in 3s")
            await asyncio.sleep(3)

    async def _spawn_detached(self) -> None:
        logging.info("🌐 Starting Cloudflare quick tunnel -> port %s", self.port)
        bin_q = shlex.quote(self.binary)
        log_q = shlex.quote(self._log_file)
        pid_q = shlex.quote(self._pid_file)
        url_q = shlex.quote(f"http://127.0.0.1:{self.port}")
        shell_cmd = (
            f"nohup {bin_q} tunnel --no-autoupdate --url {url_q} "
            f">/dev/null 2>{log_q} </dev/null & echo $! > {pid_q}"
        )
        subprocess.run(
            ["/bin/sh", "-c", shell_cmd],
            start_new_session=True,
            check=False,
        )
        pid = None
        if os.path.exists(self._pid_file):
            try:
                with open(self._pid_file, "r", encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                pid = None
        self._pid = pid

        for _ in range(120):
            if self._stopping or (pid and not _pid_alive(pid)):
                break
            try:
                with open(self._log_file, "r", encoding="utf-8", errors="replace") as fh:
                    matches = URL_RE.findall(fh.read())
                    if matches:
                        new_url = matches[-1]
                        if new_url != self._url:
                            self._url = new_url
                            logging.info("🌐 Public login URL: %s", self._url)
                            if self.on_url_change:
                                res = self.on_url_change(self._url)
                                if asyncio.iscoroutine(res):
                                    await res
                        break
            except OSError:
                pass
            await asyncio.sleep(0.25)
