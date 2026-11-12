"""Forwarding to the WhatsApp microservice and its Telegram controls.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import hashlib
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from unibot.config import (
    WA_HEADERS,
    WA_SERVICE_URL,
    get_wa_group_id,
    set_wa_group_id,
)
from unibot import db
from unibot.auth import ACTIVE, current_user_id, is_user_admin, is_user_allowed, lifecycle_state
from unibot.storage import shared_http


WA_ADMIN_ONLY = "⛔ Forwarding to WhatsApp is for administrators only."
WA_FORWARD_ASK_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
WA_FORWARD_CONFIRM_MARKUP = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Yes, forward", callback_data="forward_wa_confirm"),
    InlineKeyboardButton("❌ Cancel", callback_data="forward_wa_cancel"),
]])

# Telegram can deliver multiple confirmation callbacks when an administrator
# taps twice before the first WhatsApp request finishes. Keep one send in
# flight per source message to avoid duplicate posts to the group.
_wa_forwards_in_flight: set[tuple[int, int]] = set()


def whatsapp_forward_in_flight(key: tuple[int, int]) -> bool:
    return key in _wa_forwards_in_flight


async def handle_forward_wa_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on any 'Forward to WA' button - swap it for a Yes/No confirmation
    instead of sending immediately, since these messages can carry personal info."""
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer(WA_ADMIN_ONLY, show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=WA_FORWARD_CONFIRM_MARKUP)
    except Exception as e:
        logging.warning("handle_forward_wa_ask: failed to swap markup: %s", e)


async def handle_forward_wa_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User declined forwarding - restore the original button so they can retry."""
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer(WA_ADMIN_ONLY, show_alert=True)
        return
    await query.answer("Cancelled")
    try:
        await query.edit_message_reply_markup(reply_markup=WA_FORWARD_ASK_MARKUP)
    except Exception as e:
        logging.warning("handle_forward_wa_cancel: failed to restore markup: %s", e)


def _forward_allowed(uid) -> bool:
    return (uid is not None and is_user_admin(uid) and is_user_allowed(uid)
            and lifecycle_state(uid) == ACTIVE and db.private_context_is_current(uid))


async def forward_to_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message to WhatsApp group via Node.js microservice."""
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        # the group belongs to the administrator: students can't post into it
        await query.answer(WA_ADMIN_ONLY, show_alert=True)
        return
    await query.answer("Sending to WhatsApp...")
    if not _forward_allowed(query.from_user.id):
        return

    text = query.message.text or query.message.caption or "No text found"
    text = text.replace(EXAM_DATES_FOOTER, "").rstrip()
    key = (query.message.chat_id, query.message.message_id)
    success, message = await _send_text_to_whatsapp(text, dedupe_key=key)
    if not _forward_allowed(query.from_user.id):
        return
    if success:
        await query.edit_message_reply_markup(reply_markup=None)  # Remove button after sending
    await context.bot.send_message(chat_id=query.message.chat_id, text=f"✅ {message}" if success else f"❌ {message}")


