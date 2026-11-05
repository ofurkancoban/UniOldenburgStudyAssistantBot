"""Health checks, /status, /healthcheck and self-restart.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import logging
import os
import shutil
import subprocess
import sys
import time

import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

import studip_oauth
from studip_utils import berlin_now

from unibot import db, state
from unibot.config import (
    PROJECT_ROOT,
    WA_HEADERS,
    WA_SERVICE_URL,
    get_wa_group_id,
)
from unibot.storage import (
    get_primary_user_name,
    get_wardrobe_feature_enabled,
    get_watch_previous_semester_enabled,
    get_weekly_study_plan_enabled,
    link_cache,
    load_default_semester,
    shared_http,
)
from unibot.auth import (
    _get_login_server,
    _login_public_url,
    is_user_admin,
    is_user_allowed,
    login_studip,
    release_instance_lock,
)
from unibot.studip_api import get_studip_user_profile, list_courses
from unibot.ai import check_gwdg_health, check_openrouter_health
from unibot.weather import check_weather_health, load_weather_location


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the bot process itself (and cleanly shut down the WhatsApp microservice first)."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_admin(user_id):
        await update.message.reply_text("⛔ Bu komut yalnızca yöneticilere özeldir.")
        return

    await update.message.reply_text("♻️ Restarting bot...")
    logging.info("Restart requested by user %s.", user_id)
    asyncio.create_task(_restart_process())


async def _restart_process():
    from unibot.app import stop_whatsapp_microservice
    # Give the reply above a moment to actually reach Telegram before we tear things down.
    await asyncio.sleep(0.5)

    stop_whatsapp_microservice()
    link_cache.flush()

    # os.execv replaces the current process image in place (same PID), which
    # works the same whether pm2 is managing us or not - pm2 just sees its
    # monitored PID stay alive and keeps watching it, no restart-detection
    # needed. execv also skips atexit handlers, which is why the WhatsApp
    # subprocess is stopped explicitly above.
    #
    # The lock file must still be released here even though the PID will be
    # identical after execv: acquire_instance_lock() only checks "does a
    # process with this PID exist", and since our own PID trivially exists,
    # a stale lock left in place makes the re-exec'd process think another
    # instance is already running and refuse to start.
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
    """(ok, detail) — logs in (or confirms the cached session still works)
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
    """(ok, detail) — asks the local WhatsApp microservice for its own
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
    for display names). Each check is independently best-effort — one
    failing never blocks or skews the others."""
    studip = await check_studip_health()
    whatsapp = await check_whatsapp_health()
    weather = await check_weather_health()
    openrouter = await check_openrouter_health(live=live_openrouter)
    gwdg = await check_gwdg_health(live=live_openrouter)
    disk = check_disk_health()
    return {"studip": studip, "whatsapp": whatsapp, "weather": weather, "openrouter": openrouter, "gwdg": gwdg, "disk": disk}


