"""Health checks, /status, /healthcheck and self-restart.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import os
import shutil
import sys
import time

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from datetime import datetime
import studip_oauth
from fast_enroll import semester_display
from studip_utils import TZ_BERLIN, berlin_now

from unibot import db, state
from unibot.telegram_ui import user_data, broadcast
from unibot.config import (
    ADMIN_USER_IDS,
    ALLOWED_USER_IDS,
    PRIMARY_ADMIN_ID,
    PROJECT_ROOT,
    WA_HEADERS,
    WA_SERVICE_URL,
    get_wa_group_id,
)
from unibot.preferences import get_pref
from unibot.storage import (
    get_user_name,
    link_cache,
    load_default_semester,
    load_general_cache,
    shared_http,
)
from unibot.diagnostics import connection_report
from unibot.auth import (
    _get_login_server,
    _login_public_url,
    _perform_logout,
    get_user_session,
    is_user_admin,
    is_user_allowed,
    login_studip,
    release_instance_lock,
)
from unibot.studip_api import get_studip_user_profile, list_courses
from unibot.ai import check_gwdg_health, check_openrouter_health
from unibot.weather import check_weather_health, load_weather_location
from unibot.security_events import log_security_event


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the bot process itself (and cleanly shut down the WhatsApp microservice first)."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_admin(user_id):
        await update.message.reply_text("⛔ This command is restricted to administrators.")
        return

    await update.message.reply_text("♻️ Restarting bot...")
    logging.info("Restart requested by user %s.", user_id)
    state.spawn(_restart_process(), name="restart")


async def _restart_process():
    from unibot.app import stop_whatsapp_microservice
    # Give the reply above a moment to actually reach Telegram before we tear things down.
    await asyncio.sleep(0.5)

    from unibot.schedule import stop_fastenroll_jobs
    await stop_fastenroll_jobs()
    stop_whatsapp_microservice()
    link_cache.flush()

    # os.execv replaces the current process image in place (same PID), which
    # works the same whether pm2 is managing us or not - pm2 just sees its
    # monitored PID stay alive and keeps watching it, no restart-detection
    # needed. execv also skips atexit handlers, which is why the WhatsApp
    # subprocess is stopped explicitly above.
    #
    # The lock file is released here as well, although acquire_instance_lock()
    # already treats a lock holding its own PID (identical after execv) as stale.
    release_instance_lock()
    logging.info("Re-executing process for restart...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


HEALTH_CHECK_LABELS = {
    "studip": "🎓 Stud.IP",
    "whatsapp": "📱 WhatsApp",
    "weather": "🌤️ Weather (Open-Meteo)",
    "openrouter": "🤖 OpenRouter (AI features)",
    "gwdg": "🎓 GWDG AI (text, tagging, try-on fallback)",
    "disk": "💾 Server disk",
}

# Below this much free space the disk counts as unhealthy: a full disk
# breaks every cache write and once truncated the WhatsApp session file.
DISK_MIN_FREE_GB = 5


async def check_studip_health() -> tuple[bool, str]:
    """(ok, detail) - logs in (or confirms the cached session still works)
    and fetches the course list as a cheap end-to-end check that Stud.IP
    access actually works, not just that credentials are configured."""
    try:
        await login_studip()
        courses = await list_courses()
        if courses:
            return True, "OK"
        return False, "Logged in, but no courses returned"
    except Exception as e:
        return False, str(e)[:150]


async def check_whatsapp_health() -> tuple[bool, str]:
    """(ok, detail) - asks the local WhatsApp microservice for its own
    connection state (same /status endpoint the 📱 WhatsApp status line
    in /status uses)."""
    try:
        async with shared_http() as session:
            async with session.get(f"{WA_SERVICE_URL}/status", headers=WA_HEADERS, timeout=5) as resp:
                if resp.status != 200:
                    return False, f"Service returned HTTP {resp.status}"
                data = await resp.json()
                if data.get("isAuthenticated"):
                    return True, "OK"
                if data.get("isReconnecting"):
                    return False, "Reconnecting"
                return False, "Not authenticated (needs a QR scan)"
    except Exception as e:
        return False, f"Service unreachable: {str(e)[:120]}"


def check_disk_health() -> tuple[bool, str]:
    """(ok, detail) for the disk the bot's files live on; unhealthy below
    DISK_MIN_FREE_GB free."""
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
    except OSError as e:
        return False, str(e)[:150]
    free_gb = usage.free / 1024 ** 3
    detail = f"{free_gb:.1f} GB free ({usage.used / usage.total:.0%} used)"
    if free_gb < DISK_MIN_FREE_GB:
        return False, f"Almost full, {detail}. Cache and WhatsApp session writes will fail."
    return True, detail


async def run_health_checks(live_openrouter: bool = False) -> dict:
    """Run every subsystem check and return {key: (ok, detail)} for
    "studip", "whatsapp", "weather", "openrouter", "gwdg", "disk" (see HEALTH_CHECK_LABELS
    for display names). Each check is independently best-effort - one
    failing never blocks or skews the others."""
    studip = await check_studip_health()
    whatsapp = await check_whatsapp_health()
    weather = await check_weather_health()
    openrouter = await check_openrouter_health(live=live_openrouter)
    gwdg = await check_gwdg_health(live=live_openrouter)
    disk = check_disk_health()
    return {"studip": studip, "whatsapp": whatsapp, "weather": weather, "openrouter": openrouter, "gwdg": gwdg, "disk": disk}


async def _student_health_text(user_id: int) -> str:
    """What a student sees: their own Stud.IP connection and one line from
    the last background health check. Server details (and the live, paid AI
    check) are for administrators."""
    try:
        signed_in = await get_user_session(user_id).is_logged_in()
    except Exception:
        logging.debug("Session check failed", exc_info=True)
        signed_in = False
    health = load_general_cache().get("health_state") or {}
    down = [HEALTH_CHECK_LABELS[k] for k, ok in health.items() if not ok and k in HEALTH_CHECK_LABELS]
    service = ("✅ All services are working." if not down
               else "⚠️ Some services have problems right now; the administrators have been told.")
    return "\n".join([
        "🩺 <b>Status</b>",
        "━━━━━━━━━━━━━━━━━",
        f"🎓 Your Stud.IP connection: {'✅ connected' if signed_in else '❌ not connected (/login)'}",
        service,
    ])


async def healthcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /healthcheck - an on-demand, full (live OpenRouter call
    included) check of every subsystem the bot depends on, plus the
    currently-running git commit, so "is everything actually working
    right now" is a single command instead of SSHing into the server."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return
    if not is_user_admin(user_id):
        await update.message.reply_text(await _student_health_text(user_id), parse_mode="HTML")
        return

    msg = await update.message.reply_text("🩺 Running health check...")
    results = await run_health_checks(live_openrouter=True)

    lines = ["🩺 <b>Health Check</b>", "━━━━━━━━━━━━━━━━━"]
    for key, (ok, detail) in results.items():
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} {HEALTH_CHECK_LABELS[key]}: {html.escape(detail)}")
    lines.append("━━━━━━━━━━━━━━━━━")
    if is_user_admin(user_id):
        lines += connection_report()
        lines.append("━━━━━━━━━━━━━━━━━")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD", cwd=PROJECT_ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        commit = out.decode().strip()
        if proc.returncode == 0 and commit:
            lines.append(f"📦 Running commit: <code>{html.escape(commit)}</code>")
    except (OSError, asyncio.TimeoutError):
        logging.debug("Commit lookup skipped", exc_info=True)

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed bot status"""
    user_id = update.effective_user.id if update.effective_user else None
    is_admin = is_user_admin(user_id)

    use_oauth = studip_oauth.oauth_enabled()
    auth_label = "OAuth2 (StudyLoft)" if use_oauth else "Self-Healing Cookie"

    public_login = _login_public_url()
    if public_login.startswith("https://"):
        login_btn = InlineKeyboardButton(
            "🔐 Login Stud.IP",
            web_app=WebAppInfo(url=_get_login_server().app_url()),
        )
    else:
        login_btn = InlineKeyboardButton("🔐 Login Stud.IP", callback_data="studip_login")

    # ── Non-admin student dashboard ───────────────────────────────────────────
    if not is_admin:
        sess = get_user_session(user_id)
        user_logged_in = await sess.is_logged_in()
        studip_status = f"✅ Connected ({auth_label})" if user_logged_in else "❌ Not connected"

        user_info = db.get_user(user_id) if user_id else None
        db_studip = (user_info.get("studip_username") if user_info else "") or ""
        profile_name = db_studip
        if user_logged_in:
            try:
                prof = await get_studip_user_profile(user_id=user_id, session=sess)
                profile_name = prof.get("formatted_name") or prof.get("given_name") or prof.get("username") or db_studip
            except Exception:
                logging.debug("Ignored a non-critical error", exc_info=True)

        default_semester = load_default_semester(user_id)
        default_semester_label = html.escape(semester_display(default_semester.get("label") or "Not set"))
        weather_location = load_weather_location(user_id)
        weather_location_label = (html.escape(f"{weather_location['name']}, {weather_location['country']}")
                                  if weather_location else "Uni Oldenburg (default)")
        study_plan_enabled = get_pref(user_id, "weekly_study_plan")

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "🎓 <b>Student Account Status</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Stud.IP Account:</b> {html.escape(profile_name or 'Not linked')}\n"
            f"🔑 <b>Connection:</b> {studip_status}\n"
            f"📆 <b>Current Semester:</b> {default_semester_label}\n"
            f"📍 <b>Campus / Weather:</b> {weather_location_label}\n"
            f"📘 <b>Weekly Study Plan:</b> {'✅ Enabled' if study_plan_enabled else '🔲 Disabled'}\n"
        )
        if not user_logged_in:
            text += "\n💡 <i>Connect your Stud.IP account below to access your personal courses, files, and deadlines.</i>"

        keyboard = []
        if user_logged_in:
            keyboard.append([
                login_btn,
                InlineKeyboardButton("🚪 Disconnect Stud.IP", callback_data="studip_logout"),
            ])
        else:
            keyboard.append([login_btn])

        keyboard.append([
            InlineKeyboardButton("🎯 Tracked Courses", callback_data="sp_track_menu"),
            InlineKeyboardButton("📆 Semester & Calendar", callback_data="status_group|semester"),
        ])
        keyboard.append([
            InlineKeyboardButton("📄 Documents & Tuition", callback_data="status_group|docs_tuition"),
            InlineKeyboardButton("🌤️ Weather & Location", callback_data="status_group|weather"),
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ── Administrator full system status ──────────────────────────────────────
    is_active = state.global_session and await state.global_session.is_logged_in()
    logged_in = f"✅ Yes ({auth_label})" if is_active else "❌ No"

    # Watcher status
    if state.watcher_controller_running:
        watcher_status = "🟢 Running"
    else:
        watcher_status = "🔴 Stopped"

    wa_group_id = get_wa_group_id()

    wa_status = "🔴 Disconnected (service unreachable)"
    wa_group_label = "⚠️ Not set - use 📱 WhatsApp -> 🔍 Detect WA Groups" if not wa_group_id else wa_group_id
    try:
        async with shared_http() as session:
            async with session.get(f"{WA_SERVICE_URL}/status", headers=WA_HEADERS, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("isAuthenticated"):
                        wa_status = "🟢 Connected"
                    elif data.get("hasEverConnected") or data.get("isReconnecting"):
                        wa_status = "🔴 Disconnected (session dropped, reconnecting...)"
                    else:
                        wa_status = "🟡 Waiting for first QR scan"
            if wa_group_id:
                resolved = False
                try:
                    async with session.get(f"{WA_SERVICE_URL}/group_name", headers=WA_HEADERS, params={"groupId": wa_group_id}, timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("name"):
                                wa_group_label = data["name"]
                                resolved = True
                except Exception:
                    logging.debug("WhatsApp group name request failed", exc_info=True)
                if not resolved:
                    async with session.get(f"{WA_SERVICE_URL}/discovered_groups", headers=WA_HEADERS, timeout=2) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for g in data.get("groups", []):
                                if g.get("groupId") == wa_group_id and g.get("name"):
                                    wa_group_label = g["name"]
                                    break
    except Exception:
        logging.debug("WhatsApp status request failed", exc_info=True)

    text = (
        "━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🔑 Logged in: {logged_in}\n"
        f"👀 Unified Watcher: {watcher_status}\n"
        f"🔁 Global Scans: {state.global_scan_count}\n"
        f"📱 WA Group: {html.escape(str(wa_group_label))}\n"
        f"💬 WA Status: {wa_status}"
    )

    # 💾 System resource information
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 ** 2
    total_mem = psutil.virtual_memory()
    sys_cpu = await asyncio.to_thread(psutil.cpu_percent, 0.3)

    sys_info = (
        f"💾 <b>Bot Memory:</b> {mem_mb:.1f} MB\n"
        f"💽 <b>System RAM:</b> {total_mem.percent:.1f}%\n"
        f"🧠 <b>System CPU:</b> {sys_cpu:.1f}%"
    )

    if mem_mb > 500:
        sys_info += "\n\n⚠️ <b>High memory usage detected - bot restart recommended.</b>"

    # Last full check time
    if state.last_full_check_time:
        since = berlin_now() - state.last_full_check_time
        hours, remainder = divmod(int(since.total_seconds()), 3600)
        minutes = remainder // 60
        text += f"\n\n🕓 Last Full Check: {state.last_full_check_time.strftime('%d %b %Y %H:%M')} ({hours}h {minutes}m ago)"
    else:
        text += "\n\n🕓 Last Full Check: -"

    text += f"\n\n{sys_info}"

    default_semester = load_default_semester(user_id)
    default_semester_label = html.escape(semester_display(default_semester.get("label") or "Not set (always asks)"))
    text += f"\n\n📆 Default Semester: {default_semester_label}"

    watch_prev = get_pref(user_id, "watch_previous_semester")
    text += f"\n👀 Watch Previous Semester Too: {'✅ On' if watch_prev else '🔲 Off'}"

    studip_name = None
    try:
        profile = await get_studip_user_profile()
        studip_name = profile.get("formatted_name") or profile.get("given_name")
    except Exception:
        logging.debug("Stud.IP profile lookup failed", exc_info=True)
    if not studip_name:
        studip_name = get_user_name(user_id)
    text += f"\n👤 Name: {html.escape(studip_name or 'Not connected')} (from Stud.IP)"

    weather_location = load_weather_location(user_id)
    weather_location_label = (html.escape(f"{weather_location['name']}, {weather_location['country']}")
                              if weather_location else "Uni Oldenburg (default)")
    text += f"\n📍 Weather Location: {weather_location_label}"

    wardrobe_enabled = bool(get_pref(user_id, "outfit"))
    text += f"\n👔 Daily Outfit Suggestion: {'✅ On' if wardrobe_enabled else '🔲 Off'}"

    study_plan_enabled = get_pref(user_id, "weekly_study_plan")
    text += f"\n📘 Weekly Study Plan: {'✅ On' if study_plan_enabled else '🔲 Off'}"

    row_tools = [
        InlineKeyboardButton("👔 Wardrobe & Weather", callback_data="outfit_menu"),
        InlineKeyboardButton("📱 WhatsApp Gateway", callback_data="wa_menu"),
    ]

    keyboard = [
        [login_btn, InlineKeyboardButton("🚪 Logout", callback_data="studip_logout")],
        [InlineKeyboardButton("📄 Documents & Tuition", callback_data="status_group|docs_tuition"), InlineKeyboardButton("📆 Semester & Calendar", callback_data="status_group|semester")],
        row_tools,
        [InlineKeyboardButton("🎯 Tracked Courses", callback_data="sp_track_menu"), InlineKeyboardButton("👤 Profile", callback_data="status_group|profile")],
        [InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_menu")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)



def _format_time_ago(ts: float) -> str:
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    elif diff < 3600:
        return f"{int(diff // 60)}m ago"
    elif diff < 86400:
        return f"{int(diff // 3600)}h ago"
    else:
        return f"{int(diff // 86400)}d ago"


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin dashboard command."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ This command is restricted to administrators.")
        return
    text, reply_markup = _build_admin_overview_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


def _build_admin_overview_view() -> tuple[str, InlineKeyboardMarkup]:
    stats = db.get_user_analytics()
    text = (
        "👑 <b>UniOldenburg Admin Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Registered:</b> <code>{stats['total_users']}</code> students\n"
        f"🟢 <b>Active Accounts:</b> <code>{stats['active_users']}</code>\n"
        f"📅 <b>Daily Active (DAU):</b> <code>{stats['dau']}</code>\n"
        f"📆 <b>Weekly Active (WAU):</b> <code>{stats['wau']}</code>\n"
        f"🎫 <b>Pending Invite Codes:</b> <code>{stats['pending_invites']}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>Most Active Students:</b>\n"
    )
    if stats["top_users"]:
        for idx, u in enumerate(stats["top_users"], 1):
            # Telegram names are chosen by the student: never markup
            name = html.escape(str(u["username"] or u["full_name"] or u["telegram_id"]))
            ago = _format_time_ago(u["last_active_at"])
            text += f"{idx}. <b>@{name}</b> - {u['interactions']} actions ({ago})\n"
    else:
        text += "<i>No activity records yet.</i>\n"

    text += "\n⚡ <b>Recent Activities:</b>\n"
    if stats["recent_activities"]:
        for a in stats["recent_activities"]:
            name = html.escape(str(a["username"] or a["full_name"] or a["telegram_id"]))
            ago = _format_time_ago(a["created_at"])
            # the action holds what the student typed
            text += f"• <b>@{name}:</b> <code>{html.escape(str(a['action'] or ''))}</code> ({ago})\n"
    else:
        text += "<i>No recent activity.</i>\n"

    keyboard = [
        [
            InlineKeyboardButton("👥 User Directory", callback_data="admin_users"),
            InlineKeyboardButton("🔑 Invite Codes", callback_data="admin_invites"),
        ],
        [
            InlineKeyboardButton("➕ New Invite Code", callback_data="admin_new_invite"),
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_prompt"),
        ],
        [
            InlineKeyboardButton("⚡ System Health", callback_data="admin_system_health"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin_menu"),
        ],
        [
            InlineKeyboardButton("♻️ Restart Bot", callback_data="admin_restart_prompt"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast an announcement to all registered students (Admin only)."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ This command is restricted to administrators.")
        return

    text = " ".join(context.args).strip() if context.args else ""
    if text:
        announcement = (
            "📢 <b>Announcement from Administrator</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(text)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Sent to all registered students.</i>"
        )
        result = await broadcast(
            context.bot, announcement, parse_mode="HTML", disable_notification=False,
            sender_user_id=user.id,
        )
        sender_current = _admin_authority_is_current(user.id)
        outcome = "revoked" if result.get("sender_revoked") or not sender_current else (
            "failed" if result["failed"] else "success"
        )
        log_security_event("admin_broadcast", actor_id=user.id, outcome=outcome)
        if result.get("sender_revoked") or not sender_current:
            return
        sent_count = len(result["sent"])
        failed_count = len(result["failed"])
        if failed_count:
            reply = (f"⚠️ Announcement delivered to {sent_count} student(s); "
                     f"it failed for {failed_count} student(s). Check the bot logs for details.")
        else:
            reply = f"✅ Announcement delivered to {sent_count} active student(s)."
        await update.message.reply_text(reply, parse_mode="HTML")
    else:
        user_data(context)["pending_step"] = "admin_broadcast_text"
        await update.message.reply_text(
            "📢 <b>Broadcast Announcement</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please send the message you wish to broadcast to all registered students (or send /cancel):",
            parse_mode="HTML",
        )


INVITE_MAX_USES = 200
INVITE_MAX_DAYS = 365
INVITE_USAGE = (
    "🔑 <b>Invites</b>\n"
    "<code>/invite</code> or <code>/invite &lt;label&gt;</code>: one student, 7 days\n"
    "<code>/invite create &lt;uses&gt; [days] [label]</code>: one code for several students\n"
    "<code>/invite list</code>: codes still usable\n"
    "<code>/invite revoke &lt;code&gt;</code>: stop a code at once"
)


def parse_invite_args(args: list) -> dict:
    """{"action": "create"|"list"|"revoke"|"help", ...} from /invite arguments;
    ValueError with a message for bad numbers."""
    if not args:
        return {"action": "create", "uses": 1, "days": 7.0, "label": None}
    head = args[0].lower()
    if head == "list":
        return {"action": "list"}
    if head == "revoke":
        if len(args) < 2:
            raise ValueError("Which code? /invite revoke UNI-ABC-123")
        return {"action": "revoke", "code": args[1]}
    if head in ("help", "?"):
        return {"action": "help"}
    if head == "create":
        rest = list(args[1:])
        uses, days = 1, 7.0
        if rest and rest[0].isdigit():
            uses = int(rest.pop(0))
        if rest and rest[0].replace(".", "", 1).isdigit():
            days = float(rest.pop(0))
        if not 1 <= uses <= INVITE_MAX_USES:
            raise ValueError(f"Uses must be 1 to {INVITE_MAX_USES}.")
        if not 0 < days <= INVITE_MAX_DAYS:
            raise ValueError(f"Days must be more than 0 and at most {INVITE_MAX_DAYS}.")
        return {"action": "create", "uses": uses, "days": days, "label": " ".join(rest).strip() or None}
    return {"action": "create", "uses": 1, "days": 7.0, "label": " ".join(args).strip()}


def _invite_list_text(tokens: list) -> str:
    if not tokens:
        return "🔑 No usable invite codes. Make one with <code>/invite create 5 14</code>."
    lines = ["🔑 <b>Usable invite codes</b>", ""]
    for t in tokens:
        left = max(0, (t.get("max_uses") or 1) - (t.get("use_count") or 0))
        days = max(0.0, (t["expires_at"] - time.time()) / 86400)
        label = f" · {html.escape(t['label'])}" if t.get("label") else ""
        lines.append(f"<code>{t['code']}</code>: {left} left, {days:.0f} days{label}")
    return "\n".join(lines)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admins: create (single-use or with a quota), list and revoke invite codes."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ This command is restricted to administrators.")
        return
    try:
        req = parse_invite_args(list(context.args or []))
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {html.escape(str(e))}\n\n{INVITE_USAGE}", parse_mode="HTML")
        return
    if req["action"] == "help":
        await update.message.reply_text(INVITE_USAGE, parse_mode="HTML")
        return
    if req["action"] == "list":
        tokens = [t for t in db.list_invite_tokens(active_only=True)
                  if (t.get("use_count") or 0) < (t.get("max_uses") or 1)]
        await update.message.reply_text(_invite_list_text(tokens), parse_mode="HTML")
        return
    if req["action"] == "revoke":
        ok = db.revoke_invite_token(req["code"])
        await update.message.reply_text("🚫 Revoked: nobody can use it any more." if ok else "⚠️ No such code.")
        return

    label, uses, days = req["label"], req["uses"], req["days"]
    code = db.create_invite_token(created_by=user.id, label=label, expires_days=days, max_uses=uses)
    bot_me = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_me.username}?start=inv_{code.replace('-', '').lower()}"
    usage = "single-use" if uses == 1 else f"for {uses} students"
    text = (
        "🔑 <b>New Invite Code</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎫 <b>Access Code:</b> <code>{code}</code>\n"
        f"👤 <b>Label:</b> <code>{html.escape(label or 'Unlabeled')}</code>\n"
        f"⏳ <b>Validity:</b> {days:g} days ({usage})\n\n"
        "🔗 <b>One-Tap Invite Link:</b>\n"
        f"{deep_link}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Share this code or link. /invite list shows what is left, /invite revoke stops it.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


def _is_protected_admin(user_id: int) -> bool:
    """Administrators set in the environment (and the primary admin): no other
    admin can block them, change their role or revoke their session."""
    return (user_id in (ADMIN_USER_IDS or set()) or user_id in (ALLOWED_USER_IDS or set())
            or user_id == PRIMARY_ADMIN_ID)


def _admin_authority_is_current(user_id: int) -> bool:
    """Recheck the actor's current administrator role after an awaited operation."""
    return is_user_admin(user_id)


def _build_user_detail_view(target_id: int, viewer_id: int):
    """The admin dashboard's view of one user, or None when the user is gone."""
    target = db.get_user(target_id)
    if not target:
        return None
    name = target["username"] or target["full_name"] or str(target_id)
    role = target.get("role", "user")
    is_active = target.get("is_active", True)
    studip_user = target.get("studip_username") or "Not linked"
    reg_time = datetime.fromtimestamp(target.get("created_at", time.time()), TZ_BERLIN).strftime("%d.%m.%Y %H:%M")
    last_active = _format_time_ago(target.get("last_active_at", time.time()))

    has_active_session = target_id in state.user_sessions
    session_badge = "🟢 In-memory Session Active" if has_active_session else "⚪ No active memory session"

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Student Profile: @{html.escape(str(name))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Telegram ID:</b> <code>{target_id}</code>\n"
        f"📛 <b>Full Name:</b> {html.escape(target.get('full_name') or 'N/A')}\n"
        f"🎓 <b>Stud.IP Account:</b> <code>{html.escape(str(studip_user))}</code>\n"
        f"👑 <b>Role:</b> {'👑 Administrator' if role == 'admin' else '🎓 Student'}\n"
        f"📶 <b>Account Status:</b> {'🟢 Active' if is_active else '🔴 Blocked'}\n"
        f"⚡ <b>Session State:</b> {session_badge}\n"
        f"📊 <b>Interactions:</b> {target.get('interaction_count', 0)} commands\n"
        f"🕓 <b>Last Active:</b> {last_active}\n"
        f"📅 <b>Registered:</b> {reg_time}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    keyboard = []
    protected = _is_protected_admin(target_id)
    if not protected and target_id != viewer_id:
        block_label = "🚫 Block Account" if is_active else "✅ Activate Account"
        keyboard.append([InlineKeyboardButton(block_label, callback_data=f"admin_toggle_user|{target_id}")])

        role_label = "🎓 Demote to Student" if role == "admin" else "👑 Promote to Admin"
        keyboard.append([InlineKeyboardButton(role_label, callback_data=f"admin_role_user|{target_id}")])

    if not protected or target_id == viewer_id:
        keyboard.append([InlineKeyboardButton("🚪 Revoke Stud.IP Session", callback_data=f"admin_revoke_user|{target_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Users", callback_data="admin_users")])
    return text, InlineKeyboardMarkup(keyboard)


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback router for Admin Dashboard. The callback is answered exactly
    once: Telegram rejects a second answer, so an alert must be the only one."""
    query = update.callback_query
    user = query.from_user
    if not user or not is_user_admin(user.id):
        log_security_event("admin_callback", actor_id=getattr(user, "id", None), outcome="denied")
        await query.answer("⛔ Access denied. Admin-only feature.", show_alert=True)
        return

    data = query.data or ""
    notice = None

    # actions on one user redraw that user's detail view afterwards
    action, _, raw_target = data.partition("|")
    if action in ("admin_toggle_user", "admin_role_user", "admin_revoke_user"):
        try:
            target_id = int(raw_target)
        except ValueError:
            await query.answer("Unknown user.", show_alert=True)
            return
        if action == "admin_revoke_user":
            if _is_protected_admin(target_id) and target_id != user.id:
                await query.answer("Cannot revoke this administrator's session.", show_alert=True)
                return
            await _perform_logout(user_id=target_id)
            log_security_event("admin_session_revoked", actor_id=user.id, subject_id=target_id)
            if not _admin_authority_is_current(user.id):
                return
            notice = "Stud.IP session revoked and credentials cleared."
        else:
            if _is_protected_admin(target_id) or target_id == user.id:
                await query.answer("Cannot modify this administrator." if action == "admin_toggle_user"
                                   else "Cannot change this administrator's role.", show_alert=True)
                return
            target = db.get_user(target_id)
            if target and action == "admin_toggle_user":
                new_state = not target["is_active"]
                db.set_user_active(target_id, new_state)
                if not new_state:
                    # Blocking an account must also stop already-running or
                    # restart-persistent enrollment work and revoke its Mini
                    # App cookies. Keep stored credentials for a later unblock.
                    if state.login_server is not None:
                        state.login_server.revoke_user(target_id)
                    from fast_enroll import clear_pending_job
                    from unibot.schedule import fastenroll_tasks

                    clear_pending_job(target_id)
                    for task in (fastenroll_tasks.pop(target_id, None) or {}).values():
                        task.cancel()
                log_security_event(
                    "admin_account_state_changed", actor_id=user.id, subject_id=target_id,
                )
                notice = f"User {'activated' if new_state else 'blocked'}."
            elif target:
                new_role = "user" if target["role"] == "admin" else "admin"
                db.set_user_role(target_id, new_role)
                log_security_event("admin_role_changed", actor_id=user.id, subject_id=target_id)
                notice = f"Role updated to {new_role}."
        data = f"admin_user_detail|{target_id}"

    if data.startswith("admin_user_detail|"):
        try:
            view = _build_user_detail_view(int(data.split("|")[1]), user.id)
        except ValueError:
            view = None
        if view is None:
            await query.answer("User not found.", show_alert=True)
            return
        await query.answer(notice, show_alert=bool(notice))
        if not _admin_authority_is_current(user.id):
            return
        await query.edit_message_text(view[0], parse_mode="HTML", reply_markup=view[1])
        return

    await query.answer()
    if not _admin_authority_is_current(user.id):
        return

    if data == "admin_menu":
        # This is also the Cancel button on the pending broadcast prompt.
        user_data(context).pop("pending_step", None)
        text, reply_markup = _build_admin_overview_view()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return

    if data == "admin_users":
        users = db.get_all_users()
        lines = [
            "👥 <b>Registered Students & Users</b>",
            "━━━━━━━━━━━━━━━━━━",
            "Tap a student to view details, toggle status, change role, or revoke session:\n",
        ]
        keyboard = []
        for u in users:
            raw_name = str(u["username"] or u["full_name"] or u["telegram_id"])
            name = html.escape(raw_name)
            role_badge = "👑 Admin" if u["role"] == "admin" else "🎓 Student"
            status_icon = "🟢" if u["is_active"] else "🔴"
            ago = _format_time_ago(u["last_active_at"])
            lines.append(
                f"{status_icon} <b>@{name}</b> (<code>{u['telegram_id']}</code>)\n"
                f"   Role: {role_badge} | Actions: {u['interaction_count']} | Last: {ago}"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"{status_icon} @{raw_name} ({role_badge})"[:64],
                    callback_data=f"admin_user_detail|{u['telegram_id']}",
                )
            ])

        keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_invites":
        invites = db.list_invite_tokens(active_only=False)[:10]
        lines = [
            "🔑 <b>Invite Codes (Last 10)</b>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        if invites:
            for inv in invites:
                status = "✅ Used" if inv["is_used"] else ("⏳ Pending" if inv["expires_at"] > time.time() else "⌛ Expired")
                lbl = f" ({html.escape(inv['label'])})" if inv['label'] else ""
                lines.append(f"🎫 <code>{inv['code']}</code>{lbl} - {status}")
        else:
            lines.append("<i>No invite codes generated yet.</i>")

        keyboard = [
            [InlineKeyboardButton("➕ Generate New Code", callback_data="admin_new_invite")],
            [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")],
        ]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_new_invite":
        code = db.create_invite_token(created_by=user.id, label="Dashboard Generated", expires_days=7.0)
        bot_me = await context.bot.get_me()
        if not _admin_authority_is_current(user.id):
            return
        deep_link = f"https://t.me/{bot_me.username}?start=inv_{code.replace('-', '').lower()}"
        text = (
            "✅ <b>New Invite Code Generated</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🎫 <b>Code:</b> <code>{code}</code>\n"
            f"🔗 <b>Quick Link:</b>\n{deep_link}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        keyboard = [
            [InlineKeyboardButton("🔑 All Codes", callback_data="admin_invites")],
            [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")],
        ]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_broadcast_prompt":
        user_data(context)["pending_step"] = "admin_broadcast_text"
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]
        await query.edit_message_text(
            "📢 <b>Broadcast Announcement</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Please send the message you wish to broadcast to all registered students:\n\n"
            "<i>Type your announcement in chat or tap Cancel below.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "admin_system_health":
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        disk = shutil.disk_usage("/")
        text = (
            "⚡ <b>System & Server Health</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💻 <b>CPU Usage:</b> <code>%{cpu}</code>\n"
            f"🧠 <b>RAM:</b> <code>{mem.used // (1024*1024)}MB / {mem.total // (1024*1024)}MB</code> (%{mem.percent})\n"
            f"💾 <b>Disk:</b> <code>{disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB</code>\n"
            f"🌐 <b>Cloudflare Tunnel:</b> <code>{'Online' if state.quick_tunnel and state.quick_tunnel.url else 'Connecting' if state.quick_tunnel else 'Direct / Off'}</code>\n"
            f"🔄 <b>Watcher Task:</b> <code>{'Running' if state.unified_watcher_task and not state.unified_watcher_task.done() else 'Stopped'}</code>\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_restart_prompt":
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Restart Now", callback_data="admin_restart_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")],
        ]
        await query.edit_message_text("⚠️ <b>Restart the bot service?</b>\nThis takes ~2-3 seconds.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_restart_confirm":
        await query.edit_message_text("♻️ <b>Restarting bot...</b>", parse_mode="HTML")
        if not _admin_authority_is_current(user.id):
            return
        state.spawn(_restart_process(), name="restart")
        return