async def set_wa_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persist a WhatsApp group ID discovered by the Node.js microservice as WA_GROUP_ID."""
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    group_id = query.data.split("|", 1)[1]

    set_wa_group_id(group_id)

    service_synced = False
    sync_error = None
    try:
        async with shared_http() as session:
            async with session.post(
                f"{WA_SERVICE_URL}/set_group_id",
                headers=WA_HEADERS,
                json={"groupId": group_id},
                timeout=10,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    service_synced = data.get("success") is True
                    if not service_synced:
                        sync_error = data.get("error", "service did not confirm the update")
                else:
                    sync_error = f"service returned HTTP {response.status}"
    except Exception as e:
        logging.error("Failed to notify WhatsApp service of new group ID: %s", e)
        sync_error = str(e)

    if service_synced:
        await query.answer("Group ID saved and activated!")
        await query.edit_message_text(f"✅ WA_GROUP_ID saved and active:\n{group_id}")
    else:
        await query.answer("Saved locally, but WhatsApp service was not updated.", show_alert=True)
        await query.edit_message_text(
            f"⚠️ WA_GROUP_ID saved locally, but the running WhatsApp service may still use its previous group.\n"
            f"Selected group: {group_id}\n"
            f"Service update error: {sync_error or 'no confirmation received'}\n\n"
            "Retry selecting this group after the WhatsApp service is reachable."
        )


def _html_to_whatsapp_text(text: str) -> str:
    """Convert the Telegram-style HTML markup used in bot-composed WA
    forwards (e.g. handle_examopen_wa_confirm's "EXAM REGISTRATION OPEN"
    text) into WhatsApp's own plain-text markdown, since WhatsApp doesn't
    render HTML at all - literal "<b>...</b>" was showing up verbatim in
    forwarded messages instead of being bold. <b>/<strong> become
    WhatsApp's *bold*, <i>/<em> become _italic_, and any other stray tag
    (there shouldn't be any today, but this is the one chokepoint every
    WA-forwarded message passes through) is stripped rather than leaked
    through as-is."""
    text = re.sub(r"<(b|strong)>(.*?)</\1>", r"*\2*", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(i|em)>(.*?)</\1>", r"_\2_", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


async def _send_text_to_whatsapp(text: str, *, dedupe_key: tuple[int, int] | None = None) -> tuple[bool, str]:
    """POST plain text to the local WhatsApp microservice's target group.
    Returns (success, message) - mirrors forward_to_whatsapp's own request,
    factored out so the "EXAM REGISTRATION OPEN"-style notifications (which
    have no single Telegram message to re-read text from, since their WA
    button opens its own confirm step) can send their own composed text
    through the same path.

    The target group is always WA_GROUP_ID - the exact group ID captured
    from a real message via "🔍 Detect WA Groups" (see set_wa_group_handler),
    never a manually-typed group name (fragile: names can collide or drift
    from what WhatsApp actually has stored, which was silently sending
    nothing in exactly the group-not-found case)."""
    owner = dedupe_key[0] if dedupe_key is not None else current_user_id()
    if not _forward_allowed(owner):
        return False, "Forward cancelled because account access changed."
    group_id = get_wa_group_id()
    if not group_id:
        return False, "No WhatsApp group is set yet. Use /status → \"📱 WhatsApp\" → \"🔍 Detect WA Groups\" and pick one first."
    if dedupe_key is not None:
        if dedupe_key in _wa_forwards_in_flight:
            return False, "This message is already being sent."
        _wa_forwards_in_flight.add(dedupe_key)
    action_key = hashlib.sha256(f"wa:{group_id}:{dedupe_key}".encode()).hexdigest() if dedupe_key is not None else None
    attempted = False
    claimed = False
    text = _html_to_whatsapp_text(text)
    try:
        if action_key:
            previous = db.claim_outgoing_action(action_key)
            if previous == "sent":
                return True, "This message was already sent to WhatsApp."
            if previous is not None:
                return False, "Delivery status is uncertain. Check the WhatsApp group before sending again."
            claimed = True
        async with shared_http() as session:
            if not _forward_allowed(owner):
                return False, "Forward cancelled because account access changed."
            payload = {"text": text, "groupName": group_id}
            attempted = True
            async with session.post(f"{WA_SERVICE_URL}/send", headers=WA_HEADERS, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    if action_key:
                        db.finish_outgoing_action(action_key, "sent")
                    return True, "Sent to WhatsApp successfully!"
                if action_key and 400 <= resp.status < 500:
                    db.finish_outgoing_action(action_key, None)
                data = await resp.json()
                return False, f"Failed to send to WhatsApp: {data.get('error')}"
    except Exception as e:
        logging.error("WhatsApp forward error: %s", e)
        return False, f"Error connecting to WhatsApp service: {e}"
    finally:
        if action_key and claimed and not attempted:
            db.finish_outgoing_action(action_key, None)
        if dedupe_key is not None:
            _wa_forwards_in_flight.discard(dedupe_key)


# Bot-only hint under the new exam dates; forward_to_whatsapp leaves it out.
EXAM_DATES_FOOTER = "They're now in 📚 My Exam Dates, the weekly study plan and the exam reminders."