async def healthcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /healthcheck — an on-demand, full (live OpenRouter call
    included) check of every subsystem the bot depends on, plus the
    currently-running git commit, so "is everything actually working
    right now" is a single command instead of SSHing into the server."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return

    msg = await update.message.reply_text("🩺 Running health check...")
    results = await run_health_checks(live_openrouter=True)

    lines = ["🩺 <b>Health Check</b>", "━━━━━━━━━━━━━━━━━"]
    for key, (ok, detail) in results.items():
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} {HEALTH_CHECK_LABELS[key]}: {html.escape(detail)}")
    lines.append("━━━━━━━━━━━━━━━━━")

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        lines.append(f"📦 Running commit: <code>{commit}</code>")
    except (OSError, subprocess.SubprocessError):
        pass

    await msg.edit_text("\n".join(lines), parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed bot status"""
    user_id = update.effective_user.id if update.effective_user else None

    use_oauth = studip_oauth.oauth_enabled()
    auth_label = "OAuth2 (StudyLoft)" if use_oauth else "Self-Healing Cookie"
    is_active = state.global_session and await state.global_session.is_logged_in()
    logged_in = f"✅ Yes ({auth_label})" if is_active else "❌ No"

    # Watcher durumu
    if state.watcher_controller_running:
        watcher_status = "🟢 Running"
    else:
        watcher_status = "🔴 Stopped"

    wa_group_id = get_wa_group_id()

    # Check WA Connection Status, and resolve the active group's display
    # name (the group is always set by exact ID via "🔍 Detect WA Groups"
    # now — this is just a friendlier label for it here). Tries /group_name
    # first (asks WhatsApp directly by ID via getChatById, so it works even
    # right after a service restart), falling back to the discovered-groups
    # log (names seen via message_create) if that's unreachable.
    wa_status = "🔴 Disconnected (service unreachable)"
    wa_group_label = "⚠️ Not set — use 📱 WhatsApp → 🔍 Detect WA Groups" if not wa_group_id else wa_group_id
    try:
        async with shared_http() as session:
            async with session.get(f"{WA_SERVICE_URL}/status", headers=WA_HEADERS, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("isAuthenticated"):
                        wa_status = "🟢 Connected"
                    elif data.get("hasEverConnected") or data.get("isReconnecting"):
                        # Was connected before (or is mid-reconnect) - a real
                        # session drop, not first-time setup. See the
                        # proactive 🔴/🟢 Telegram alerts in server.js for
                        # when this actually changed.
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
        f"📱 WA Group: {wa_group_label}\n"
        f"💬 WA Status: {wa_status}"
    )

    # 💾 System resource information
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 ** 2
    total_mem = psutil.virtual_memory()
    # cpu_percent(interval=...) sleeps, so sample it off the event loop
    sys_cpu = await asyncio.to_thread(psutil.cpu_percent, 0.3)

    sys_info = (
        f"💾 <b>Bot Memory:</b> {mem_mb:.1f} MB\n"
        f"💽 <b>System RAM:</b> {total_mem.percent:.1f}%\n"
        f"🧠 <b>System CPU:</b> {sys_cpu:.1f}%"
    )

    # High RAM warning
    if mem_mb > 500:
        sys_info += "\n\n⚠️ <b>High memory usage detected — bot restart recommended.</b>"

    # Last full check time
    if state.last_full_check_time:
        since = berlin_now() - state.last_full_check_time
        hours, remainder = divmod(int(since.total_seconds()), 3600)
        minutes = remainder // 60
        text += f"\n\n🕓 Last Full Check: {state.last_full_check_time.strftime('%d %b %Y %H:%M')} ({hours}h {minutes}m ago)"
    else:
        text += "\n\n🕓 Last Full Check: —"

    text += f"\n\n{sys_info}"

    default_semester = load_default_semester()
    default_semester_label = default_semester.get("label") or "Not set (always asks)"
    text += f"\n\n📆 Default Semester: {default_semester_label}"

    watch_prev = get_watch_previous_semester_enabled()
    text += f"\n👀 Watch Previous Semester Too: {'✅ On' if watch_prev else '🔲 Off'}"

    studip_name = None
    try:
        profile = await get_studip_user_profile()
        studip_name = profile.get("formatted_name") or profile.get("given_name")
    except Exception:
        logging.debug("Stud.IP profile lookup failed", exc_info=True)
    if not studip_name:
        studip_name = get_primary_user_name()
    text += f"\n👤 Name: {studip_name or 'Not connected'} (from Stud.IP)"

    weather_location = load_weather_location()
    weather_location_label = f"{weather_location['name']}, {weather_location['country']}" if weather_location else "Uni Oldenburg (default)"
    text += f"\n📍 Weather Location: {weather_location_label}"

    wardrobe_enabled = get_wardrobe_feature_enabled()
    is_admin = is_user_admin(user_id)
    if is_admin:
        text += f"\n👔 Daily Outfit Suggestion: {'✅ On' if wardrobe_enabled else '🔲 Off'}"

    study_plan_enabled = get_weekly_study_plan_enabled()
    text += f"\n📘 Weekly Study Plan: {'✅ On' if study_plan_enabled else '🔲 Off'}"

    public_login = _login_public_url()
    if public_login.startswith("https://"):
        login_btn = InlineKeyboardButton(
            "🔐 Login",
            web_app=WebAppInfo(url=_get_login_server().app_url()),
        )
    else:
        login_btn = InlineKeyboardButton("🔐 Login", callback_data="studip_login")

    row_tools = []
    if is_admin:
        row_tools.append(InlineKeyboardButton("👔 Wardrobe & Weather", callback_data="outfit_menu"))
        row_tools.append(InlineKeyboardButton("📱 WhatsApp Gateway", callback_data="wa_menu"))
    else:
        row_tools.append(InlineKeyboardButton("🌤️ Weather & Location", callback_data="status_group|weather"))

    keyboard = [
        [login_btn, InlineKeyboardButton("🚪 Logout", callback_data="studip_logout")],
        [InlineKeyboardButton("📄 Documents & Tuition", callback_data="status_group|docs_tuition"), InlineKeyboardButton("📆 Semester & Calendar", callback_data="status_group|semester")],
        row_tools,
        [InlineKeyboardButton("🎯 Tracked Courses", callback_data="sp_track_menu"), InlineKeyboardButton("👤 Profile", callback_data="status_group|profile")],
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


def _format_time_ago(ts: float) -> str:
    diff = time.time() - ts
    if diff < 60:
        return "şimdi"
    elif diff < 3600:
        return f"{int(diff // 60)} dk önce"
    elif diff < 86400:
        return f"{int(diff // 3600)} sa önce"
    else:
        return f"{int(diff // 86400)} gün önce"


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin dashboard command."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ Bu komut yalnızca yöneticilere özeldir.")
        return
    text, reply_markup = _build_admin_overview_view()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


def _build_admin_overview_view() -> tuple[str, InlineKeyboardMarkup]:
    stats = db.get_user_analytics()
    text = (
        "👑 <b>UniOldenburg Yönetim Paneli</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Toplam Kayıtlı:</b> <code>{stats['total_users']}</code> öğrenci\n"
        f"🟢 <b>Aktif Hesaplar:</b> <code>{stats['active_users']}</code>\n"
        f"📅 <b>Bugün Aktif (DAU):</b> <code>{stats['dau']}</code>\n"
        f"📆 <b>Bu Hafta Aktif (WAU):</b> <code>{stats['wau']}</code>\n"
        f"🎫 <b>Bekleyen Davet Kodları:</b> <code>{stats['pending_invites']}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>En Aktif Öğrenciler:</b>\n"
    )
    if stats["top_users"]:
        for idx, u in enumerate(stats["top_users"], 1):
            name = u["username"] or u["full_name"] or str(u["telegram_id"])
            ago = _format_time_ago(u["last_active_at"])
            text += f"{idx}. <b>@{name}</b> - {u['interactions']} işlem ({ago})\n"
    else:
        text += "<i>Henüz aktivite kaydı yok.</i>\n"

    text += "\n⚡ <b>Son Aktiviteler:</b>\n"
    if stats["recent_activities"]:
        for a in stats["recent_activities"]:
            name = a["username"] or a["full_name"] or str(a["telegram_id"])
            ago = _format_time_ago(a["created_at"])
            text += f"• <b>@{name}:</b> <code>{a['action']}</code> ({ago})\n"
    else:
        text += "<i>Son aktivite bulunmuyor.</i>\n"

    keyboard = [
        [
            InlineKeyboardButton("👥 Kullanıcı Listesi", callback_data="admin_users"),
            InlineKeyboardButton("🔑 Davet Kodları", callback_data="admin_invites"),
        ],
        [
            InlineKeyboardButton("➕ Yeni Davet Kodu", callback_data="admin_new_invite"),
            InlineKeyboardButton("⚡ Sistem Sağlığı", callback_data="admin_system_health"),
        ],
        [
            InlineKeyboardButton("🔄 Yenile", callback_data="admin_menu"),
            InlineKeyboardButton("♻️ Botu Yeniden Başlat", callback_data="admin_restart_prompt"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a new personalized invite code."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ Bu komut yalnızca yöneticilere özeldir.")
        return

    label = " ".join(context.args).strip() if context.args else None
    code = db.create_invite_token(created_by=user.id, label=label, expires_days=7.0)
    bot_me = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_me.username}?start=inv_{code.replace('-', '').lower()}"

    text = (
        "🔑 <b>Yeni Kişiye Özel Davet Kodu</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎫 <b>Erişim Kodu:</b> <code>{code}</code>\n"
        f"👤 <b>Etiket:</b> <code>{label or 'İsimsiz'}</code>\n"
        "⏳ <b>Geçerlilik Süresi:</b> 7 gün (Tek kullanımlık)\n\n"
        "🔗 <b>Tek Tıkla Doğrudan Katılma Linki:</b>\n"
        f"{deep_link}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Bu linki veya kodu öğrenciye iletebilirsiniz.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback router for Admin Dashboard."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not user or not is_user_admin(user.id):
        await query.answer("⛔ Bu panel yalnızca yöneticilere özeldir.", show_alert=True)
        return

    data = query.data

    if data == "admin_menu":
        text, reply_markup = _build_admin_overview_view()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return

    if data == "admin_users":
        users = db.get_all_users()
        lines = [
            "👥 <b>Kayıtlı Öğrenciler & Kullanıcılar</b>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        keyboard = []
        for u in users:
            name = u["username"] or u["full_name"] or str(u["telegram_id"])
            role_badge = "👑 Admin" if u["role"] == "admin" else "🎓 Öğrenci"
            status_badge = "🟢 Aktif" if u["is_active"] else "🔴 Engelli"
            ago = _format_time_ago(u["last_active_at"])
            lines.append(
                f"• <b>@{name}</b> (<code>{u['telegram_id']}</code>)\n"
                f"   Rol: {role_badge} | Durum: {status_badge}\n"
                f"   İşlem: {u['interaction_count']} | Son: {ago}"
            )
            if u["role"] != "admin":
                toggle_btn_text = "🚫 Engelle" if u["is_active"] else "✅ Aktif Et"
                keyboard.append([InlineKeyboardButton(f"{toggle_btn_text} (@{name})", callback_data=f"admin_toggle_user|{u['telegram_id']}")])

        keyboard.append([InlineKeyboardButton("⬅️ Admin Paneline Dön", callback_data="admin_menu")])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("admin_toggle_user|"):
        target_id = int(data.split("|")[1])
        target = db.get_user(target_id)
        if target and target["role"] != "admin":
            new_state = not target["is_active"]
            db.set_user_active(target_id, new_state)
            await query.answer(f"Kullanıcı {'aktif edildi' if new_state else 'engellendi'}.", show_alert=True)

        users = db.get_all_users()
        lines = ["👥 <b>Kayıtlı Öğrenciler & Kullanıcılar</b>", "━━━━━━━━━━━━━━━━━━"]
        keyboard = []
        for u in users:
            name = u["username"] or u["full_name"] or str(u["telegram_id"])
            role_badge = "👑 Admin" if u["role"] == "admin" else "🎓 Öğrenci"
            status_badge = "🟢 Aktif" if u["is_active"] else "🔴 Engelli"
            ago = _format_time_ago(u["last_active_at"])
            lines.append(
                f"• <b>@{name}</b> (<code>{u['telegram_id']}</code>)\n"
                f"   Rol: {role_badge} | Durum: {status_badge}\n"
                f"   İşlem: {u['interaction_count']} | Son: {ago}"
            )
            if u["role"] != "admin":
                toggle_btn_text = "🚫 Engelle" if u["is_active"] else "✅ Aktif Et"
                keyboard.append([InlineKeyboardButton(f"{toggle_btn_text} (@{name})", callback_data=f"admin_toggle_user|{u['telegram_id']}")])
        keyboard.append([InlineKeyboardButton("⬅️ Admin Paneline Dön", callback_data="admin_menu")])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_invites":
        invites = db.list_invite_tokens(active_only=False)[:10]
        lines = [
            "🔑 <b>Davet Kodları (Son 10)</b>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        if invites:
            for inv in invites:
                status = "✅ Kullanıldı" if inv["is_used"] else ("⏳ Bekliyor" if inv["expires_at"] > time.time() else "⌛ Süresi Doldu")
                lbl = f" ({inv['label']})" if inv['label'] else ""
                lines.append(f"🎫 <code>{inv['code']}</code>{lbl} - {status}")
        else:
            lines.append("<i>Henüz üretilmiş davet kodu yok.</i>")

        keyboard = [
            [InlineKeyboardButton("➕ Yeni Kod Üret", callback_data="admin_new_invite")],
            [InlineKeyboardButton("⬅️ Admin Paneline Dön", callback_data="admin_menu")],
        ]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_new_invite":
        code = db.create_invite_token(created_by=user.id, label="Panelden Üretildi", expires_days=7.0)
        bot_me = await context.bot.get_me()
        deep_link = f"https://t.me/{bot_me.username}?start=inv_{code.replace('-', '').lower()}"
        text = (
            "✅ <b>Yeni Davet Kodu Üretildi</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🎫 <b>Kod:</b> <code>{code}</code>\n"
            f"🔗 <b>Hızlı Link:</b>\n{deep_link}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        keyboard = [
            [InlineKeyboardButton("🔑 Tüm Kodlar", callback_data="admin_invites")],
            [InlineKeyboardButton("⬅️ Admin Paneline Dön", callback_data="admin_menu")],
        ]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_system_health":
        mem = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        disk = shutil.disk_usage("/")
        text = (
            "⚡ <b>Sistem & Sunucu Sağlığı</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💻 <b>CPU Kullanımı:</b> <code>%{cpu}</code>\n"
            f"🧠 <b>RAM:</b> <code>{mem.used // (1024*1024)}MB / {mem.total // (1024*1024)}MB</code> (%{mem.percent})\n"
            f"💾 <b>Disk:</b> <code>{disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB</code>\n"
            f"🌐 <b>Cloudflare Tunnel:</b> <code>{'Online' if state.quick_tunnel else 'Direct / Off'}</code>\n"
            f"🔄 <b>Watcher Görevi:</b> <code>{'Çalışıyor' if state.unified_watcher_task and not state.unified_watcher_task.done() else 'Durdu'}</code>\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Admin Paneline Dön", callback_data="admin_menu")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_restart_prompt":
        keyboard = [
            [InlineKeyboardButton("✅ Evet, Yeniden Başlat", callback_data="admin_restart_confirm")],
            [InlineKeyboardButton("❌ İptal", callback_data="admin_menu")],
        ]
        await query.edit_message_text("⚠️ <b>Bot yeniden başlatılsın mı?</b>\nBu işlem ~2-3 saniye sürer.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "admin_restart_confirm":
        await query.edit_message_text("♻️ <b>Bot yeniden başlatılıyor...</b>", parse_mode="HTML")
        asyncio.create_task(_restart_process())
        return
