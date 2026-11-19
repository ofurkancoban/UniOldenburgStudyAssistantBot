"""Wardrobe catalog, outfit picking and try-on image generation.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import ipaddress
import logging
import os
import random
import re
import socket
import tempfile
import uuid
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import aiohttp
import base64
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from studip_utils import TZ_BERLIN, aread_bytes, awrite_bytes, redact_url

from unibot.config import (
    ADMIN_USER_IDS,
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
from unibot.preferences import get_pref, in_quiet_hours, set_pref
from unibot import db
from unibot.storage import (
    add_outfit_likes,
    add_wardrobe_item,
    clear_wardrobe_items,
    dirty_item_ids,
    get_cached_outfit_today,
    get_days_since_worn,
    get_outfit_history,
    get_outfit_likes,
    get_outfit_stats,
    get_outfit_weather_baseline,
    get_recently_worn_item_ids,
    get_wardrobe_entry,
    mark_items_clean,
    mark_outfit_weather_alerted,
    NEVER_WORN_DAYS,
    record_outfit_stat,
    record_worn_items,
    remove_wardrobe_item,
    save_cached_outfit_today,
    save_outfit_weather_baseline,
    save_wardrobe_item_descriptions,
    shared_http,
    update_wardrobe_item,
)
from unibot.telegram_ui import (
    user_data,
    _download_telegram_file,
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
)
from unibot.auth import get_user_session, is_user_admin
from unibot.ai import (
    _call_gwdg_chat,
    _call_openrouter,
    personal_ai_allowed,
    _extract_json_object,
    generate_outfit_tryon_gwdg,
)
from unibot.weather import get_afternoon_outlook, get_weather_snapshot


CLOUDFLARE_AI_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def outfit_caption(outfit: dict, heading: str = "Today's outfit") -> str:
    """HTML caption for an outfit pick (fresh or cached): just the chosen
    items as a bullet list. Falls back to the description for a cached pick
    saved before item labels were stored. Labels can hold a bare "&"
    ("Jack & Jones"), so they're escaped."""
    labels = outfit.get("item_labels") or [it["label"] for it in outfit.get("chosen_items", [])]
    note = f"\n\n{html.escape(outfit['laundry_note'])}" if outfit.get("laundry_note") else ""
    if not labels:
        return f"👔 <b>{heading}:</b> {html.escape(outfit['description'])}{note}"
    return f"👔 <b>{heading}:</b>\n" + "\n".join(f"• {html.escape(label)}" for label in labels) + note


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
Variety: when several items fit the weather equally well, prefer the ones marked "not worn recently" or worn longest ago over ones worn a few days ago, and treat "[liked before]" as a small tie-breaker only. If a Schedule line is given, use it only as a tie-breaker: a long day away from home or classes on both campuses favour practical layers and comfortable shoes, a day without classes favours the most relaxed option. The weather rules above always win.
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


def _apply_laundry(items: list[dict], dirty: set) -> tuple[list[dict], str]:
    """Drop the dirty tops and bottoms from the candidates. If every top (or
    every bottom) is dirty they all stay, so an outfit is still possible.
    Returns the candidates and a short note when the clean stock runs low."""
    clean = [it for it in items if it["id"] not in dirty]
    notes = []
    for item_type in ("top", "bottom"):
        total = [it for it in items if it.get("type") == item_type]
        left = sum(1 for it in clean if it.get("type") == item_type)
        if not total or left == len(total):
            continue
        if left == 0:
            clean += total
            notes.append(f"no clean {item_type}s left, picked from the laundry")
        elif left <= 1:
            notes.append(f"{left} clean {item_type} left")
    return clean, ("🧺 Laundry: " + "; ".join(notes) + ".") if notes else ""


def _rotation_rank(item_id: str, days_since_worn: dict, likes: dict) -> float:
    """Lower sorts first: the longer an item has not been worn the better, a
    liked item counts as worn a little longer ago (a tie-breaker only)."""
    return -float(days_since_worn.get(item_id, NEVER_WORN_DAYS)) - 0.5 * min(likes.get(item_id, 0), 6)


