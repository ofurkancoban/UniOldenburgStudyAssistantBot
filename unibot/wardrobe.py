"""Wardrobe catalog, outfit picking and try-on image generation.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import os
import random
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import base64
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN, aread_bytes, awrite_bytes, redact_url

from unibot.config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_IMAGE_MODEL,
    CLOUDFLARE_VISION_MODEL,
    FAL_GEMINI_TRYON_MODEL,
    FAL_KEY,
    FAL_TRYON_MODEL,
    GWDG_OUTFIT_MODEL,
    GWDG_TEXT_MODEL,
    GWDG_VISION_MODEL,
    HF_SPACE_ACCESSORIES,
    HF_SPACE_VTON,
    HF_TOKEN,
    OPENROUTER_MODEL,
    OUTFIT_OPENROUTER_MODEL,
)
from unibot.preferences import get_pref, set_pref
from unibot import db
from unibot.storage import (
    clear_wardrobe_items,
    get_cached_outfit_today,
    get_recently_worn_item_ids,
    get_wardrobe_entry,
    record_worn_items,
    remove_wardrobe_item,
    save_cached_outfit_today,
    shared_http,
)
from unibot.telegram_ui import (
    user_data,
    _download_telegram_file,
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
)
from unibot.auth import is_user_admin
from unibot.ai import (
    _call_gwdg_chat,
    _call_openrouter,
    personal_ai_allowed,
    _extract_json_object,
    generate_outfit_tryon_gwdg,
)
from unibot.weather import get_weather_snapshot


CLOUDFLARE_AI_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def outfit_caption(outfit: dict) -> str:
    """HTML caption for an outfit pick (fresh or cached): just the chosen
    items as a bullet list. Falls back to the description for a cached pick
    saved before item labels were stored. Labels can hold a bare "&"
    ("Jack & Jones"), so they're escaped."""
    labels = outfit.get("item_labels") or [it["label"] for it in outfit.get("chosen_items", [])]
    if not labels:
        return f"👔 <b>Today's outfit:</b> {html.escape(outfit['description'])}"
    return "👔 <b>Today's outfit:</b>\n" + "\n".join(f"• {html.escape(label)}" for label in labels)


OUTFIT_RECOMMENDATION_SYSTEM_PROMPT = """You are picking a coherent, weather-appropriate outfit for a student from their own wardrobe, using only the candidate items listed (each with an id, type, subtype, color, and warmth level). Follow this layering logic based on today's forecast high temperature and whether rain is expected:
- Below 5°C ("very cold"): full layering - a top (prefer subtype=sweater, else shirt/t-shirt) + a light_jacket AND a coat together if both are available (worn as mid layer + outer shell) + pants.
- 5-12°C ("cold"): a top + exactly ONE outerwear item (a coat if available, else a light_jacket) + pants.
- 12-18°C ("mild"): a top + pants; add a light_jacket if rain is expected OR if the morning low is below 10°C (the high alone can be misleading on a day that starts cold).
- 18-24°C ("warm"): a top (prefer subtype=t-shirt) + pants or shorts, preferring shorts (subtype=shorts) when available; add a light_jacket only if rain is expected.
- 24°C and above ("hot"): a top (prefer subtype=t-shirt) + shorts, preferring shorts over pants even if rain is expected - just add a light_jacket too if one's available on a rainy hot day.
Never pick two tops or two bottoms. Also always include exactly one "shoes" item and one headwear "accessory" item (a cap/beanie/hat, not socks or other accessories) if any such items are present in the candidate list - these can't be shown in the outfit image, but still name them in your reasoning. Some items are tagged "[worn in the last couple days, prefer an alternative if one fits]" - pick a different item of that same type when a suitable alternative exists in the list, but don't force an unsuitable combination just to avoid a repeat.
Among the weather-appropriate options, pick the combination whose colors work best together:
- Neutrals (black, white, grey, navy, denim blue, beige, camel, brown, olive/khaki) go with almost anything; build on them.
- At most one statement piece per outfit (a bold color like bordeaux or bright green, or a loud pattern); keep the rest neutral. Never combine two loud patterns.
- Earth tones (olive, dark green, brown, camel, bordeaux, rust) look good together; a tonal outfit (shades of one color, e.g. dark green trousers + green tee) is fine too.
- Don't clash warm and cool brights (e.g. bordeaux with bright blue), and avoid near-matches that look like a mistake (e.g. a black top under a slightly different black jacket).
- Repeat a color: shoes or headwear that echo a color from the top/outerwear tie the outfit together.
Respond with strict JSON only, no other text: {"item_ids": ["<id>", ...], "reasoning": "<one short sentence, e.g. 'Layering up with the navy hoodie and the coat since it'll be cold and dry, plus the beanie and trainers.'>", "color_tip": "<one short sentence on the colors: why they work together (e.g. 'The olive jacket and olive trainers frame the neutral black tee and light denim.'), or a concrete swap from the list that would match better if the weather forced a weaker combination>"}. Use only ids from the given list."""


# A morning low under this needs a jacket even on a mild (12-18°C) day:
# a foggy 8.4°C start in just a t-shirt was the trigger for raising it
# from 8.
COLD_MORNING_CELSIUS = 10


def _outerwear_required(weather: Optional[dict]) -> bool:
    """Whether today's weather calls for an outer layer, per the band
    rules in OUTFIT_RECOMMENDATION_SYSTEM_PROMPT."""
    if not weather:
        return False
    band = _temperature_band(weather.get("temp_max"))
    temp_min = weather.get("temp_min")
    is_wet = bool(weather.get("is_wet"))
    if band in ("very_cold", "cold"):
        return True
    if band == "mild":
        return is_wet or (temp_min is not None and temp_min < COLD_MORNING_CELSIUS)
    return band == "warm" and is_wet


def _add_required_outerwear(items: list[dict], chosen: list[dict], weather: Optional[dict], recent_ids: set) -> list[dict]:
    """Models sometimes skip the jacket on a mild day with a cold morning
    despite the prompt; add one when the weather needs it and none was
    picked: a coat when it's cold, else a light jacket."""
    if not _outerwear_required(weather):
        return chosen
    pool = _prefer_unworn([it for it in items if it.get("type") == "outerwear"], recent_ids)
    band = _temperature_band((weather or {}).get("temp_max"))
    if band == "very_cold":
        # Full winter layering needs both a middle layer and an outer shell
        # when the wardrobe has both, even if the model selected only one.
        present = {it.get("subtype") for it in chosen if it.get("type") == "outerwear"}
        additions: list[dict] = []
        for subtype in ("light_jacket", "coat"):
            if subtype in present:
                continue
            item = next((it for it in pool if it.get("subtype") == subtype), None)
            if item and item["id"] not in {it["id"] for it in chosen + additions}:
                additions.append(item)
        if not present and not additions and pool:
            additions.append(pool[0])
        return chosen + additions
    if any(it.get("type") == "outerwear" for it in chosen):
        return chosen
    preferred = "coat" if band == "cold" else "light_jacket"
    outer = next((it for it in pool if it.get("subtype") == preferred), None) or (pool[0] if pool else None)
    return chosen + [outer] if outer else chosen


