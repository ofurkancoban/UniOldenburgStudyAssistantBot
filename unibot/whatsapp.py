"""Forwarding to the WhatsApp microservice and its Telegram controls.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

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
from unibot.auth import is_user_admin
from unibot.storage import shared_http


WA_FORWARD_ASK_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
WA_FORWARD_CONFIRM_MARKUP = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Yes, forward", callback_data="forward_wa_confirm"),
    InlineKeyboardButton("❌ Cancel", callback_data="forward_wa_cancel"),
]])


async def handle_forward_wa_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on any 'Forward to WA' button - swap it for a Yes/No confirmation
    instead of sending immediately, since these messages can carry personal info."""
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=WA_FORWARD_CONFIRM_MARKUP)
    except Exception as e:
        logging.warning("handle_forward_wa_ask: failed to swap markup: %s", e)


async def handle_forward_wa_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User declined forwarding - restore the original button so they can retry."""
    query = update.callback_query
    await query.answer("Cancelled")
    try:
        await query.edit_message_reply_markup(reply_markup=WA_FORWARD_ASK_MARKUP)
    except Exception as e:
        logging.warning("handle_forward_wa_cancel: failed to restore markup: %s", e)


async def forward_to_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message to WhatsApp group via Node.js microservice."""
    query = update.callback_query
    await query.answer("Sending to WhatsApp...")

    text = query.message.text or query.message.caption or "No text found"
    text = text.replace(EXAM_DATES_FOOTER, "").rstrip()
    success, message = await _send_text_to_whatsapp(text)
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

    try:
        async with shared_http() as session:
            await session.post(f"{WA_SERVICE_URL}/set_group_id", headers=WA_HEADERS, json={"groupId": group_id}, timeout=10)
    except Exception as e:
        logging.error("Failed to notify WhatsApp service of new group ID: %s", e)

    await query.answer("Group ID saved!")
    await query.edit_message_text(f"✅ WA_GROUP_ID saved and active:\n{group_id}")


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


async def _send_text_to_whatsapp(text: str) -> tuple[bool, str]:
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
    group_id = get_wa_group_id()
    if not group_id:
        return False, "No WhatsApp group is set yet. Use /status → \"📱 WhatsApp\" → \"🔍 Detect WA Groups\" and pick one first."
    text = _html_to_whatsapp_text(text)
    try:
        async with shared_http() as session:
            payload = {"text": text, "groupName": group_id}
            async with session.post(f"{WA_SERVICE_URL}/send", headers=WA_HEADERS, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    return True, "Sent to WhatsApp successfully!"
                data = await resp.json()
                return False, f"Failed to send to WhatsApp: {data.get('error')}"
    except Exception as e:
        logging.error("WhatsApp forward error: %s", e)
        return False, f"Error connecting to WhatsApp service: {e}"


# Bot-only hint under the new exam dates; forward_to_whatsapp leaves it out.
EXAM_DATES_FOOTER = "They're now in 📚 My Exam Dates, the weekly study plan and the exam reminders."