def _prefer_unworn(pool: list[dict], recent_ids: set) -> list[dict]:
    """Stable-sort a candidate pool so recently-worn items (see
    get_recently_worn_item_ids) come after everything else, and within each
    group the items worn longest ago come first (the "_rank" the picker adds
    from the wear history). The "first match" selection below then naturally
    rotates the wardrobe, but still falls back to a recently-worn item rather
    than nothing when it's the only option in that subtype."""
    if not recent_ids and not any("_rank" in it for it in pool):
        return pool
    return sorted(pool, key=lambda it: (it["id"] in recent_ids, it.get("_rank", 0.0)))


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
                                   user_id: Optional[int] = None, day_context: Optional[str] = None,
                                   for_day=None) -> Optional[dict]:
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
    for_day = for_day or datetime.now(TZ_BERLIN).date()
    days_since = get_days_since_worn(user_id, for_day)
    likes = get_outfit_likes(user_id)
    # Work on copies carrying the rotation rank, so the item dicts stored in the
    # wardrobe stay untouched; the rank is dropped again before returning.
    items = [dict(it, _rank=_rotation_rank(it["id"], days_since, likes)) for it in items]
    items, laundry_note = _apply_laundry(items, dirty_item_ids(user_id, for_day))

    def _history_note(item_id: str) -> str:
        note = f" [worn {days_since[item_id]} days ago]" if item_id in days_since else " [not worn recently]"
        return note + (" [liked before]" if likes.get(item_id) else "")

    lines = [
        f"- id={it['id']}: {it['label']} (type={it['type']}, subtype={it.get('subtype') or 'n/a'}, color={it['color']}, warmth={it['warmth']})"
        + (" [worn in the last couple days, prefer an alternative if one fits]" if it["id"] in recent_ids else "")
        + _history_note(it["id"])
        for it in items
    ]
    if weather:
        is_wet = weather.get("is_wet")
        weather_line = f"Weather: {weather.get('description', 'unknown')}, {weather.get('temp_min')}-{weather.get('temp_max')}°C ({band}), {'rain expected' if is_wet else 'dry'}."
        if _outerwear_required(weather):
            weather_line += " An outerwear item is required today."
    else:
        weather_line = "Weather: no data available today - pick something reasonably versatile (mild band)."

    day_line = f"\n{day_context}" if day_context else ""
    user_text = f"{weather_line}{day_line}\n\nWardrobe:\n" + "\n".join(lines)
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
        record_outfit_stat("pick_fallback")
        chosen_items = _heuristic_outfit_pick(items, weather, recent_ids)
    if not chosen_items:
        return None
    else:
        chosen_items = _normalize_model_outfit(items, chosen_items, weather, recent_ids)
    pre_ids = {it["id"] for it in chosen_items}
    chosen_items = _add_required_outerwear(items, chosen_items, weather, recent_ids)
    chosen_items = _add_mandatory_shoes_and_headwear(items, chosen_items)
    forced_extras = [it["label"] for it in chosen_items if it["id"] not in pre_ids]
    chosen_items = [{k: v for k, v in it.items() if k != "_rank"} for it in chosen_items]

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
    return {"chosen_items": chosen_items, "description": description, "color_tip": color_tip,
            "laundry_note": laundry_note}


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
    if items:
        keyboard.append([InlineKeyboardButton("✏️ Fix item types", callback_data="wardrobe_edit_list"),
                         InlineKeyboardButton("🧺 Laundry", callback_data="wardrobe_laundry")])
        keyboard.append([InlineKeyboardButton("🔎 What's missing?", callback_data="wardrobe_gaps"),
                         InlineKeyboardButton("📜 History", callback_data="wardrobe_history")])
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


def _find_item(user_id, item_id: str) -> Optional[dict]:
    return next((it for it in get_wardrobe_entry(user_id).get("items", []) if it["id"] == item_id), None)


def _edit_list_view(user_id) -> tuple:
    items = get_wardrobe_entry(user_id).get("items", [])
    rows = [[InlineKeyboardButton(f"✏️ {it['label']} ({it['type']}/{it.get('subtype') or '-'})",
                                  callback_data=f"wardrobe_edit|{it['id']}")] for it in items]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="wardrobe_edit_done")])
    return "✏️ <b>Fix item types</b>\nPick the piece that was tagged wrongly.", InlineKeyboardMarkup(rows)


def _item_edit_view(user_id, item_id: str) -> Optional[tuple]:
    item = _find_item(user_id, item_id)
    if not item:
        return None

    def mark(current, value):
        return ("✅ " if current == value else "") + value

    def row(field, values, current):
        return [InlineKeyboardButton(mark(current, v), callback_data=f"wardrobe_set|{item_id}|{field}|{v}") for v in values]

    text = (f"✏️ <b>{html.escape(item['label'])}</b>\n"
            f"Type: {item['type']}\nSubtype: {item.get('subtype') or '-'}\nWarmth: {item.get('warmth') or 'medium'}")
    rows = [row("type", WARDROBE_TYPES[:3], item["type"]), row("type", WARDROBE_TYPES[3:], item["type"])]
    subtypes = WARDROBE_SUBTYPES.get(item["type"], ())
    if subtypes:
        rows.append(row("subtype", subtypes, item.get("subtype")))
    rows.append(row("warmth", WARDROBE_WARMTH, item.get("warmth") or "medium"))
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="wardrobe_edit_list")])
    return text, InlineKeyboardMarkup(rows)


