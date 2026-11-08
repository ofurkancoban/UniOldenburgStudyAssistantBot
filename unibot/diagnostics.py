"""Connection diagnostics for the admin /healthcheck: request counters per
university host, throttle cooldowns, Cloudflare Worker routing and the sign-in
state of every user session. Read-only: nothing here sends a request."""

from __future__ import annotations

import html
import time

from studip_http import THROTTLE
from studip_session import worker_status
from unibot import state


def _session_line(uid, sess) -> str:
    jar = getattr(getattr(sess, "session", None), "cookie_jar", None)
    hosts = {c["domain"].lstrip(".") for c in jar} if jar is not None else set()
    studip = "✅" if any(h.startswith("elearning.") for h in hosts) else "—"
    verified_at = vars(sess).get("_stums_verified_at", 0.0) or 0.0
    if verified_at:
        stums = f"✅ {int((time.time() - verified_at) // 60)} min ago"
    else:
        stums = "—"
    paused = (vars(sess).get("stums_blocked_until", 0.0) or 0.0) - time.time()
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
