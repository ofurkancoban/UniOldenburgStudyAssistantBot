"""Mutable runtime state shared across modules (sessions, servers, watcher flags).

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import subprocess
from datetime import datetime
from typing import Any, Optional

import aiohttp

from studip_login_server import LoginServer
from studip_session import StudIPSession
from studip_tunnel import QuickTunnel


last_full_check_time: Optional[datetime] = None
global_scan_count = 0
# ── global runtime state ───────────────────────────────────────────────────────
global_session: Optional[StudIPSession] = None
user_sessions: dict[int, StudIPSession] = {}
global_watcher_paused = False
message_poll_task: Optional[asyncio.Task] = None  # see message_poll_loop
session_keepalive_task: Optional[asyncio.Task] = None  # see session_keepalive_loop
unified_watcher_task: Optional[asyncio.Task] = None
watcher_controller_running = False
courses_map: dict[str, str] = {}  # cid -> course_name


_shared_http_session: Optional[aiohttp.ClientSession] = None

# Handle to the WhatsApp microservice subprocess, set once it's spawned in __main__.
# Referenced by restart_command() so a self-restart can shut it down cleanly first.
wa_process: Optional[subprocess.Popen] = None
_login_failure_notified = False


# ── web / Mini App login ──────────────────────────────────────────────────────
login_server: Optional[LoginServer] = None
quick_tunnel: Optional[QuickTunnel] = None
_menu_app: Any = None  # telegram.ext.Application, set at startup
_last_notified_studip_cookie: Optional[str] = None


_courses_map_built_with_prev: Optional[bool] = None  # tracks whether the shared courses_map currently reflects get_watch_previous_semester_enabled() == True/False, so it's rebuilt only when that's actually changed
_studip_api_user_id: Optional[str] = None
_studip_user_profile: Optional[dict] = None
_studip_api_semester_starts: Optional[dict] = None  # {semester_id: start datetime}, fetched once per process
_studip_api_current_semester: Optional[str] = None

# Today's prepared summary ({"date": "YYYY-MM-DD", "data": <prepare_morning_summary result>})
# and the task that delivers it at MORNING_SUMMARY_HOUR. In memory only: if
# the bot restarts in between, the watcher just prepares it again.
_prepared_morning: dict = {}
_morning_delivery_task: Optional[asyncio.Task] = None


# ──────────────────────────────────────────────────────────────────────────────
#  CALLBACK ROUTER + REPLY KEYBOARD + MAIN BOT LOOP
# ──────────────────────────────────────────────────────────────────────────────
# Separate pause control for each watcher:
message_watcher_paused = False
announcement_watcher_paused = False
file_watcher_paused = False
_gwdg_blocked_until = 0.0