def _label_words(label) -> set:
    return set(re.findall(r"[a-z0-9]+", str(label or "").lower())) - {"and", "the", "with"}


def _looks_alike(a: dict, b: dict) -> bool:
    """Same type, subtype and color, and mostly the same words in the label."""
    if a.get("type") != b.get("type") or a.get("subtype") != b.get("subtype"):
        return False
    if str(a.get("color") or "").lower() != str(b.get("color") or "").lower():
        return False
    words_a, words_b = _label_words(a.get("label")), _label_words(b.get("label"))
    return bool(words_a and words_b) and len(words_a & words_b) / len(words_a | words_b) >= 0.5


def find_similar_items(items: list) -> list:
    """Pairs of items that look like the same garment added twice."""
    return [(a, b) for i, a in enumerate(items) for b in items[i + 1:] if _looks_alike(a, b)]


def wardrobe_gaps(items: list) -> list:
    """What the wardrobe lacks for the temperature bands the pick uses, plus
    possible duplicates. Empty when nothing obvious is missing."""
    def count(item_type, subtype=None):
        return sum(1 for it in items if it.get("type") == item_type and (subtype is None or it.get("subtype") == subtype))

    gaps = []
    if not count("bottom", "shorts"):
        gaps.append("No shorts: on days of 18°C and up the pick uses pants.")
    if not count("top", "sweater"):
        gaps.append("No sweater: days under 5°C have no warm layer under the jacket.")
    if not count("outerwear", "coat"):
        gaps.append("No coat: days under 12°C rely on a light jacket.")
    if not count("outerwear", "light_jacket"):
        gaps.append("No light jacket: mild or rainy days have no outer layer.")
    if count("top") < 4:
        gaps.append(f"Only {count('top')} top(s): with washing in between, tops repeat within a few days.")
    if count("bottom") < 3:
        gaps.append(f"Only {count('bottom')} bottom(s): with washing in between, bottoms repeat within a few days.")
    if count("shoes") < 2:
        gaps.append(f"Only {count('shoes')} pair(s) of shoes.")
    if not any(it.get("type") == "accessory" and any(kw in str(it.get("label", "")).lower() for kw in _HEADWEAR_KEYWORDS)
               for it in items):
        gaps.append("No cap or beanie to finish an outfit.")
    for a, b in find_similar_items(items):
        gaps.append(f"Looks alike: {a['label']} and {b['label']} (remove one if it is a duplicate).")
    return gaps


_BACK_ROW = [InlineKeyboardButton("⬅️ Back", callback_data="wardrobe_edit_done")]


def _laundry_view(user_id) -> tuple:
    dirty = dirty_item_ids(user_id)
    dirty_items = [it for it in get_wardrobe_entry(user_id).get("items", []) if it["id"] in dirty]
    if not dirty_items:
        text = ("🧺 <b>Laundry</b>\nNothing is waiting to be washed. Tops and bottoms count as dirty "
                "from the day after they were in an outfit, and are left out of the picks until you mark them washed.")
        return text, InlineKeyboardMarkup([_BACK_ROW])
    rows = [[InlineKeyboardButton(f"🧺 Washed: {it['label'][:40]}", callback_data=f"wardrobe_wash|{it['id']}")]
            for it in dirty_items[:20]]
    rows.append([InlineKeyboardButton("✅ All washed", callback_data="wardrobe_wash_all")])
    rows.append(_BACK_ROW)
    return f"🧺 <b>Laundry</b>\n{len(dirty_items)} piece(s) are waiting to be washed and are left out of the picks.", InlineKeyboardMarkup(rows)


def _gaps_view(user_id) -> tuple:
    gaps = wardrobe_gaps(get_wardrobe_entry(user_id).get("items", []))
    body = "\n".join(f"• {html.escape(g)}" for g in gaps) if gaps else "Nothing obvious is missing."
    return f"🔎 <b>What's missing?</b>\n{body}", InlineKeyboardMarkup([_BACK_ROW])


def _history_view(user_id) -> tuple:
    items = {it["id"]: it for it in get_wardrobe_entry(user_id).get("items", [])}
    lines = []
    for date_str, ids in sorted(get_outfit_history(user_id).items(), reverse=True)[:14]:
        labels = [items[i]["label"] for i in ids if i in items]
        if labels:
            lines.append(f"<b>{html.escape(date_str)}</b>: {html.escape(', '.join(labels))}")
    liked = sorted(((n, items[i]["label"]) for i, n in get_outfit_likes(user_id).items() if i in items), reverse=True)[:3]
    text = "📜 <b>Outfit history</b>\n" + ("\n".join(lines) if lines else "No outfits yet.")
    if liked:
        text += "\n\n👍 Most liked: " + html.escape(", ".join(f"{label} ({n})" for n, label in liked))
    return text, InlineKeyboardMarkup([_BACK_ROW])