def _normalize_model_outfit(items: list[dict], chosen: list[dict], weather: Optional[dict], recent_ids: set) -> list[dict]:
    """Enforce garment-count and weather rules on untrusted model output."""
    unique = []
    seen_ids = set()
    for item in chosen:
        if item["id"] not in seen_ids:
            unique.append(item)
            seen_ids.add(item["id"])

    result = []
    kept_types = set()
    for item in unique:
        kind = item.get("type")
        if kind in ("top", "bottom", "shoes"):
            if kind in kept_types:
                continue
            kept_types.add(kind)
            result.append(item)
        elif kind == "outerwear":
            band = _temperature_band(weather.get("temp_max") if weather else None)
            needed = _outerwear_required(weather)
            if not needed:
                continue
            if band == "very_cold":
                if item.get("subtype") not in ("light_jacket", "coat"):
                    if any(it.get("type") == "outerwear" for it in result):
                        continue
                elif any(it.get("subtype") == item.get("subtype") for it in result if it.get("type") == "outerwear"):
                    continue
            elif any(it.get("type") == "outerwear" for it in result):
                continue
            result.append(item)
        elif kind == "accessory" and any(word in item.get("label", "").lower() for word in _HEADWEAR_KEYWORDS):
            if not any(it.get("type") == "accessory" and any(
                word in it.get("label", "").lower() for word in _HEADWEAR_KEYWORDS
            ) for it in result):
                result.append(item)

    # A partial but syntactically valid model answer should not omit the
    # basic top/bottom when the wardrobe contains them.
    fallback = _heuristic_outfit_pick(items, weather, recent_ids)
    present_types = {it.get("type") for it in result}
    for kind in ("top", "bottom"):
        if kind not in present_types:
            fill = next((it for it in fallback if it.get("type") == kind), None)
            if fill and fill["id"] not in {it["id"] for it in result}:
                result.append(fill)
                present_types.add(kind)
    return result


def _temperature_band(temp_max: Optional[float]) -> str:
    """Map a forecast high to one of the 5 bands the outfit logic (both
    the LLM prompt above and this heuristic fallback) reasons in terms
    of - kept as one function so the two stay in sync if the thresholds
    ever change."""
    if temp_max is None:
        return "mild"
    if temp_max < 5:
        return "very_cold"
    if temp_max < 12:
        return "cold"
    if temp_max < 18:
        return "mild"
    if temp_max < 24:
        return "warm"
    return "hot"


def _prefer_unworn(pool: list[dict], recent_ids: set) -> list[dict]:
    """Stable-partition a candidate pool so recently-worn items (see
    get_recently_worn_item_ids) sort after everything else - the
    "first match" selection below then naturally avoids repeats when a
    genuine alternative exists, but still falls back to a recently-worn
    item rather than nothing when it's the only option in that subtype."""
    if not recent_ids:
        return pool
    return [it for it in pool if it["id"] not in recent_ids] + [it for it in pool if it["id"] in recent_ids]


def _heuristic_outfit_pick(items: list[dict], weather: Optional[dict], recent_ids: Optional[set] = None) -> list[dict]:
    """Fallback used when the LLM call fails or returns unparsable JSON -
    deterministic temperature-band + layering logic (mirrors
    OUTFIT_RECOMMENDATION_SYSTEM_PROMPT) instead of a plain warmth match,
    since an incoherent random outfit is a worse failure mode than an
    incoherent random meal pick (build_food_recommendation's equivalent
    fallback)."""
    band = _temperature_band(weather.get("temp_max") if weather else None)
    is_wet = bool(weather.get("is_wet")) if weather else False
    temp_min = weather.get("temp_min") if weather else None
    cold_morning = temp_min is not None and temp_min < COLD_MORNING_CELSIUS
    recent_ids = recent_ids or set()

    tops = _prefer_unworn([it for it in items if it.get("type") == "top"], recent_ids)
    bottoms = _prefer_unworn([it for it in items if it.get("type") == "bottom"], recent_ids)
    outerwear = _prefer_unworn([it for it in items if it.get("type") == "outerwear"], recent_ids)

    def _by_subtype(pool, subtype):
        return next((it for it in pool if it.get("subtype") == subtype), None)

    chosen = []

    if band == "very_cold":
        top = _by_subtype(tops, "sweater") or _by_subtype(tops, "shirt") or _by_subtype(tops, "t-shirt") or (tops[0] if tops else None)
    elif band == "hot" or band == "warm":
        top = _by_subtype(tops, "t-shirt") or (tops[0] if tops else None)
    else:
        top = _by_subtype(tops, "shirt") or _by_subtype(tops, "sweater") or _by_subtype(tops, "t-shirt") or (tops[0] if tops else None)
    if top:
        chosen.append(top)

    if band in ("warm", "hot"):
        bottom = _by_subtype(bottoms, "shorts") or _by_subtype(bottoms, "pants") or (bottoms[0] if bottoms else None)
    else:
        bottom = _by_subtype(bottoms, "pants") or (bottoms[0] if bottoms else None)
    if bottom:
        chosen.append(bottom)

    light_jacket = _by_subtype(outerwear, "light_jacket")
    coat = _by_subtype(outerwear, "coat")
    if band == "very_cold":
        # Full layering - both pieces together if the wardrobe has them.
        for piece in (light_jacket, coat):
            if piece:
                chosen.append(piece)
        if not light_jacket and not coat and outerwear:
            chosen.append(outerwear[0])
    elif band == "cold":
        outer = coat or light_jacket or (outerwear[0] if outerwear else None)
        if outer:
            chosen.append(outer)
    elif band == "mild" and (is_wet or cold_morning):
        # A dry mild afternoon can still start from a genuinely cold
        # morning (e.g. 6°C low, 12°C high) - temp_max alone would miss
        # that, so a cold morning low also triggers a light layer even
        # when it won't rain.
        outer = light_jacket or coat or (outerwear[0] if outerwear else None)
        if outer:
            chosen.append(outer)
    elif band in ("warm", "hot") and is_wet:
        outer = light_jacket or (outerwear[0] if outerwear else None)
        if outer:
            chosen.append(outer)

    return chosen or items[:2]


