"""Connection diagnostics for the admin /healthcheck: request counters per
university host, throttle cooldowns, Cloudflare Worker routing and the sign-in
state of every user session. Read-only: nothing here sends a request."""

from __future__ import annotations

import html
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

from studip_http import THROTTLE
from studip_session import worker_status
from unibot import db, state
from unibot.config import PROJECT_ROOT


def _session_line(uid, sess) -> str:
    jar = getattr(getattr(sess, "session", None), "cookie_jar", None)
    hosts = {c["domain"].lstrip(".") for c in jar} if jar is not None else set()
    studip = "✅" if any(h.startswith("elearning.") for h in hosts) else "❌"
    verified_at = sess.stums.verified_at
    if verified_at:
        stums = f"✅ {int((time.time() - verified_at) // 60)} min ago"
    else:
        # verified_at lives in memory: after a restart it is empty until the
        # next StuMS check (every few hours) signs in again
        stums = "not checked since restart"
    paused = sess.stums.blocked_until - time.time()
    if paused > 0:
        stums += f" (paused {int(paused // 60) + 1} min)"
    state_label = "signed out" if getattr(sess, "explicitly_logged_out", False) else "active"
    watcher = " 👁" if sess is state.global_session else ""
    return (f"• <code>{html.escape(str(uid))}</code>{watcher}: {state_label}, "
            f"Stud.IP cookie {studip}, StuMS {stums}")


def connection_report() -> list[str]:
    """HTML lines for the admin part of /healthcheck."""
    lines = ["🌐 <b>University connections</b> (since start)"]
    if not THROTTLE.stats:
        lines.append("• no requests yet")
    for host in sorted(THROTTLE.stats):
        c = THROTTLE.stats[host]
        line = (f"• {html.escape(host)}: {c['requests']} req, {c['ok']} ok, "
                f"{c['throttled']} throttled, {c['server_errors']} 5xx, {c['network_errors']} network")
        cooldown = THROTTLE.cooldown_remaining(host)
        if cooldown > 0:
            line += f" ⏸ cooldown {int(cooldown)} s"
        lines.append(line)

    worker = worker_status()
    if not worker["configured"]:
        lines.append("☁️ Cloudflare Worker: not configured (direct requests)")
    elif worker["down_for"] > 0:
        lines.append(f"☁️ Cloudflare Worker: ⚠️ down, direct requests for {int(worker['down_for'] // 60) + 1} min "
                     f"({worker['failovers']} failovers)")
    else:
        lines.append(f"☁️ Cloudflare Worker: ✅ in use ({worker['failovers']} failovers since start)")
    if worker["refused_hosts"]:
        lines.append("☁️ Refused by the worker: " + ", ".join(worker["refused_hosts"]))

    sessions = {uid: s for uid, s in state.user_sessions.items() if s is not None}
    lines.append(f"👥 <b>Sessions</b> ({len(sessions)})")
    for uid, sess in sorted(sessions.items(), key=lambda kv: str(kv[0])):
        lines.append(_session_line(uid, sess))
    if state.global_session is not None and state.global_session not in sessions.values():
        lines.append(_session_line("watcher", state.global_session))
    return lines


# ── daily digest ──────────────────────────────────────────────────────────────
# One summary per day for the admins: what went wrong in the last 24 hours
# (error/warning log lines), how the university hosts answered since the last
# digest, Cloudflare Worker failovers and the sign-in state of every session.

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (ERROR|WARNING|CRITICAL) \| (.*)$")
DIGEST_TOP_MESSAGES = 5


def _normalise_message(message: str) -> str:
    """Group log lines that differ only in numbers (ids, counts, times)."""
    # HTTP status codes stay readable ("HTTP 401"), other numbers are grouped
    text = re.sub(r"(?<!HTTP )\b\d+", "#", message.strip())
    return text[:120]


def scan_log_problems(paths: Iterable[str], since: datetime) -> dict[str, Counter]:
    """Count ERROR/WARNING lines newer than `since` in the given log files,
    keyed by level, grouped by normalised message. Missing files are skipped;
    traceback continuation lines carry no prefix and are ignored."""
    counts: dict[str, Counter] = {"ERROR": Counter(), "WARNING": Counter()}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _LOG_LINE.match(line.rstrip("\n"))
                    if not m:
                        continue
                    try:
                        stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if stamp < since:
                        continue
                    level = "ERROR" if m.group(2) == "CRITICAL" else m.group(2)
                    counts[level][_normalise_message(m.group(3))] += 1
        except OSError:
            continue
    return counts