async def handle_wardrobe_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The wardrobe sub screens: wardrobe_edit_list / wardrobe_edit|<id> /
    wardrobe_set|<id>|<field>|<value> correct what the vision model guessed,
    wardrobe_laundry / wardrobe_wash|<id> / wardrobe_wash_all, wardrobe_gaps
    and wardrobe_history show the rest, wardrobe_edit_done goes back."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    await query.answer()
    if not db.private_context_is_current(user_id):
        return
    parts = (query.data or "").split("|")
    view = None
    if parts[0] == "wardrobe_edit_done":
        view = _wardrobe_list_view(user_id)
    elif parts[0] == "wardrobe_edit_list":
        view = _edit_list_view(user_id)
    elif parts[0] == "wardrobe_laundry":
        view = _laundry_view(user_id)
    elif parts[0] == "wardrobe_wash" and len(parts) == 2:
        mark_items_clean(user_id, [parts[1]])
        view = _laundry_view(user_id)
    elif parts[0] == "wardrobe_wash_all":
        mark_items_clean(user_id)
        view = _laundry_view(user_id)
    elif parts[0] == "wardrobe_gaps":
        view = _gaps_view(user_id)
    elif parts[0] == "wardrobe_history":
        view = _history_view(user_id)
    elif parts[0] == "wardrobe_edit" and len(parts) == 2:
        view = _item_edit_view(user_id, parts[1]) or _edit_list_view(user_id)
    elif parts[0] == "wardrobe_set" and len(parts) == 4:
        _, item_id, field, value = parts
        item = _find_item(user_id, item_id)
        if item:
            changes = None
            if field == "type" and value in WARDROBE_TYPES:
                keep = item.get("subtype") if item.get("subtype") in WARDROBE_SUBTYPES[value] else None
                changes = {"type": value, "subtype": keep}
            elif field == "subtype" and value in WARDROBE_SUBTYPES.get(item["type"], ()):
                changes = {"subtype": value}
            elif field == "warmth" and value in WARDROBE_WARMTH:
                changes = {"warmth": value}
            if changes:
                update_wardrobe_item(user_id, item_id, **changes)
        view = _item_edit_view(user_id, item_id) or _edit_list_view(user_id)
    if view is None:
        return
    try:
        await query.edit_message_text(view[0], parse_mode="HTML", reply_markup=view[1])
    except TelegramError as ignored_error:
        logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)  # e.g. nothing changed


# ── Importing items from image links ──────────────────────────────────────────
WARDROBE_IMPORT_LIMIT = 20
MAX_IMPORT_IMAGE_BYTES = 8 * 1024 * 1024
_IMPORT_URL_RE = re.compile(r"https?://[^\s<>\"']+")