_HEADWEAR_KEYWORDS = ("cap", "beanie", "hat")
# The pick prompt lists the whole wardrobe; with a thinking phase on, a free
# model can spend minutes on it (7k+ reasoning tokens for ~30 items), so the
# pick runs with reasoning off and a timeout above the default 15 s (most
# answers take 2-3 s, the odd slow one close to a minute).
OUTFIT_AI_TIMEOUT_SECONDS = 60


def _add_mandatory_shoes_and_headwear(items: list[dict], chosen: list[dict]) -> list[dict]:
    """Shoes and a cap/beanie/hat can't be visually rendered (no free
    try-on model supports footwear/headwear), but the user wants them
    named in the outfit text whenever the wardrobe has one - applied
    after BOTH the LLM pick and the heuristic fallback, so it's
    guaranteed regardless of whether the LLM actually complied with the
    "always include" instruction in OUTFIT_RECOMMENDATION_SYSTEM_PROMPT."""
    chosen_ids = {it["id"] for it in chosen}
    if not any(it.get("type") == "shoes" for it in chosen):
        shoes = next((it for it in items if it.get("type") == "shoes"), None)
        if shoes and shoes["id"] not in chosen_ids:
            chosen.append(shoes)
    has_headwear = any(
        it.get("type") == "accessory" and any(kw in it.get("label", "").lower() for kw in _HEADWEAR_KEYWORDS)
        for it in chosen
    )
    if not has_headwear:
        headwear = next(
            (it for it in items if it.get("type") == "accessory" and any(kw in it.get("label", "").lower() for kw in _HEADWEAR_KEYWORDS)),
            None,
        )
        if headwear and headwear["id"] not in chosen_ids:
            chosen.append(headwear)
    return chosen


async def pick_outfit_from_wardrobe(items: list[dict], weather: Optional[dict], recent_ids: Optional[set] = None,
                                   user_id: Optional[int] = None) -> Optional[dict]:
    """Ask the LLM to pick a coherent, temperature-banded outfit from the
    user's cataloged wardrobe items, considering the morning's weather
    snapshot (see get_weather_snapshot) - structural twin of
    build_food_recommendation, just over wardrobe metadata instead of menu
    items. Pure text reasoning over already-tagged items, so this reuses
    the bot's existing free-tier OpenRouter text call, not the
    Cloudflare/HF image providers.

    `recent_ids` (see get_recently_worn_item_ids) nudges away from
    repeating the exact same outfit two days running - soft preference,
    not a hard exclusion, since a small wardrobe may have only one real
    option for a given slot.

    Returns {"chosen_items": [...item dicts...], "description": "<human
    readable, e.g. 'navy hoodie + black jeans'>"}, or None if there are no
    items to choose from at all."""
    if not items or user_id is None or not personal_ai_allowed(user_id):
        return None

    band = _temperature_band(weather.get("temp_max") if weather else None)
    recent_ids = recent_ids or set()
    lines = [
        f"- id={it['id']}: {it['label']} (type={it['type']}, subtype={it.get('subtype') or 'n/a'}, color={it['color']}, warmth={it['warmth']})"
        + (" [worn in the last couple days, prefer an alternative if one fits]" if it["id"] in recent_ids else "")
        for it in items
    ]
    if weather:
        is_wet = weather.get("is_wet")
        weather_line = f"Weather: {weather.get('description', 'unknown')}, {weather.get('temp_min')}-{weather.get('temp_max')}°C ({band}), {'rain expected' if is_wet else 'dry'}."
        if _outerwear_required(weather):
            weather_line += " An outerwear item is required today."
    else:
        weather_line = "Weather: no data available today - pick something reasonably versatile (mild band)."

    user_text = f"{weather_line}\n\nWardrobe:\n" + "\n".join(lines)
    raw = None
    # Free models get rate-limited upstream now and then; the default
    # models are the backup.
    for model, gwdg_model in ((OUTFIT_OPENROUTER_MODEL, GWDG_OUTFIT_MODEL), (OPENROUTER_MODEL, GWDG_TEXT_MODEL)):
        if user_id is not None and not personal_ai_allowed(user_id):
            return None
        raw = await _call_openrouter(
            OUTFIT_RECOMMENDATION_SYSTEM_PROMPT, user_text,
            temperature=0.9, timeout=OUTFIT_AI_TIMEOUT_SECONDS, reasoning=False, model=model, gwdg_model=gwdg_model,
            can_send=lambda: personal_ai_allowed(user_id),
        )
        if raw:
            break
    parsed = _extract_json_object(raw)
    chosen_items = []
    raw_item_ids = parsed.get("item_ids") if parsed else None
    if isinstance(raw_item_ids, list):
        by_id = {it["id"]: it for it in items}
        chosen_items = [by_id[item_id] for item_id in raw_item_ids
                        if isinstance(item_id, str) and item_id in by_id]

    if not chosen_items:
        chosen_items = _heuristic_outfit_pick(items, weather, recent_ids)
    if not chosen_items:
        return None
    else:
        chosen_items = _normalize_model_outfit(items, chosen_items, weather, recent_ids)
    pre_ids = {it["id"] for it in chosen_items}
    chosen_items = _add_required_outerwear(items, chosen_items, weather, recent_ids)
    chosen_items = _add_mandatory_shoes_and_headwear(items, chosen_items)
    forced_extras = [it["label"] for it in chosen_items if it["id"] not in pre_ids]

    raw_reasoning = (parsed or {}).get("reasoning")
    reasoning = raw_reasoning.strip()[:500] if isinstance(raw_reasoning, str) else ""
    description = reasoning or " + ".join(it["label"] for it in chosen_items)
    if reasoning and forced_extras:
        # The LLM/heuristic reasoning may not mention the mandatory
        # outerwear/shoes/headwear add-ons (see _add_required_outerwear,
        # _add_mandatory_shoes_and_headwear) -
        # make sure they're always named in what the user sees.
        description += " + " + " + ".join(forced_extras)
    # Only the LLM pick reasons about colors; the heuristic fallback has no tip.
    raw_color_tip = parsed.get("color_tip") if parsed and isinstance(raw_item_ids, list) and raw_item_ids else None
    color_tip = raw_color_tip.strip()[:300] if isinstance(raw_color_tip, str) else ""
    return {"chosen_items": chosen_items, "description": description, "color_tip": color_tip}