def traffic_snapshot() -> dict:
    """Current cumulative counters, stored so the next digest can show deltas."""
    return {
        "hosts": {host: dict(c) for host, c in THROTTLE.stats.items()},
        "failovers": worker_status()["failovers"],
    }


def _delta(now: dict, before: dict) -> dict:
    """Counter growth since `before`. A counter that shrank means the bot
    restarted in between, so the current value is the growth since then."""
    out = {}
    for key, value in now.items():
        prev = before.get(key, 0)
        out[key] = value - prev if value >= prev else value
    return out


def backup_line(backup_dir: Optional[str] = None, now: Optional[float] = None) -> str:
    """The newest database backup, flagged when it is older than two days."""
    backup_dir = backup_dir or os.path.join(PROJECT_ROOT, db.BACKUP_DIR)
    try:
        names = sorted(f for f in os.listdir(backup_dir) if f.startswith("bot_data_") and f.endswith(".sqlite3.gz"))
    except OSError:
        names = []
    if not names:
        return "💾 Database backup: ⚠️ none yet"
    path = os.path.join(backup_dir, names[-1])
    age_hours = ((now or time.time()) - os.path.getmtime(path)) / 3600
    size_mb = os.path.getsize(path) / (1024 * 1024)
    flag = "⚠️" if age_hours > 48 else "✅"
    return f"💾 Database backup: {flag} {names[-1]} ({size_mb:.1f} MB, {len(names)} kept)"


def daily_digest(now: datetime, log_paths: Iterable[str], previous: Optional[dict] = None) -> list[str]:
    """HTML lines of the admin daily digest. `now` is naive local time, like
    the log timestamps; `previous` is the traffic_snapshot() of the last digest."""
    previous = previous or {}
    lines = [f"🩺 <b>Daily health digest</b> ({now.strftime('%Y-%m-%d %H:%M')})"]

    problems = scan_log_problems(log_paths, now - timedelta(hours=24))
    errors, warnings = problems["ERROR"], problems["WARNING"]
    total_errors, total_warnings = sum(errors.values()), sum(warnings.values())
    if not total_errors and not total_warnings:
        lines.append("✅ No errors or warnings in the last 24 h")
    else:
        lines.append(f"📋 Last 24 h: {total_errors} errors, {total_warnings} warnings")
        for label, counter in (("❌", errors), ("⚠️", warnings)):
            for message, n in counter.most_common(DIGEST_TOP_MESSAGES):
                lines.append(f"{label} {n}× <code>{html.escape(message)}</code>")

    current = traffic_snapshot()
    prev_hosts = previous.get("hosts", {})
    lines.append("🌐 <b>University requests</b> (since the last digest)")
    shown = False
    for host in sorted(current["hosts"]):
        d = _delta(current["hosts"][host], prev_hosts.get(host, {}))
        if not d.get("requests"):
            continue
        shown = True
        lines.append(f"• {html.escape(host)}: {d.get('requests', 0)} req, {d.get('throttled', 0)} throttled, "
                     f"{d.get('server_errors', 0)} 5xx, {d.get('network_errors', 0)} network")
    if not shown:
        lines.append("• no requests")

    failovers = _delta({"f": current["failovers"]}, {"f": previous.get("failovers", 0)})["f"]
    worker = worker_status()
    if worker["configured"]:
        state_label = "⚠️ down" if worker["down_for"] > 0 else "✅ up"
        lines.append(f"☁️ Cloudflare Worker: {state_label}, {failovers} failovers since the last digest")

    lines.append(backup_line())

    sessions = {uid: s for uid, s in state.user_sessions.items() if s is not None}
    lines.append(f"👥 <b>Sessions</b> ({len(sessions)})")
    for uid, sess in sorted(sessions.items(), key=lambda kv: str(kv[0])):
        lines.append(_session_line(uid, sess))
    return lines