async def _is_public_https_url(url: str) -> bool:
    """Only https links to hosts that resolve to public addresses: the bot must
    not be pointed at its own machine or network."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        return bool(infos) and all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
    except (OSError, ValueError):
        return False


async def _fetch_public_image(url: str) -> Optional[bytes]:
    """The image behind a public https link (no redirects, an image content
    type, at most MAX_IMPORT_IMAGE_BYTES), or None."""
    if not await _is_public_https_url(url):
        return None
    try:
        async with shared_http() as session:
            async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200 or not (resp.content_type or "").startswith("image/"):
                    return None
                if resp.content_length and resp.content_length > MAX_IMPORT_IMAGE_BYTES:
                    return None
                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_IMPORT_IMAGE_BYTES:
                        return None
                return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        logging.warning("Wardrobe import download failed for %s: %s", redact_url(url), e)
        return None


async def import_wardrobe_urls(user_id, urls: list) -> dict:
    """Tag, describe and catalog the images behind `urls` (skipping ones already
    in the wardrobe). {"added": [labels], "skipped": n, "failed": [(url, why)],
    "stopped": True if personal AI use was switched off midway}."""
    existing = {it.get("image_url") for it in get_wardrobe_entry(user_id).get("items", [])}
    result: dict = {"added": [], "skipped": 0, "failed": [], "stopped": False, "similar": []}
    for url in urls:
        if url in existing:
            result["skipped"] += 1
            continue
        if not db.private_context_is_current(user_id) or not personal_ai_allowed(user_id):
            result["stopped"] = True
            break
        data = await _fetch_public_image(url)
        if not data:
            result["failed"].append((url, "could not download an image"))
            continue
        tags = await tag_wardrobe_item(data, user_id)
        if not tags:
            result["failed"].append((url, "could not tell what it is"))
            continue
        tags["description"] = await describe_wardrobe_item(tags, data, user_id=user_id)
        before = list(get_wardrobe_entry(user_id).get("items", []))
        new_item = add_wardrobe_item(user_id, tags, image_url=url)
        existing.add(url)
        result["added"].append(tags["label"])
        result["similar"] += [(new_item["label"], other["label"]) for other in before if _looks_alike(new_item, other)]
    return result


async def backfill_wardrobe_descriptions(user_id, bot=None) -> int:
    """Write the one-line description for items that have none (items added in
    bulk), so the GWDG try-on can dress from text. Returns how many were filled."""
    descriptions: dict = {}
    for item in get_wardrobe_entry(user_id).get("items", []):
        if item.get("description") or (item.get("file_id") and bot is None):
            continue
        data = await _fetch_wardrobe_item_bytes(bot, item)
        text = await describe_wardrobe_item(item, data, user_id=user_id) if data else None
        if text:
            descriptions[item["id"]] = text
    if descriptions:
        save_wardrobe_item_descriptions(descriptions, user_id)
    return len(descriptions)


async def wardrobe_import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wardrobe_import <image links>: add clothing items from product photos."""
    user = update.effective_user
    if not user or not is_user_admin(user.id):
        await update.message.reply_text("⛔ This command is restricted to administrators.")
        return
    user_id = user.id
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await update.message.reply_text(
            "🔒 Clothing photos are sent to an outside AI service for tagging. "
            "Allow personal data use under /settings → 🔒 Privacy first."
        )
        return
    words = (update.message.text or "").split(None, 1)
    urls = list(dict.fromkeys(u.rstrip(".,;)") for u in _IMPORT_URL_RE.findall(words[1] if len(words) > 1 else "")))
    if not urls:
        await update.message.reply_text(
            f"📸 Send /wardrobe_import followed by up to {WARDROBE_IMPORT_LIMIT} https links to clothing photos "
            "(one per line). Each is tagged and added to your wardrobe."
        )
        return
    if len(urls) > WARDROBE_IMPORT_LIMIT:
        await update.message.reply_text(f"Send at most {WARDROBE_IMPORT_LIMIT} links at a time.")
        return
    status_msg = await update.message.reply_text(f"👔 Importing {len(urls)} link(s), this takes a moment...", disable_notification=True)
    result = await import_wardrobe_urls(user_id, urls)
    lines = [f"👔 Added {len(result['added'])} item(s)."]
    lines += [f"• {html.escape(label)}" for label in result["added"]]
    if result["skipped"]:
        lines.append(f"Skipped {result['skipped']} already in your wardrobe.")
    for new_label, other_label in result["similar"]:
        lines.append(f"↔️ {html.escape(new_label)} looks like {html.escape(other_label)}, remove one if it is a duplicate.")
    for url, why in result["failed"]:
        lines.append(f"⚠️ {html.escape(redact_url(url)[:60])}: {why}")
    if result["stopped"]:
        lines.append("🔒 Personal AI use was turned off, the import stopped.")
    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")


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


def outfit_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 I like it", callback_data="outfit_fb|like"),
                                  InlineKeyboardButton("🔁 Another one", callback_data="outfit_fb|another")]])


async def outfit_day_context(user_id, day) -> Optional[str]:
    """One line about the day's classes for the outfit pick: how many, the
    time span and whether both campuses are involved. Never course names or
    rooms, since this text goes to an outside AI service. None if the
    calendar cannot be read."""
    from unibot.morning import _fetch_today_events  # morning imports this module
    from unibot.study_day import campus_of
    try:
        session = get_user_session(user_id)
        if not await session.is_logged_in():
            return None
        _, today_events, ok = await _fetch_today_events(session, day, user_id=user_id)
    except Exception as ignored_error:
        logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)
        return None
    if not ok:
        return None
    live = [ev for ev in today_events if not ev.get("is_cancelled")]
    if not live:
        return "Schedule: no classes today."
    times = sorted(t.zfill(5) for ev in live for t in re.findall(r"\b\d{1,2}:\d{2}\b", ev.get("time") or ""))
    campuses = {c for c in (campus_of(ev.get("location")) for ev in live) if c}
    line = f"Schedule: {len(live)} class(es)"
    if times:
        line += f" between {times[0]} and {times[-1]}"
    if len(campuses) > 1:
        line += ", on both campuses"
    return line + "."


async def _outfit_ready(message, user_id) -> Optional[tuple]:
    """(items, reference photo id) once an outfit can be made, otherwise tell
    the student what is missing and return None."""
    if not personal_ai_allowed(user_id):
        await message.reply_text(
            "🔒 Outfit suggestions use your wardrobe and reference photo with outside AI services. "
            "Allow personal data use under /settings → 🔒 Privacy first."
        )
        return None
    entry = get_wardrobe_entry(user_id)
    items = entry.get("items", [])
    ref_file_id = entry.get("reference_photo_file_id")
    if not items:
        await message.reply_text("👔 Add at least one clothing item first - tap 📸 Add Clothing Item.")
        return None
    if not ref_file_id:
        await message.reply_text("🖼️ Set your reference photo first - tap 🖼️ Set Reference Photo in /status.")
        return None
    return items, ref_file_id