def _wardrobe_list_view(user_id) -> tuple:
    """Build the (text, reply_markup) pair for the "👔 My Wardrobe" screen -
    direct template match to _food_preferences_management_view: one delete
    button per cataloged item, an "Add" button, and Clear All when there's
    more than one item."""
    entry = get_wardrobe_entry(user_id)
    items = entry.get("items", [])

    text = "👔 <b>My Wardrobe</b>\n"
    text += f"{len(items)} item(s) cataloged.\n" if items else "Nothing added yet.\n"
    keyboard = [[InlineKeyboardButton(f"🗑 {it['label']} ({it['type']}/{it.get('subtype') or '-'})", callback_data=f"wardrobe_remove|{it['id']}")] for it in items]
    keyboard.append([InlineKeyboardButton("📸 Add Clothing Item", callback_data="wardrobe_add_item")])
    if items and entry.get("reference_photo_file_id"):
        keyboard.append([InlineKeyboardButton("👔 Suggest Outfit Now", callback_data="wardrobe_suggest_now")])
    if len(items) > 1:
        keyboard.append([InlineKeyboardButton("🗑 Clear all", callback_data="wardrobe_clear_ask")])
    return text, InlineKeyboardMarkup(keyboard)


async def handle_wardrobe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    text, keyboard = _wardrobe_list_view(user_id)
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_wardrobe_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await query.message.reply_text(
            "🔒 Clothing photos are sent to an outside AI service for tagging. "
            "Allow personal data use under /settings → 🔒 Privacy before adding an item."
        )
        return
    user_data(context)["pending_step"] = "wardrobe_add_item"
    await query.message.reply_text("📸 Send a photo of one clothing item. It will be sent to an outside AI service for tagging.")


async def handle_wardrobe_set_reference(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    user_data(context)["pending_step"] = "wardrobe_set_reference"
    await query.message.reply_text(
        "🖼️ Send one clear full-body photo of yourself. It is stored for outfit suggestions and sent to outside AI services only "
        "when personal AI use is enabled under /settings → 🔒 Privacy."
    )


async def handle_wardrobe_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    item_id = query.data.split("|", 1)[1]
    remove_wardrobe_item(user_id, item_id)
    text, keyboard = _wardrobe_list_view(user_id)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_wardrobe_clear_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(query.from_user.id):
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, clear all", callback_data="wardrobe_clear_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="wardrobe_clear_cancel"),
    ]])
    await query.edit_message_text("🗑 Clear your whole wardrobe? Your reference photo is kept.", reply_markup=keyboard)


