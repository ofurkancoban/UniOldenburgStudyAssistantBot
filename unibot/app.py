"""Bot startup: handler registration, main loop and the WhatsApp microservice process.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import logging
import os
import signal
import subprocess
import time
from typing import Any

import psutil
import atexit
import socket
from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.error import TelegramError


from unibot import state
from unibot.config import (
    ALLOWED_USER_IDS,
    PROJECT_ROOT,
    TELEGRAM_TOKEN,
    WA_SERVICE_PORT,
    validate_settings,
)
from unibot.storage import close_shared_http, link_cache
from unibot.telegram_ui import TelegramSafetyLimiter, _show_typing
from unibot.auth import (
    _authorization_gate,
    _get_login_server,
    _start_login_infra,
    acquire_instance_lock,
    close_all_sessions,
    pick_watcher_session,
    login_command,
    login_studip,
    logout_command,
    release_instance_lock,
)
from unibot.studip_api import get_studip_user_profile, list_courses
from unibot.documents import handle_file_audio_summary
from unibot.weather import handle_set_weather_location
from unibot.course_registry import handle_course_alerts
from unibot.privacy import handle_privacy, stop_command
from unibot.settings_menu import handle_settings, settings_command
from unibot.study_week import handle_calendar_sync, handle_study_buddy, register_calendar_feed
from unibot.mensa import (
    handle_foodpref_category,
    handle_foodpref_clear_ask,
    handle_foodpref_clear_cancel,
    handle_foodpref_clear_confirm,
    handle_foodpref_remove,
    handle_foodpref_toggle,
    handle_menu_photos_button,
    handle_mensa_location_button,
    handle_menu_preferences_button,
    handle_menu_recommend_button,
    menu_command,
)
from unibot.wardrobe import (
    handle_outfit_menu,
    handle_toggle_wardrobe_feature,
    handle_wardrobe_add_item,
    handle_wardrobe_clear_ask,
    handle_wardrobe_clear_cancel,
    handle_wardrobe_clear_confirm,
    handle_wardrobe_menu,
    handle_wardrobe_remove,
    handle_wardrobe_set_reference,
    handle_wardrobe_suggest_now,
)
from unibot.schedule import (
    activity_command,
    handle_activity_stream,
    handle_calendar_timetable,
    handle_calendar_today,
    handle_calendar_week,
    handle_calendar_weekly,
    handle_upcoming_dashboard,
    timetable_command,
)
from unibot.whatsapp import (
    forward_to_whatsapp,
    handle_forward_wa_ask,
    handle_forward_wa_cancel,
    set_wa_group_handler,
)
from unibot.monitoring import (
    check_command,
    handle_studip_message_reply_cancel,
    handle_studip_message_reply_start,
    handle_voice_message,
    show_last_announcements,
    show_last_files,
    show_last_forum_posts,
    show_last_messages,
    watch,
)
from unibot.exams import (
    documents_command,
    exams_command,
    handle_all_exam_dates,
    handle_exam_buttons,
    handle_exam_dates_list,
    handle_exam_files_scan,
    handle_examopen_cancel,
    handle_examopen_mute_ask,
    handle_examopen_mute_confirm,
    handle_examopen_wa_ask,
    handle_examopen_wa_confirm,
    handle_stums_doc_download,
    handle_stums_documents_menu,
    handle_transcript_summary,
    handle_tuition_status,
    tuition_command,
)
from unibot.study_plan import (
    handle_study_block_feedback,
    handle_study_plan_toggle,
    handle_study_progress_menu,
    handle_study_track_menu,
    handle_toggle_weekly_study_plan,
    progress_command,
    study_plan_command,
    track_courses_command,
)
from unibot.tasks import (
    handle_announcement_to_task,
    handle_task_buttons,
    handle_task_course_pick,
    handle_task_reminder_action,
    handle_task_suggestion_add,
)
from unibot.enrollment import (
    fastenroll_cancel_callback,
    fastenroll_cancel_command,
    fastenroll_command,
    fastenroll_list_command,
    handle_browse_courses,
    handle_browse_my_courses_for_deenroll,
    handle_course_deenroll_ask,
    handle_course_deenroll_cancel,
    handle_course_deenroll_confirm,
    handle_course_enroll_ask,
    handle_course_enroll_cancel,
    handle_course_enroll_confirm,
    handle_course_semester_pick,
    handle_deenroll_semester_pick,
    handle_default_semester_clear,
    handle_default_semester_pick,
    handle_enrollment_menu,
    handle_set_default_semester,
    handle_toggle_prev_sem_scan,
    handle_toggle_watch_prev_sem,
    restore_pending_fastenroll_jobs,
)
from unibot.morning import handle_calendar_today_voice, handle_calendar_weekly_voice
from unibot.voice import handle_photo_message, handle_voice_clarify, handle_voice_confirm, handle_voice_reject
from unibot.system import (
    admin_command,
    broadcast_command,
    handle_admin_callbacks,
    healthcheck_command,
    invite_command,
    restart_command,
    status_command,
)
from unibot.menus import (
    _send_courses_menu,
    handle_change_semester,
    handle_course_noop,
    handle_course_view,
    handle_files_semester_pick,
    handle_reply_buttons,
    handle_selection,
    handle_set_user_name,
    handle_status_buttons,
    handle_status_group,
    menu_button_handler,
    start,
)
from unibot.watcher import start_unified_watcher


start_in_progress: set[int] = set()  # chat_id currently running /start


# ── BEGIN FLOW (login + course list) ──────────────────────────────────────────
async def _run_begin_flow(message, user, context: ContextTypes.DEFAULT_TYPE):
    chat_id = message.chat.id
    if chat_id in start_in_progress:
        return
    start_in_progress.add(chat_id)

    temp_message = None

    try:
        # TEMPORARY MESSAGE
        temp_message = await message.reply_text("🚀 Starting Stud.IP Bot...")
        await _show_typing(message)

        # Loading animasyonu
        steps = [
            "🚀 Starting Stud.IP Bot.",
            "🚀 Starting Stud.IP Bot..",
            "🚀 Starting Stud.IP Bot...",
            "🔐 Logging in...",
            "📚 Fetching courses..."
        ]

        for step in steps:
            await temp_message.edit_text(step)
            await asyncio.sleep(0.5)

        # Actual operations
        await login_studip()
        courses = await list_courses()

        # DELETE TEMPORARY MESSAGE!
        await temp_message.delete()

        # Show course menu
        await _send_courses_menu(message, courses)

    except Exception as e:
        if temp_message:
            try:
                await temp_message.delete()
            except TelegramError:
                pass
        await message.reply_text(f"❌ Error: {str(e)}")

    finally:
        if chat_id in start_in_progress:
            start_in_progress.discard(chat_id)


def register_handlers(app) -> None:
    """Attach every command, callback and message handler to `app`.

    Kept separate from main() so the full handler table can be built and
    checked in tests without logging in or contacting Telegram."""
    # Runs before every other handler (group -1): one allowlist check for
    # all commands, buttons, messages and Mini App data, instead of
    # relying on each handler to remember its own.
    app.add_handler(TypeHandler(Update, _authorization_gate), group=-1)

    # Command & Callback handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("exams", exams_command))
    app.add_handler(CommandHandler("exam", exams_command))
    app.add_handler(CommandHandler("studyplan", study_plan_command))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("trackcourses", track_courses_command))
    app.add_handler(CommandHandler("fastenroll", fastenroll_command))
    app.add_handler(CommandHandler("fastenroll_list", fastenroll_list_command))
    app.add_handler(CommandHandler("fastenroll_cancel", fastenroll_cancel_command))
    app.add_handler(CallbackQueryHandler(fastenroll_cancel_callback, pattern="^fastenroll_cancel\\|.*$"))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("healthcheck", healthcheck_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("timetable", timetable_command))
    app.add_handler(CommandHandler("activity", activity_command))
    app.add_handler(CommandHandler("documents", documents_command))
    app.add_handler(CommandHandler("tuition", tuition_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    app.add_handler(CallbackQueryHandler(handle_admin_callbacks, pattern="^admin_.*$"))
    app.add_handler(CallbackQueryHandler(show_last_messages, pattern="^show_last_messages$"))
    app.add_handler(CallbackQueryHandler(handle_studip_message_reply_start, pattern=r"^msg_reply\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_studip_message_reply_cancel, pattern=r"^msg_reply_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_file_audio_summary, pattern=r"^file_audio_sum\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_stums_documents_menu, pattern=r"^stums_docs_menu$"))
    app.add_handler(CallbackQueryHandler(handle_stums_doc_download, pattern=r"^stums_doc\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_tuition_status, pattern=r"^tuition_status$"))
    app.add_handler(CallbackQueryHandler(handle_forward_wa_ask, pattern="^forward_wa$"))
    app.add_handler(CallbackQueryHandler(forward_to_whatsapp, pattern="^forward_wa_confirm$"))
    app.add_handler(CallbackQueryHandler(handle_forward_wa_cancel, pattern="^forward_wa_cancel$"))
    app.add_handler(CallbackQueryHandler(show_last_announcements, pattern="^show_last_announcements$"))
    app.add_handler(CallbackQueryHandler(show_last_files, pattern="^show_last_files$"))
    app.add_handler(CallbackQueryHandler(show_last_forum_posts, pattern="^show_last_forum_posts$"))
    app.add_handler(CallbackQueryHandler(handle_status_buttons, pattern="^(studip_login|studip_logout|start_watchers|stop_watchers|request_wa_qr|force_wa_qr|detect_wa_groups|wa_test_message|fastenroll_menu|fastenroll_new|fastenroll_list_inline|wa_menu|mensa_food)$"))
    app.add_handler(CallbackQueryHandler(handle_exam_buttons, pattern="^(exam_menu|exam_noop|exam_cancel|exam_ask\\|.*|exam_do\\|.*)$"))
    app.add_handler(CallbackQueryHandler(handle_examopen_wa_ask, pattern="^examopen_wa_ask\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_examopen_wa_confirm, pattern="^examopen_wa_confirm\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_examopen_mute_ask, pattern="^examopen_mute_ask\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_examopen_mute_confirm, pattern="^examopen_mute_confirm\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_examopen_cancel, pattern="^examopen_cancel$"))
    app.add_handler(CallbackQueryHandler(set_wa_group_handler, pattern=r"^set_wa_group\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_today, pattern="^calendar_today$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_today_voice, pattern="^calendar_today_voice$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_weekly, pattern="^calendar_weekly$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_timetable, pattern="^calendar_timetable$"))
    app.add_handler(CallbackQueryHandler(handle_activity_stream, pattern=r"^activity_stream(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_exam_dates_list, pattern="^exam_dates_list$"))
    app.add_handler(CallbackQueryHandler(handle_exam_files_scan, pattern="^exam_files_scan$"))
    app.add_handler(CallbackQueryHandler(handle_all_exam_dates, pattern="^exam_dates_all$"))
    app.add_handler(CallbackQueryHandler(handle_transcript_summary, pattern="^transcript_summary$"))
    app.add_handler(CallbackQueryHandler(handle_upcoming_dashboard, pattern="^upcoming_dashboard$"))
    app.add_handler(CallbackQueryHandler(handle_task_buttons, pattern="^(task_add|task_list|task_by_course|task_done\\|.*|task_delete\\|.*)$"))
    app.add_handler(CallbackQueryHandler(handle_voice_confirm, pattern="^voice_confirm$"))
    app.add_handler(CallbackQueryHandler(handle_voice_reject, pattern="^voice_reject$"))
    app.add_handler(CallbackQueryHandler(handle_voice_clarify, pattern="^voice_clarify\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_task_course_pick, pattern="^(task_course\\|.*|task_course_skip)$"))
    app.add_handler(CallbackQueryHandler(handle_task_suggestion_add, pattern=r"^tasksug_add\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_announcement_to_task, pattern=r"^ann_to_task\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_task_reminder_action, pattern=r"^(task_reminder_done|task_reminder_snooze)\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_view, pattern="^course_view\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_browse_courses, pattern="^browse_courses$"))
    app.add_handler(CallbackQueryHandler(handle_course_noop, pattern="^course_noop$"))
    app.add_handler(CallbackQueryHandler(handle_course_semester_pick, pattern="^course_sem\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_prev_sem_scan, pattern=r"^toggle_prev_sem_scan\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_enroll_ask, pattern="^course_enroll_ask\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_enroll_confirm, pattern="^course_enroll_confirm\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_enroll_cancel, pattern="^course_enroll_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_browse_my_courses_for_deenroll, pattern="^browse_my_courses_deenroll$"))
    app.add_handler(CallbackQueryHandler(handle_deenroll_semester_pick, pattern="^deenroll_sem\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_files_semester_pick, pattern="^files_sem\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_enrollment_menu, pattern="^enrollment_menu$"))
    app.add_handler(CallbackQueryHandler(handle_set_default_semester, pattern="^set_default_semester$"))
    app.add_handler(CallbackQueryHandler(handle_set_user_name, pattern="^set_user_name$"))
    app.add_handler(CallbackQueryHandler(handle_set_weather_location, pattern="^set_weather_location$"))
    app.add_handler(CallbackQueryHandler(handle_default_semester_pick, pattern="^default_sem\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_default_semester_clear, pattern="^default_sem_clear$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_watch_prev_sem, pattern="^toggle_watch_prev_sem$"))
    app.add_handler(CallbackQueryHandler(handle_change_semester, pattern="^change_sem\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_deenroll_ask, pattern="^course_deenroll_ask\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_deenroll_confirm, pattern="^course_deenroll_confirm\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_course_deenroll_cancel, pattern="^course_deenroll_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_week, pattern=r"^calendar_week\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_weekly_voice, pattern="^calendar_weekly_voice\\|.*$"))
    app.add_handler(CallbackQueryHandler(menu_button_handler, pattern=r"^menu_nav\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_mensa_location_button, pattern=r"^mensa_loc\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_menu_preferences_button, pattern="^menu_prefs$"))
    app.add_handler(CallbackQueryHandler(handle_menu_recommend_button, pattern=r"^menu_recommend(\|\d+)?$"))
    app.add_handler(CallbackQueryHandler(handle_menu_photos_button, pattern=r"^menu_photos\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_category, pattern=r"^foodpref_cat\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_toggle, pattern=r"^foodpref_toggle\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_remove, pattern="^foodpref_remove\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_clear_ask, pattern="^foodpref_clear_ask$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_clear_confirm, pattern="^foodpref_clear_confirm$"))
    app.add_handler(CallbackQueryHandler(handle_foodpref_clear_cancel, pattern="^foodpref_clear_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_wardrobe_feature, pattern="^toggle_wardrobe_feature$"))
    app.add_handler(CallbackQueryHandler(handle_toggle_weekly_study_plan, pattern="^toggle_weekly_study_plan$"))
    app.add_handler(CallbackQueryHandler(handle_outfit_menu, pattern="^(outfit_menu|outfit_menu_toggle)$"))
    app.add_handler(CallbackQueryHandler(handle_status_group, pattern=r"^(status_group|status_group_toggle)\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_study_plan_toggle, pattern=r"^sp_plan\|\d+$"))
    app.add_handler(CallbackQueryHandler(handle_study_block_feedback, pattern=r"^sp_block\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_calendar_sync, pattern=r"^cal_sync(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_study_buddy, pattern=r"^buddy(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_course_alerts, pattern=r"^calert(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_privacy, pattern=r"^privacy(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_settings, pattern=r"^settings(\|.*)?$"))
    app.add_handler(CallbackQueryHandler(handle_study_progress_menu, pattern=r"^(sp_progress|sp_progress_refresh|sp_course\|.*|sp_unit\|.*)$"))
    app.add_handler(CallbackQueryHandler(handle_study_track_menu, pattern=r"^(sp_track_menu|sp_track_auto|sp_track\|.*)$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_menu, pattern="^wardrobe_menu$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_add_item, pattern="^wardrobe_add_item$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_suggest_now, pattern="^wardrobe_suggest_now$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_set_reference, pattern="^wardrobe_set_reference$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_remove, pattern="^wardrobe_remove\\|.*$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_clear_ask, pattern="^wardrobe_clear_ask$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_clear_confirm, pattern="^wardrobe_clear_confirm$"))
    app.add_handler(CallbackQueryHandler(handle_wardrobe_clear_cancel, pattern="^wardrobe_clear_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_selection))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_buttons))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))


# ── MAIN ──────────────────────────────────────────────────────────────────────



async def main():
    logging.info("Telegram bot running (100% browser-less Stud.IP automation)...")

    if not acquire_instance_lock():
        logging.error("❌ Another bot instance is already running. Exiting.")
        return

    try:
        from unibot.diagnostics import record_start
        record_start()  # the daily digest tells deploys from crashes by this
    except Exception as exc:
        logging.warning("Could not log this start: %s", exc)

    app = None
    try:
        # Bootstrap admin users and migrate existing credentials to DB vault
        try:
            from unibot.config import ADMIN_USER_IDS
            from unibot import db
            db.bootstrap_admins(ADMIN_USER_IDS)
            db.retire_imported_files()
            # Logins live per user in the DB vault. The single-user era files
            # cannot be attributed to one account reliably (they were also
            # written by sessions bound to nobody), so they are retired instead
            # of being copied into an admin's vault.
            for legacy in ("studip_cookies.json", "studip_oauth_tokens.json"):
                if os.path.exists(legacy):
                    os.replace(legacy, legacy + ".retired")
                    logging.info("Retired the shared %s; each user signs in with their own login", legacy)
        except Exception as exc:
            logging.debug("Credentials DB bootstrap skipped: %s", exc)

        state.global_session = pick_watcher_session()

        logging.info("🔐 Checking Stud.IP session...")
        if not await state.global_session.login():
            logging.warning("⚠️ Not logged in to Stud.IP yet. Starting bot so you can sign in via /login.")

        # validate_settings() requires the token; every API call is spaced and survives flood waits
        app = ApplicationBuilder().token(TELEGRAM_TOKEN or "").rate_limiter(TelegramSafetyLimiter()).build()
        register_handlers(app)

        # Explicitly initialize and start the application
        await app.initialize()
        await app.start()

        # Clear command list so Telegram's popup menu button does not show a command list
        try:
            await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
            await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
            for uid in ALLOWED_USER_IDS or []:
                try:
                    await app.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
                except TelegramError:
                    pass
            logging.info("🧹 Telegram bot commands and menu list cleared successfully")
        except Exception as exc:
            logging.warning("Could not clear Telegram bot commands: %s", exc)

        # Login page (+ optional Cloudflare tunnel + menu button)
        try:
            register_calendar_feed(_get_login_server())  # GET /cal/<token>.ics
            await _start_login_infra(app)
        except Exception as exc:
            logging.error("⚠️ Login infrastructure failed to start: %s", exc)

        # Pre-fetch Stud.IP profile to sync user name
        try:
            await get_studip_user_profile()
        except Exception as exc:
            logging.debug("Initial Stud.IP profile sync skipped: %s", exc)

        logging.info("🚀 Starting unified watcher...")
        # Run watcher in background task
        asyncio.create_task(start_unified_watcher(app))

        await restore_pending_fastenroll_jobs(app)

        logging.info("🤖 Starting Telegram bot polling...")
        await app.updater.start_polling(drop_pending_updates=True)

        logging.info("💬 Bot is now online and listening for messages.")

        # Run until the process is stopped
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            await stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("🛑 Stop signal received.")

    except Exception as e:
        logging.error("❌ Fatal error in main: %s", e, exc_info=True)
    finally:
        logging.info("🧹 Cleaning up...")
        stop_whatsapp_microservice()
        if app:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as se:
                logging.error("Error during app shutdown: %s", se)
            
        if state.quick_tunnel:
            try:
                await state.quick_tunnel.stop()
            except Exception:
                logging.debug("Quick tunnel stop failed", exc_info=True)
        if state.login_server:
            try:
                await state.login_server.stop()
            except Exception:
                logging.debug("Login server stop failed", exc_info=True)

        # write out a debounced link_cache change that hasn't fired yet
        link_cache.flush()
        await close_shared_http()

        await close_all_sessions()

        release_instance_lock()
        logging.info("✅ Bot shutdown complete")

def _cleanup_stale_whatsapp_service(wa_dir: str):
    """Ensure no stale or orphaned node server.js is running before startup."""
    lock_file = os.path.join(wa_dir, "whatsapp_service.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid() and psutil.pid_exists(old_pid):
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(0.5)
                    if psutil.pid_exists(old_pid):
                        os.kill(old_pid, signal.SIGKILL)
                    logging.info("Cleaned up stale WhatsApp service process (PID %s)", old_pid)
                except (ProcessLookupError, OSError):
                    pass
            try:
                os.remove(lock_file)
            except OSError:
                pass
        except Exception as e:
            logging.debug("Error reading WhatsApp lock file: %s", e)

    port = WA_SERVICE_PORT
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                logging.warning("Port %s is occupied by an existing process. Terminating stale process...", port)
                if os.name != 'nt':
                    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=5)
                time.sleep(1)
    except Exception as e:
        logging.debug("Port check/cleanup skipped: %s", e)


def start_whatsapp_microservice():
    wa_service_dir = os.path.join(PROJECT_ROOT, "whatsapp_service")
    if not os.path.exists(wa_service_dir):
        return
    _cleanup_stale_whatsapp_service(wa_service_dir)
    logging.info("Starting WhatsApp microservice...")
    kwargs: dict[str, Any] = {}
    if os.name != 'nt':
        kwargs['preexec_fn'] = os.setsid
    try:
        state.wa_process = subprocess.Popen(["node", "server.js"], cwd=wa_service_dir, **kwargs)
    except Exception as exc:
        logging.error("Failed to start WhatsApp microservice: %s", exc)


def stop_whatsapp_microservice():
    if state.wa_process is None:
        return
    logging.info("Stopping WhatsApp microservice...")
    try:
        if os.name != 'nt':
            try:
                pgid = os.getpgid(state.wa_process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        else:
            state.wa_process.terminate()

        try:
            state.wa_process.wait(timeout=5)
        except (subprocess.TimeoutExpired, Exception):
            if os.name != 'nt':
                try:
                    pgid = os.getpgid(state.wa_process.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            else:
                state.wa_process.kill()
    except Exception as e:
        logging.warning("Error stopping WhatsApp microservice: %s", e)
    finally:
        state.wa_process = None
def run() -> None:
    """Process entry point (was the __main__ block of studip_bot.py)."""

    for warning in validate_settings():
        logging.warning("⚠️ Config: %s", warning)

    start_whatsapp_microservice()
    atexit.register(stop_whatsapp_microservice)

    asyncio.run(main())