async def _run_outfit_request(message, bot, user_id: int, items: list, ref_file_id: str, exclude_ids=frozenset()) -> None:
    """Pick today's outfit, dress the reference photo in it and send it with
    the like/another buttons. `exclude_ids` are items to steer away from (the
    outfit that "Another one" replaces)."""
    await _show_typing(message)
    status_msg = await message.reply_text("👔 Picking an outfit for you...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Picking an outfit for you...")
    try:
        today = datetime.now(TZ_BERLIN).date()
        weather = await get_weather_snapshot(today, user_id=user_id)
        if not db.private_context_is_current(user_id):
            return
        if not personal_ai_allowed(user_id):
            await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
            return
        recent_ids = get_recently_worn_item_ids(user_id) | set(exclude_ids)
        day_context = await outfit_day_context(user_id, today)
        outfit = await pick_outfit_from_wardrobe(items, weather, recent_ids, user_id=user_id, day_context=day_context)
        if not db.private_context_is_current(user_id):
            return
        if not personal_ai_allowed(user_id):
            await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
            return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if not outfit:
        await message.reply_text("❌ Couldn't put together an outfit right now - try again in a bit.")
        return

    caption = outfit_caption(outfit)
    reference_bytes = await _download_telegram_file(bot, ref_file_id)
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
        return
    image_bytes = await generate_outfit_tryon_image(
        bot, reference_bytes, outfit["chosen_items"], user_id=user_id
    ) if reference_bytes else None
    if not db.private_context_is_current(user_id):
        return
    if not personal_ai_allowed(user_id):
        await status_msg.edit_text("🔒 Personal AI use was turned off. The outfit request was stopped.")
        return
    photo_file_id = None
    keyboard = outfit_feedback_keyboard()
    if image_bytes:
        sent = await message.reply_photo(photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML", reply_markup=keyboard)
        photo_file_id = sent.photo[-1].file_id if sent.photo else None
    else:
        await message.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)

    record_worn_items(user_id, [it["id"] for it in outfit["chosen_items"]])
    save_cached_outfit_today(user_id, outfit["description"], photo_file_id, outfit.get("color_tip", ""),
                             [it["label"] for it in outfit["chosen_items"]],
                             [it["id"] for it in outfit["chosen_items"]])
    save_outfit_weather_baseline(user_id, weather)


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

    ready = await _outfit_ready(query.message, user_id)
    if not ready:
        return
    items, ref_file_id = ready

    # Reuse today's already-generated pick if this is a second trigger the
    # same day (e.g. morning summary already ran) - resending a cached
    # photo_file_id is instant and free, unlike burning another round of
    # quota-limited image-generation calls for the same day's outfit.
    cached = get_cached_outfit_today(user_id)
    if cached:
        caption = outfit_caption(cached)
        keyboard = outfit_feedback_keyboard() if cached.get("item_ids") else None
        if cached.get("photo_file_id"):
            await query.message.reply_photo(photo=cached["photo_file_id"], caption=caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            await query.message.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)
        return

    await _run_outfit_request(query.message, context.bot, user_id, items, ref_file_id)