async def handle_wardrobe_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer("Cleared")
    user_id = query.from_user.id
    if not db.private_context_is_current(user_id):
        return
    clear_wardrobe_items(user_id)
    text, keyboard = _wardrobe_list_view(user_id)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_wardrobe_clear_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_user_admin(query.from_user.id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer("Cancelled")
    user_id = query.from_user.id
    if not db.private_context_is_current(user_id):
        return
    text, keyboard = _wardrobe_list_view(user_id)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_wardrobe_suggest_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wardrobe_suggest_now callback ("👔 Suggest Outfit Now") - the
    on-demand twin of the morning summary's automatic section (see 4c in
    prepare_morning_summary), using the same pick_outfit_from_wardrobe/
    generate_outfit_tryon_image pipeline. Unlike the morning summary, which
    skips silently when the wardrobe/reference photo aren't set up yet,
    this is an explicit request - tell the user why nothing happened."""
    query = update.callback_query

    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return

    if not personal_ai_allowed(user_id):
        await query.message.reply_text(
            "🔒 Outfit suggestions use your wardrobe and reference photo with outside AI services. "
            "Allow personal data use under /settings → 🔒 Privacy first."
        )
        return

    entry = get_wardrobe_entry(user_id)
    items = entry.get("items", [])
    ref_file_id = entry.get("reference_photo_file_id")
    if not items:
        await query.message.reply_text("👔 Add at least one clothing item first - tap 📸 Add Clothing Item.")
        return
    if not ref_file_id:
        await query.message.reply_text("🖼️ Set your reference photo first - tap 🖼️ Set Reference Photo in /status.")
        return

    # Reuse today's already-generated pick if this is a second trigger the
    # same day (e.g. morning summary already ran) - resending a cached
    # photo_file_id is instant and free, unlike burning another round of
    # quota-limited image-generation calls for the same day's outfit.
    cached = get_cached_outfit_today(user_id)
    if cached:
        caption = outfit_caption(cached)
        if cached.get("photo_file_id"):
            await query.message.reply_photo(photo=cached["photo_file_id"], caption=caption, parse_mode="HTML")
        else:
            await query.message.reply_text(caption, parse_mode="HTML")
        return

    await _show_typing(query)
    status_msg = await query.message.reply_text("👔 Picking an outfit for you...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Picking an outfit for you...")
    try:
        weather = await get_weather_snapshot(datetime.now(TZ_BERLIN).date(), user_id=user_id)
        if not db.private_context_is_current(user_id):
            return
        if not personal_ai_allowed(user_id):
            await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
            return
        recent_ids = get_recently_worn_item_ids(user_id)
        outfit = await pick_outfit_from_wardrobe(items, weather, recent_ids, user_id=user_id)
        if not db.private_context_is_current(user_id):
            return
        if not personal_ai_allowed(user_id):
            await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
            return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if not outfit:
        await query.message.reply_text("❌ Couldn't put together an outfit right now - try again in a bit.")
        return

    caption = outfit_caption(outfit)
    reference_bytes = await _download_telegram_file(context.bot, ref_file_id)
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
        return
    image_bytes = await generate_outfit_tryon_image(
        context.bot, reference_bytes, outfit["chosen_items"], user_id=user_id
    ) if reference_bytes else None
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
        return
    photo_file_id = None
    if image_bytes:
        sent = await query.message.reply_photo(photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML")
        photo_file_id = sent.photo[-1].file_id if sent.photo else None
    else:
        await query.message.reply_text(caption, parse_mode="HTML")

    record_worn_items(user_id, [it["id"] for it in outfit["chosen_items"]])
    save_cached_outfit_today(user_id, outfit["description"], photo_file_id, outfit.get("color_tip", ""), [it["label"] for it in outfit["chosen_items"]])


async def handle_toggle_wardrobe_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stateless flip toggle, same template as handle_toggle_watch_prev_sem."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    new_state = not get_pref(user_id, "outfit")
    set_pref(user_id, "outfit", new_state)
    await query.edit_message_text(
        f"👔 Daily Outfit Suggestion is now {'✅ On' if new_state else '🔲 Off'}.\nRun /status to see the full menu again."
    )


def _outfit_menu_view(user_id: Optional[int] = None) -> tuple:
    """/status's grouped outfit submenu: the daily suggestion toggle,
    the wardrobe list and the reference photo setup in one place."""
    enabled = bool(get_pref(user_id, "outfit") if isinstance(user_id, int) else False)
    text = f"👔 <b>Outfit &amp; Wardrobe</b>\n\nDaily Outfit Suggestion: {'✅ On' if enabled else '🔲 Off'}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if enabled else '🔲'} Daily Outfit Suggestion", callback_data="outfit_menu_toggle")],
        [InlineKeyboardButton("👔 My Wardrobe", callback_data="wardrobe_menu")],
        [InlineKeyboardButton("🖼️ Set Reference Photo", callback_data="wardrobe_set_reference")],
    ])
    return text, keyboard


async def handle_outfit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """outfit_menu opens the submenu as a new message (keeping /status
    intact, same as wardrobe_menu); outfit_menu_toggle flips the daily
    suggestion and redraws the submenu in place."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    if query.data == "outfit_menu_toggle":
        current = bool(get_pref(user_id, "outfit"))
        new_state = not current
        set_pref(user_id, "outfit", new_state)
        text, keyboard = _outfit_menu_view(user_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
    text, keyboard = _outfit_menu_view(user_id)
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _fetch_wardrobe_item_bytes(bot, item: dict) -> Optional[bytes]:
    """Resolve a wardrobe item (or the reference-photo dict shape) back to
    image bytes, regardless of whether it's backed by a Telegram file_id
    (the normal upload flow) or a direct image_url (bulk-imported items).
    Returns None on any failure so callers degrade gracefully."""
    if item.get("file_id"):
        return await _download_telegram_file(bot, item["file_id"])
    image_url = item.get("image_url")
    if image_url:
        try:
            async with shared_http() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logging.warning("_fetch_wardrobe_item_bytes: HTTP %s for %s", resp.status, redact_url(image_url))
        except Exception as e:
            logging.warning("_fetch_wardrobe_item_bytes failed for %s: %s", redact_url(image_url), e)
    return None


TAG_WARDROBE_ITEM_PROMPT = """You're looking at a photo of a single clothing item. Respond with strict JSON only, no other text: {"type": "top|bottom|outerwear|shoes|accessory", "subtype": "<for type=top: t-shirt|shirt|sweater; for type=bottom: pants|shorts; for type=outerwear: light_jacket|coat; otherwise null>", "color": "<main color, 1-2 words>", "warmth": "light|medium|warm", "label": "<short description, e.g. 'navy blue hoodie'>"}."""


def _validated_wardrobe_tags(value) -> Optional[dict]:
    """Validate and bound vision-model output before storing it in the
    wardrobe or using it to build Telegram buttons and prompts."""
    if not isinstance(value, dict):
        return None
    item_type = value.get("type")
    if not isinstance(item_type, str) or item_type not in {"top", "bottom", "outerwear", "shoes", "accessory"}:
        return None
    valid_subtypes = {
        "top": {"t-shirt", "shirt", "sweater"},
        "bottom": {"pants", "shorts"},
        "outerwear": {"light_jacket", "coat"},
        "shoes": set(),
        "accessory": set(),
    }
    subtype = value.get("subtype")
    if not isinstance(subtype, str) or subtype not in valid_subtypes[item_type]:
        subtype = None
    warmth = value.get("warmth")
    if not isinstance(warmth, str) or warmth not in {"light", "medium", "warm"}:
        warmth = "medium"

    def short_text(key, limit, fallback=""):
        raw = value.get(key)
        if not isinstance(raw, str):
            return fallback
        return " ".join(raw.split())[:limit].strip() or fallback

    return {
        "type": item_type,
        "subtype": subtype,
        "color": short_text("color", 32),
        "warmth": warmth,
        # The wardrobe menu appends type and subtype to the label in a
        # Telegram button (maximum 64 characters).
        "label": short_text("label", 32, "clothing item"),
    }


async def tag_wardrobe_item(image_bytes: bytes, user_id: Optional[int] = None) -> Optional[dict]:
    """Auto-tag a newly uploaded clothing-item photo via GWDG's vision
    model (GWDG_VISION_MODEL), falling back to Cloudflare Workers AI's
    free-tier one. Returns {"type", "color", "warmth", "label"} or None on
    any failure (missing config, non-2xx, unparsable JSON); the caller must
    not catalog a garbage entry on None, and should let the user retry."""
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    raw = await _call_gwdg_chat(
        [{"role": "user", "content": [
            {"type": "text", "text": TAG_WARDROBE_ITEM_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        GWDG_VISION_MODEL, timeout=30,
        can_send=lambda: personal_ai_allowed(user_id),
    )
    tags = _validated_wardrobe_tags(_extract_json_object(raw)) if raw else None
    if tags:
        return tags
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return None
    url = CLOUDFLARE_AI_URL.format(account_id=CLOUDFLARE_ACCOUNT_ID, model=CLOUDFLARE_VISION_MODEL)
    try:
        async with shared_http() as session:
            if not personal_ai_allowed(user_id):
                return None
            async with session.post(
                url,
                headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"},
                json={
                    "messages": [
                        {"role": "user", "content": [
                            {"type": "text", "text": TAG_WARDROBE_ITEM_PROMPT},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ]},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.warning("Cloudflare tagging call failed: HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                raw = data.get("result", {}).get("response")
                # Some Workers AI models return the JSON already parsed.
                parsed = raw if isinstance(raw, dict) else _extract_json_object(raw)
                return _validated_wardrobe_tags(parsed)
    except Exception as e:
        logging.warning("Cloudflare tagging call failed: %s", e)
        return None


# Full-body poses for the Nano Banana try-on, one picked at random per
# image for variety. Each keeps the whole outfit visible: no crossed arms
# or props covering prints, logos, shoes or headwear.
TRYON_POSES = (
    "standing relaxed, weight on one leg, arms loosely at the sides",
    "walking toward the camera mid-stride, arms swinging naturally",
    "standing with one hand in a trouser pocket, the other arm relaxed",
    "turned three-quarters to the left, head turned back to the camera",
    "turned three-quarters to the right, head turned back to the camera",
    "standing casually, one hand lightly touching the collar or the cap",
    "relaxed stance, looking off to the side with a slight smile",
    "standing with both thumbs hooked in the front trouser pockets",
)


def _tryon_prompt(garments: list, pose: Optional[str] = None) -> str:
    """Instruction for the Nano Banana try-on: image 1 is the person, then
    one image per garment. With `pose` the person is shown in that pose
    (see TRYON_POSES), otherwise in the pose of Image 1."""
    pose_text = (
        f"and keep the original background and lighting, but show the person in this pose: {pose}. "
        "The pose must keep every garment, the shoes and any cap or beanie fully visible. "
        "Give the person a calm, relaxed, natural expression with the lips closed: at most a faint "
        "closed-mouth smile, no teeth showing, never a posed or forced smile. Soft eyes and a relaxed "
        "brow, jaw and shoulders, never tense, stern or frowning. Only the expression changes, the "
        "face stays clearly the same person."
    ) if pose else "and pose exactly as in Image 1, and keep the original background and lighting."
    garment_lines = "\n".join(f"- Image {i + 2}: {item.get('label', 'clothing item')} ({item.get('type', 'item')})" for i, (item, _) in enumerate(garments))
    return (
        "Virtual try-on. Image 1 is a photo of a person. Dress this exact person in ALL of the "
        "clothing items shown in the other images:\n"
        f"{garment_lines}\n"
        "Reproduce each garment faithfully (color, cut, fit, length, logos, prints). Layer outerwear "
        "over tops, put the shoes on the feet and any cap or beanie on the head. Keep each pair of "
        "trousers at its real length and leg width as shown in its image: full-length trousers and "
        "jeans fall all the way to the shoes with a natural break, and straight, relaxed or wide legs "
        "stay straight, relaxed or wide. Never crop, roll up or taper a hem unless the garment itself "
        "is cropped. The person always wears plain white crew socks with the shoes; they only show "
        "where the trousers naturally leave the ankle bare, so never shorten trousers to reveal "
        "them. Keep the person's "
        "face, identity, hair, skin tone and body shape exactly as in Image 1, "
        f"{pose_text} Show the full body from head to feet. Photorealistic photo, no text."
    )


GWDG_GARMENT_DESCRIBE_PROMPT = (
    "Describe the clothing item in this product photo in ONE line for an image generator: exact colors, "
    "material, fit and length, closures, hood, pockets, and every logo or print with its position, size and "
    "colors. No people, no brand history, no item name, just the description."
)


async def describe_wardrobe_item(item: dict, image_bytes: bytes,
                                user_id: Optional[int] = None) -> Optional[str]:
    """One-line visual description of a garment photo by GWDG's vision
    model, stored on the wardrobe item (see add_wardrobe_item) so the GWDG
    try-on can dress the reference photo from text without describing the
    garments again each time. None if GWDG is unset or the call failed."""
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    content = [
        {"type": "text", "text": f"{GWDG_GARMENT_DESCRIBE_PROMPT}\nItem: {item.get('label', 'clothing item')} ({item.get('type', 'item')})"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")}},
    ]
    return await _call_gwdg_chat(
        [{"role": "user", "content": content}], GWDG_VISION_MODEL,
        temperature=0.2, timeout=30,
        can_send=lambda: personal_ai_allowed(user_id),
    )


async def generate_outfit_tryon_gemini(reference_bytes: bytes, garments: list,
                                      user_id: Optional[int] = None) -> Optional[bytes]:
    """Dress the reference photo in every garment at once via a Gemini image
    model on fal.ai (FAL_GEMINI_TRYON_MODEL). garments is [(item, bytes)]
    in outer-to-inner order, shoes and headwear included; unlike the
    dedicated try-on models, this one sees the actual shoe/hat photos, so
    no separate text-described accessory pass is needed. Returns None if
    unconfigured or the call failed, so the caller falls back to FASHN."""
    if user_id is None or not personal_ai_allowed(user_id) or not FAL_KEY or not FAL_GEMINI_TRYON_MODEL or not garments:
        return None
    try:
        import fal_client
    except ImportError:
        logging.warning("generate_outfit_tryon_gemini: fal_client not installed")
        return None

    paths = []
    try:
        urls = []
        for data in [reference_bytes] + [g[1] for g in garments]:
            if not personal_ai_allowed(user_id):
                return None
            path = os.path.join(tempfile.gettempdir(), f"gemini_tryon_{uuid.uuid4().hex}.jpg")
            paths.append(path)
            await awrite_bytes(path, data)
            urls.append(await fal_client.upload_file_async(Path(path)))

        if not personal_ai_allowed(user_id):
            return None
        result = await fal_client.subscribe_async(
            FAL_GEMINI_TRYON_MODEL,
            arguments={"prompt": _tryon_prompt(garments, random.choice(TRYON_POSES)), "image_urls": urls, "num_images": 1, "output_format": "png"},
        )
        image_url = result["images"][0]["url"]
        async with shared_http() as session:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    return await resp.read()
        return None
    except Exception as e:
        logging.warning("Gemini try-on failed: %s", e)
        return None
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass


async def generate_outfit_tryon_fashn(reference_bytes: bytes, upper_bytes: Optional[bytes],
                                      lower_bytes: Optional[bytes],
                                      user_id: Optional[int] = None) -> Optional[bytes]:
    """Dress the reference photo via FASHN's try-on model (fal.ai-hosted,
    paid, ~$0.075/image) - the primary provider when FAL_KEY is set, since
    a live comparison against the free Spaces below showed it preserves
    the actual garment (cut/taper, logo sharpness) noticeably better.
    Chained the same way as generate_outfit_tryon_ootd: apply the upper
    garment, then feed that result back in as the new model photo for the
    lower garment. Each step is independently best-effort. Returns None
    if FAL_KEY isn't configured, there's nothing to apply, or both steps
    failed - caller falls back to the free OOTDiffusion path."""
    if user_id is None or not personal_ai_allowed(user_id) or not FAL_KEY or (not upper_bytes and not lower_bytes):
        return None
    try:
        import fal_client
    except ImportError:
        logging.warning("generate_outfit_tryon_fashn: fal_client not installed")
        return None

    async def _apply_garment(person_bytes: bytes, garment_bytes: bytes) -> Optional[bytes]:
        person_path = os.path.join(tempfile.gettempdir(), f"fashn_person_{uuid.uuid4().hex}.jpg")
        garment_path = os.path.join(tempfile.gettempdir(), f"fashn_garment_{uuid.uuid4().hex}.jpg")
        try:
            if not personal_ai_allowed(user_id):
                return None
            await awrite_bytes(person_path, person_bytes)
            await awrite_bytes(garment_path, garment_bytes)
            person_url = await fal_client.upload_file_async(Path(person_path))
            if not personal_ai_allowed(user_id):
                return None
            garment_url = await fal_client.upload_file_async(Path(garment_path))
            if not personal_ai_allowed(user_id):
                return None
            result = await fal_client.subscribe_async(
                FAL_TRYON_MODEL,
                arguments={"model_image": person_url, "garment_image": garment_url},
            )
            image_url = result["images"][0]["url"]
            async with shared_http() as session:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        return await resp.read()
            return None
        except Exception as e:
            logging.warning("FASHN try-on step failed: %s", e)
            return None
        finally:
            for p in (person_path, garment_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    current = reference_bytes
    applied_any = False
    if upper_bytes and personal_ai_allowed(user_id):
        result = await _apply_garment(current, upper_bytes)
        if result:
            current = result
            applied_any = True
    if lower_bytes and personal_ai_allowed(user_id):
        result = await _apply_garment(current, lower_bytes)
        if result:
            current = result
            applied_any = True

    return current if applied_any else None


async def generate_outfit_tryon_ootd(reference_bytes: bytes, upper_bytes: Optional[bytes],
                                     lower_bytes: Optional[bytes],
                                     user_id: Optional[int] = None) -> Optional[bytes]:
    """Dress the reference photo via levihsu/OOTDiffusion's dress-code
    endpoint (process_dc) - unlike yisol/IDM-VTON, this exposes a
    category selector (Upper-body/Lower-body/Dress), so it can be
    CHAINED: apply the upper garment first, then feed that result back in
    as the new person photo for the lower garment, actually dressing both
    halves instead of leaving the lower body untouched. Each step is
    independently best-effort (a failed upper-body step still lets the
    lower-body step run against the original reference). Returns None if
    neither step produced an image, or if there's nothing to apply.
    Quality depends heavily on the reference photo being a plain-
    background, front-facing, full-body shot - not guaranteed otherwise,
    this is a free shared-GPU community demo, not a production API."""
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        logging.warning("generate_outfit_tryon_ootd: gradio_client not installed")
        return None
    if not upper_bytes and not lower_bytes:
        return None

    tmp_paths = []

    def _write_tmp(data: bytes) -> str:
        path = os.path.join(tempfile.gettempdir(), f"ootd_{uuid.uuid4().hex}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        tmp_paths.append(path)
        return path

    def _call(person_path, garm_path, category):
        client = Client(HF_SPACE_VTON, token=HF_TOKEN or None)
        return client.predict(
            vton_img=handle_file(person_path),
            garm_img=handle_file(garm_path),
            category=category,
            # 40 steps noticeably sharpens small garment logos/text over the
            # default 20 (confirmed by live comparison). The seed is pinned
            # (not randomized) - a different seed was observed hallucinating
            # an extra object (a bag) onto the shoulder on one test run, so
            # a fixed seed is the safer choice even though it means every
            # generation for a given photo+garment pair looks the same.
            n_samples=1, n_steps=40, image_scale=2.0, seed=42,
            api_name="/process_dc",
        )

    person_path = _write_tmp(reference_bytes)
    applied_any = False
    try:
        if upper_bytes and personal_ai_allowed(user_id):
            garm_path = _write_tmp(upper_bytes)
            try:
                result = await asyncio.to_thread(_call, person_path, garm_path, "Upper-body")
                output_path = result[0]["image"] if result else None
                if output_path and os.path.exists(output_path):
                    person_path = output_path
                    applied_any = True
            except Exception as e:
                logging.warning("OOTDiffusion upper-body step failed: %s", e)

        if lower_bytes and personal_ai_allowed(user_id):
            garm_path = _write_tmp(lower_bytes)
            try:
                result = await asyncio.to_thread(_call, person_path, garm_path, "Lower-body")
                output_path = result[0]["image"] if result else None
                if output_path and os.path.exists(output_path):
                    person_path = output_path
                    applied_any = True
            except Exception as e:
                logging.warning("OOTDiffusion lower-body step failed: %s", e)

        if not applied_any:
            return None
        return await aread_bytes(person_path)
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass


async def generate_outfit_tryon_flux(reference_bytes: bytes, item_images: list[bytes],
                                     user_id: Optional[int] = None) -> Optional[bytes]:
    """Fallback try-on image via Cloudflare Workers AI's FLUX.2 [dev] -
    a general multi-reference image-editing model (not purpose-built for
    try-on like IDM-VTON, used only when that Space is unavailable). Takes
    the reference photo plus up to 3 item photos as reference images.
    Returns image bytes, or None on any failure."""
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return None
    url = CLOUDFLARE_AI_URL.format(account_id=CLOUDFLARE_ACCOUNT_ID, model=CLOUDFLARE_IMAGE_MODEL)
    try:
        # FLUX.2 [dev] requires multipart/form-data even with no file inputs
        # (confirmed against a live call - a plain JSON body is rejected
        # with "required properties at '/' are 'multipart'"), with each
        # reference image in its own field: image, image2, image3, image4
        # (up to 4 references total, confirmed working with 2). The
        # response is JSON with a base64 "image" field, not raw bytes.
        form = aiohttp.FormData()
        form.add_field(
            "prompt",
            "Dress the person shown in the first reference image with the clothing items shown in the following reference images. Keep the person's face, body, pose, and background realistic and unchanged.",
        )
        form.add_field("image", reference_bytes, filename="reference.jpg", content_type="image/jpeg")
        for i, item_bytes in enumerate(item_images[:3], start=2):
            form.add_field(f"image{i}", item_bytes, filename=f"item{i}.jpg", content_type="image/jpeg")

        async with shared_http() as session:
            if not personal_ai_allowed(user_id):
                return None
            async with session.post(
                url,
                headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.warning("Cloudflare FLUX.2 call failed: HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                image_b64 = data.get("result", {}).get("image")
                return base64.b64decode(image_b64) if image_b64 else None
    except Exception as e:
        logging.warning("Cloudflare FLUX.2 call failed: %s", e)
        return None


def _describe_item_for_prompt(item: dict) -> str:
    color = (item.get("color") or "").strip()
    label = item.get("label") or "item"
    return f"{color} {label}".strip()


async def generate_outfit_tryon_accessories(reference_bytes: bytes, shoes_item: Optional[dict],
                                            headwear_item: Optional[dict],
                                            user_id: Optional[int] = None) -> Optional[bytes]:
    """Add shoes/headwear via black-forest-labs/FLUX.1-Kontext-Dev - a
    general instruction-based image editor (not garment-category-locked
    like OOTDiffusion/IDM-VTON), so it can place accessories neither of
    those support. Described in TEXT only (color + label), not from the
    actual wardrobe photo, since Kontext takes one image + one instruction,
    not a reference garment image - so exact styling/logos won't match
    perfectly, just the general look.

    Both accessories are requested in a SINGLE combined call rather than
    two sequential ones - confirmed via live comparison that chaining
    separate hat-then-shoes calls compounds visible distortion (warped
    face/proportions) across each successive edit, while one combined
    instruction stays clean. Returns None if there's nothing to add or
    the call fails - caller keeps the un-accessorized image rather than
    nothing."""
    if user_id is None or not personal_ai_allowed(user_id) or (not shoes_item and not headwear_item):
        return None
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        logging.warning("generate_outfit_tryon_accessories: gradio_client not installed")
        return None

    parts = []
    if headwear_item:
        parts.append(f"a small fitted {_describe_item_for_prompt(headwear_item)} on top of his head, not covering his face or eyes")
    if shoes_item:
        # House rule: always plain white socks, same as generate_outfit_tryon_gemini.
        parts.append(f"{_describe_item_for_prompt(shoes_item)} with plain white crew socks on his feet, replacing his current shoes")
    instruction = "Add " + " and ".join(parts) + ". Keep his face, body proportions, pose, clothing and background exactly unchanged."

    ref_path = os.path.join(tempfile.gettempdir(), f"kontext_{uuid.uuid4().hex}.jpg")
    try:
        await awrite_bytes(ref_path, reference_bytes)
        if not personal_ai_allowed(user_id):
            return None

        def _call():
            client = Client(HF_SPACE_ACCESSORIES, token=HF_TOKEN or None)
            return client.predict(
                input_image=handle_file(ref_path),
                prompt=instruction,
                seed=7, randomize_seed=False, guidance_scale=2.5, steps=20,
                api_name="/infer",
            )

        result = await asyncio.to_thread(_call)
        output_path = result[0] if isinstance(result, (list, tuple)) else result
        if output_path and os.path.exists(output_path):
            return await aread_bytes(output_path)
        return None
    except Exception as e:
        logging.warning("generate_outfit_tryon_accessories failed: %s", e)
        return None
    finally:
        try:
            os.remove(ref_path)
        except OSError:
            pass


async def generate_outfit_tryon_image(bot, reference_bytes: bytes, chosen_items: list[dict],
                                      user_id: Optional[int] = None) -> Optional[bytes]:
    """Dress the reference photo in the chosen outfit. First tries the
    Gemini model with every visible item in one call (see
    generate_outfit_tryon_gemini), then GWDG's free describe-and-edit
    (see generate_outfit_tryon_gwdg). If both fail, picks one
    representative upper-body garment (the outermost layer if the outfit
    has layering, e.g. a coat over a t-shirt, since none of these models
    stack multiple garments visually in one region) and one bottom,
    downloads their bytes, and tries FASHN (paid, via fal.ai) first if
    FAL_KEY is configured - confirmed via live comparison to preserve the
    actual garment better than the free options - falling back to the
    free OOTDiffusion Space (chained upper+lower, see
    generate_outfit_tryon_ootd), then to Cloudflare's general FLUX.2
    editing model if neither is available. If the outfit also
    includes shoes/a cap-or-beanie (see _add_mandatory_shoes_and_headwear),
    one more pass via generate_outfit_tryon_accessories adds them - text-
    described, not photo-matched, and best-effort: a failure there just
    means the base outfit image goes out without them. Returns None only
    if the base top/bottom dressing itself produced nothing - caller must
    degrade to a text-only outfit message in that case."""
    if user_id is None or not personal_ai_allowed(user_id):
        return None
    shoes_item = next((it for it in chosen_items if it.get("type") == "shoes"), None)
    headwear_item = next(
        (it for it in chosen_items if it.get("type") == "accessory" and any(kw in it.get("label", "").lower() for kw in _HEADWEAR_KEYWORDS)),
        None,
    )

    # Socks, watches etc. are left out: barely visible, and every extra
    # reference image dilutes the model's attention on the main garments.
    visible = [it for it in chosen_items if it.get("type") in ("outerwear", "top", "bottom")]
    visible += [it for it in (shoes_item, headwear_item) if it]
    garments = []
    for it in visible:
        if not personal_ai_allowed(user_id):
            return None
        data = await _fetch_wardrobe_item_bytes(bot, it)
        if data:
            garments.append((it, data))
    for provider in (generate_outfit_tryon_gemini,):
        if not personal_ai_allowed(user_id):
            return None
        image_bytes = await provider(reference_bytes, garments, user_id=user_id)
        if image_bytes:
            return image_bytes
    if not personal_ai_allowed(user_id):
        return None
    image_bytes = await generate_outfit_tryon_gwdg(reference_bytes, garments, user_id=user_id)
    if image_bytes:
        return image_bytes

    upper_item = next((it for it in reversed(chosen_items) if it.get("type") == "outerwear"), None) \
        or next((it for it in chosen_items if it.get("type") == "top"), None)
    lower_item = next((it for it in chosen_items if it.get("type") == "bottom"), None)

    upper_bytes = await _fetch_wardrobe_item_bytes(bot, upper_item) if upper_item else None
    if not personal_ai_allowed(user_id):
        return None
    lower_bytes = await _fetch_wardrobe_item_bytes(bot, lower_item) if lower_item else None

    if not personal_ai_allowed(user_id):
        return None
    image_bytes = await generate_outfit_tryon_fashn(reference_bytes, upper_bytes, lower_bytes, user_id=user_id)
    if not image_bytes:
        if not personal_ai_allowed(user_id):
            return None
        image_bytes = await generate_outfit_tryon_ootd(reference_bytes, upper_bytes, lower_bytes, user_id=user_id)
    if not image_bytes:
        if not personal_ai_allowed(user_id):
            return None
        image_bytes = await generate_outfit_tryon_flux(
            reference_bytes, [b for b in (upper_bytes, lower_bytes) if b], user_id=user_id
        )
    if not image_bytes:
        return None

    if shoes_item or headwear_item:
        if not personal_ai_allowed(user_id):
            return None
        accessorized = await generate_outfit_tryon_accessories(
            image_bytes, shoes_item, headwear_item, user_id=user_id
        )
        if accessorized:
            return accessorized
    return image_bytes