async def handle_outfit_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """outfit_fb|like makes the pieces of today's outfit a little more likely
    in later picks; outfit_fb|another picks again, steering away from them."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_user_admin(user_id):
        await query.answer("⛔ This feature is restricted to administrators.", show_alert=True)
        return
    if not db.private_context_is_current(user_id):
        await query.answer()
        return
    action = (query.data or "").split("|", 1)[-1]
    cached = get_cached_outfit_today(user_id)
    item_ids = list((cached or {}).get("item_ids") or [])
    if not item_ids:
        await query.answer("That outfit is too old, ask for a new one.", show_alert=True)
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)  # one answer per outfit
    except TelegramError as ignored_error:
        logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)
    if action == "like":
        add_outfit_likes(user_id, item_ids)
        await query.answer("Noted")
        await query.message.reply_text("👍 Noted. These pieces will come up a little more often.")
        return
    await query.answer()
    ready = await _outfit_ready(query.message, user_id)
    if not ready:
        return
    items, ref_file_id = ready
    await _run_outfit_request(query.message, context.bot, user_id, items, ref_file_id, exclude_ids=set(item_ids))


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
    def flag(key):
        return bool(get_pref(user_id, key) if isinstance(user_id, int) else False)

    enabled, evening, midday = flag("outfit"), flag("outfit_evening"), flag("outfit_midday")
    text = (f"👔 <b>Outfit &amp; Wardrobe</b>\n\nDaily Outfit Suggestion: {'✅ On' if enabled else '🔲 Off'}"
            f"\nEvening preview (20:00): {'✅ On' if evening else '🔲 Off'}"
            f"\nMidday rain alert: {'✅ On' if midday else '🔲 Off'}")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if enabled else '🔲'} Daily Outfit Suggestion", callback_data="outfit_menu_toggle")],
        [InlineKeyboardButton(f"{'✅' if evening else '🔲'} Evening preview of tomorrow", callback_data="outfit_menu_evening")],
        [InlineKeyboardButton(f"{'✅' if midday else '🔲'} Midday rain alert", callback_data="outfit_menu_midday")],
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
    if (query.data or "") in ("outfit_menu_evening", "outfit_menu_midday"):
        key = "outfit_evening" if query.data == "outfit_menu_evening" else "outfit_midday"
        set_pref(user_id, key, not get_pref(user_id, key))
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


WARDROBE_TYPES = ("top", "bottom", "outerwear", "shoes", "accessory")
WARDROBE_SUBTYPES = {
    "top": ("t-shirt", "shirt", "sweater"),
    "bottom": ("pants", "shorts"),
    "outerwear": ("light_jacket", "coat"),
    "shoes": (),
    "accessory": (),
}
WARDROBE_WARMTH = ("light", "medium", "warm")


def _validated_wardrobe_tags(value) -> Optional[dict]:
    """Validate and bound vision-model output before storing it in the
    wardrobe or using it to build Telegram buttons and prompts."""
    if not isinstance(value, dict):
        return None
    item_type = value.get("type")
    if not isinstance(item_type, str) or item_type not in WARDROBE_TYPES:
        return None
    subtype = value.get("subtype")
    if not isinstance(subtype, str) or subtype not in WARDROBE_SUBTYPES[item_type]:
        subtype = None
    warmth = value.get("warmth")
    if not isinstance(warmth, str) or warmth not in WARDROBE_WARMTH:
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
            except OSError as ignored_error:
                logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)


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
                except OSError as ignored_error:
                    logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)

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
            except OSError as ignored_error:
                logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)


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
        except OSError as ignored_error:
            logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)


async def _tryon_chain(bot, reference_bytes: bytes, chosen_items: list[dict],
                       user_id: Optional[int], used: list) -> Optional[bytes]:
    """Dress the reference photo in the chosen outfit; the name of the provider
    that produced the image is appended to `used`. First tries the
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
            used.append("gemini")
            return image_bytes
    if not personal_ai_allowed(user_id):
        return None
    image_bytes = await generate_outfit_tryon_gwdg(reference_bytes, garments, user_id=user_id)
    if image_bytes:
        used.append("gwdg")
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
    provider_name = "fashn"
    image_bytes = await generate_outfit_tryon_fashn(reference_bytes, upper_bytes, lower_bytes, user_id=user_id)
    if not image_bytes:
        if not personal_ai_allowed(user_id):
            return None
        provider_name = "ootd"
        image_bytes = await generate_outfit_tryon_ootd(reference_bytes, upper_bytes, lower_bytes, user_id=user_id)
    if not image_bytes:
        if not personal_ai_allowed(user_id):
            return None
        provider_name = "flux"
        image_bytes = await generate_outfit_tryon_flux(
            reference_bytes, [b for b in (upper_bytes, lower_bytes) if b], user_id=user_id
        )
    if not image_bytes:
        return None
    used.append(provider_name)

    if shoes_item or headwear_item:
        if not personal_ai_allowed(user_id):
            return None
        accessorized = await generate_outfit_tryon_accessories(
            image_bytes, shoes_item, headwear_item, user_id=user_id
        )
        if accessorized:
            return accessorized
    return image_bytes


# ── Scheduled extras: evening preview and midday rain alert ───────────────────
async def send_evening_outfit_preview(bot, user_id: int) -> bool:
    """Tomorrow's outfit as text (no image, so no image cost), from tomorrow's
    forecast and classes. Not recorded as worn: the morning pick is the real one."""
    if not is_user_admin(user_id) or not get_pref(user_id, "outfit_evening") or not personal_ai_allowed(user_id):
        return False
    now = datetime.now(TZ_BERLIN)
    if in_quiet_hours(user_id, now):
        return False
    items = get_wardrobe_entry(user_id).get("items", [])
    if not items:
        return False
    tomorrow = now.date() + timedelta(days=1)
    weather = await get_weather_snapshot(tomorrow, user_id=user_id)
    if not weather:
        return False
    day_context = await outfit_day_context(user_id, tomorrow)
    outfit = await pick_outfit_from_wardrobe(items, weather, get_recently_worn_item_ids(user_id), user_id=user_id,
                                             day_context=day_context, for_day=tomorrow)
    if not outfit or not db.private_context_is_current(user_id) or not personal_ai_allowed(user_id):
        return False
    forecast = (f"{weather.get('emoji', '')} {html.escape(str(weather.get('description', '')))}, "
                f"{weather['temp_min']:.0f} to {weather['temp_max']:.0f}°C" + (", rain expected" if weather.get("is_wet") else ""))
    text = (outfit_caption(outfit, "Tomorrow's outfit (preview)") + f"\n\n{forecast.strip()}\n"
            "The morning pick can differ if the forecast changes.")
    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    return True


async def send_midday_weather_alert(bot, user_id: int) -> bool:
    """Once a day, if the morning forecast was dry but the afternoon turned wet."""
    if not is_user_admin(user_id) or not get_pref(user_id, "outfit_midday"):
        return False
    baseline = get_outfit_weather_baseline(user_id)
    if not baseline or baseline.get("alerted") or baseline.get("is_wet"):
        return False
    now = datetime.now(TZ_BERLIN)
    if in_quiet_hours(user_id, now):
        return False
    outlook = await get_afternoon_outlook(now.date(), user_id=user_id)
    if not outlook or not outlook["is_wet"] or not db.private_context_is_current(user_id):
        return False
    worn = set((get_cached_outfit_today(user_id) or {}).get("item_ids") or [])
    has_outerwear = any(it["id"] in worn and it.get("type") == "outerwear"
                        for it in get_wardrobe_entry(user_id).get("items", []))
    advice = "Your jacket should cover it, an umbrella would not hurt." if has_outerwear else "Take a jacket or an umbrella."
    await bot.send_message(chat_id=user_id, text=f"🌧 Rain is now expected this afternoon (the morning forecast was dry). {advice}")
    mark_outfit_weather_alerted(user_id)
    return True


# ── Image provider statistics and alerts ──────────────────────────────────────
IMAGE_PROVIDER_LABELS = {"gemini": "Nano Banana", "gwdg": "GWDG", "fashn": "FASHN", "ootd": "OOTDiffusion", "flux": "FLUX"}
IMAGE_COST_USD = {"gemini": 0.08, "fashn": 0.075}  # paid providers, per image (fal.ai)
IMAGE_ALERT_COOLDOWN_SECONDS = 6 * 3600
_last_image_alert: float = 0.0


async def generate_outfit_tryon_image(bot, reference_bytes: bytes, chosen_items: list[dict],
                                      user_id: Optional[int] = None) -> Optional[bytes]:
    """Dress the reference photo in the chosen outfit (see _tryon_chain for the
    provider order) and count which provider delivered. When every provider
    failed the administrators are told once in a while, since the outfit then
    goes out as text only without anyone noticing."""
    used: list = []
    result = await _tryon_chain(bot, reference_bytes, chosen_items, user_id, used)
    if user_id is not None and personal_ai_allowed(user_id):  # a stopped request is not a provider failure
        if used:
            record_outfit_stat(used[0])
        elif result is None:
            record_outfit_stat("none")
            await _alert_admins_image_failed(bot)
    return result


async def _alert_admins_image_failed(bot) -> None:
    global _last_image_alert
    now = time.monotonic()
    if _last_image_alert and now - _last_image_alert < IMAGE_ALERT_COOLDOWN_SECONDS:
        return
    _last_image_alert = now
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="⚠️ Outfit image: every provider failed just now, so the outfit went out as text only. "
                     "Check the FAL_KEY credit and the GWDG key.",
            )
        except TelegramError as ignored_error:
            logging.info("Ignored a failed best-effort step: %s: %s", type(ignored_error).__name__, ignored_error)


def outfit_digest_line(today=None) -> Optional[str]:
    """One line for the daily health digest: which image providers delivered
    (today and yesterday), failures and fallbacks, and the paid spend of the
    last 30 days. None while nothing has been counted yet."""
    stats = get_outfit_stats()
    if not stats:
        return None
    today = today or datetime.now(TZ_BERLIN).date()
    recent: Counter = Counter()
    for offset in (0, 1):
        recent.update(stats.get((today - timedelta(days=offset)).isoformat(), {}))
    month: Counter = Counter()
    for offset in range(30):
        month.update(stats.get((today - timedelta(days=offset)).isoformat(), {}))
    parts = [f"{n} {IMAGE_PROVIDER_LABELS[name]}" for name, n in recent.items() if name in IMAGE_PROVIDER_LABELS and n]
    if recent.get("none"):
        parts.append(f"{recent['none']} failed")
    if recent.get("pick_fallback"):
        parts.append(f"{recent['pick_fallback']} pick fallback(s)")
    cost = sum(month.get(name, 0) * price for name, price in IMAGE_COST_USD.items())
    return f"👔 Outfit images (today and yesterday): {', '.join(parts) or 'none'}. Paid images, last 30 days: about ${cost:.2f}"
