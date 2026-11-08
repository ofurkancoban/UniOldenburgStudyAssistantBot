"""Mensa menu, dish photos, food preferences and the lunch recommendation.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import html
import logging
import random
import re
from datetime import datetime
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from studip_utils import TZ_BERLIN, berlin_now, tag_attr

from unibot import db
from unibot import state
from unibot.storage import shared_http
from unibot.telegram_ui import (
    _show_typing,
    _start_loading_animation,
    _stop_loading_animation,
    get_main_keyboard,
)
from unibot.auth import is_user_allowed, login_studip
from unibot.studip_api import BASE_URL
from unibot.ai import _call_openrouter, _extract_json_object


FOOD_PREFERENCES_CACHE_PATH = "food_preferences.json"


def load_food_preferences() -> dict:
    """Return {"avoid_codes": [...], "avoid_keywords": [...]} describing
    ingredients/allergen codes the user doesn't eat, or empty lists if none
    have been set — in which case the Mensa menu shows everything as before."""
    if db.exists(FOOD_PREFERENCES_CACHE_PATH):
        try:
            data = db.load(FOOD_PREFERENCES_CACHE_PATH)
            return {
                "avoid_codes": data.get("avoid_codes", []),
                "avoid_keywords": data.get("avoid_keywords", []),
            }
        except Exception as e:
            logging.warning("Could not load food preferences: %s", e)
    return {"avoid_codes": [], "avoid_keywords": []}


def save_food_preferences(avoid_codes: list, avoid_keywords: list) -> None:
    try:
        db.save(FOOD_PREFERENCES_CACHE_PATH, {"avoid_codes": avoid_codes, "avoid_keywords": avoid_keywords})
    except Exception as e:
        logging.error("Could not save food preferences: %s", e)


# ── menu functions ─────────────────────────────────────────────────
# Full code -> English name table for the Mensa's allergen/ingredient codes.
# Shared by the menu's allergen guide (get_todays_menu_enhanced) and the
# food-preference voice extraction prompt (extract_food_preferences), so
# spoken ingredients and displayed dishes are checked against the same codes.
ALLERGEN_CODE_NAMES = {
    # General Information
    "A": "Alcohol",
    "KL": "Calf Rennet",
    "Kn": "Garlic",
    "RG": "Beef Gelatin",
    "SG": "Pork Gelatin",

    # Additives
    "1": "Colorant",
    "2": "Preservative",
    "3": "Antioxidant",
    "4": "Flavor Enhancer",
    "5": "Sulfured",
    "6": "Blackened",
    "7": "Waxed",
    "8": "Sweeteners",
    "9": "Phenylalanine Source",
    "10": "Phosphate",
    "11": "Caffeine",
    "12": "Cocoa Coating",

    # Allergens
    "Ei": "Eggs",
    "En": "Peanuts",
    "Fi": "Fish",
    "Kr": "Crustaceans",
    "Lu": "Lupin",
    "Mi": "Milk/Lactose",
    "Se": "Sesame",
    "Sf": "Mustard",
    "Sl": "Celery",
    "So": "Soy",
    "Sw": "Sulfites",
    "Wt": "Molluscs",

    # Gluten-containing grains
    "Di": "Spelt (Wheat)",
    "Ge": "Barley",
    "Ha": "Oats",
    "Hy": "Hybrid Strains",
    "Ka": "Kamut",
    "Ro": "Rye",
    "We": "Wheat",

    # Nuts
    "Cn": "Cashews",
    "Hn": "Hazelnuts",
    "Ma": "Almonds",
    "Mn": "Macadamia",
    "Pa": "Brazil Nuts",
    "Pi": "Pistachios",
    "Pn": "Pecans",
    "Wa": "Walnuts",

    # Meat types
    "S": "Pork",
    "Sch": "Pork",
    "Su": "Pork",
    "G": "Poultry",
    "R": "Beef",
    "L": "Lamb",
    "W": "Game",
    "F": "Fish",
    "V": "Vegetarian",
    "V+": "Vegan",
}


def translate_food_codes(text):
    """Translate food codes to emojis with proper handling"""
    if not text:
        return ""

    # Code mappings - only versions without parentheses
    code_map = {
        "V+": "🌿",  # Vegan
        "V": "🥗",  # Vegetarian
        # Additives
        "1": "🎨",  # Colorant
        "2": "🧪",  # Preservative
        "3": "🛡️",  # Antioxidant
        "4": "👅",  # Flavor enhancer
        "5": "⚗️",  # Sulfured
        "6": "⚫",  # Blackened
        "7": "🕯️",  # Waxed
        "8": "🍯",  # Sweeteners
        "9": "⚠️",  # Phenylalanine
        "10": "🔬",  # Phosphate
        "11": "☕",  # Caffeine
        "12": "🍫",  # Cocoa coating

        # Allergens
        "Ei": "🥚",  # Eggs
        "En": "🥜",  # Peanuts
        "Fi": "🐟",  # Fish
        "Kr": "🦐",  # Crustaceans
        "Lu": "🫘",  # Lupin
        "Mi": "🥛",  # Milk/Lactose
        "Se": "🌱",  # Sesame
        "Sf": "🌭",  # Mustard
        "Sl": "🥬",  # Celery
        "So": "🌱",  # Soy
        "Sw": "🍷",  # Sulfites
        "Wt": "🐌",  # Molluscs

        # Gluten-containing grains
        "Di": "🌾",  # Spelt (wheat type)
        "Ge": "🌾",  # Barley
        "Ha": "🌾",  # Oats
        "Hy": "🌾",  # Hybrid strains
        "Ka": "🌾",  # Kamut
        "Ro": "🌾",  # Rye
        "We": "🌾",  # Wheat

        # Nuts
        "Cn": "🥜",  # Cashews
        "Hn": "🥜",  # Hazelnuts
        "Ma": "🥜",  # Almonds
        "Mn": "🥜",  # Macadamia
        "Pa": "🥜",  # Brazil nuts
        "Pi": "🥜",  # Pistachios
        "Pn": "🥜",  # Pecans
        "Wa": "🥜",  # Walnuts

        # Meat types
        "S": "🐷",  # Pork
        "Sch": "🐷",  # Pork
        "Su": "🐷",  # Pork
        "G": "🐔",  # Poultry
        "R": "🐄",  # Beef
        "L": "🐑",  # Lamb
        "W": "🦌",  # Game
        "F": "🐠",  # Fish

        # Other
        "A": "🍷",  # Alcohol
        "KL": "🐄",  # Calf rennet
        "Kn": "🧄",  # Garlic
        "RG": "🐄",  # Beef gelatin
        "SG": "🐷",  # Pork gelatin
    }

    # The Mensa prints codes as a comma separated list ("Sw,We,1"). Map each
    # code exactly; an unknown code is kept as is instead of being partly
    # replaced (substring replacement turned e.g. "Gl" into "🐔l").
    return ",".join(code_map.get(code.strip(), code.strip()) for code in text.split(","))


async def fetch_todays_menu_items(session, sub_path="2/") -> list:
    """Fetch today's Mensa menu and return structured dish items — a
    lighter-weight sibling to get_todays_menu_enhanced() (which builds a
    formatted HTML string for display) for callers that need the raw data
    instead, like build_food_recommendation(). Deliberately a separate,
    small parser rather than refactoring get_todays_menu_enhanced to share
    it, so this stays low-risk to the actively-used menu display.

    Each item: {"category", "name", "description", "price", "codes" (a set
    of allergen/ingredient codes)}. Returns [] on any failure."""
    try:
        if "mensawidget/menu/" in sub_path:
            sub_path = sub_path.split("mensawidget/menu/")[-1]
        url = f"{BASE_URL}/plugins.php/mensawidget/menu/{sub_path}"
        async with await session.get(url) as r:
            html_content = await r.text()
    except Exception as e:
        logging.warning("fetch_todays_menu_items: fetch failed: %s", e)
        return []

    try:
        soup = BeautifulSoup(html_content, "html.parser")
        items = []
        for category_table in soup.find_all('table', class_='default'):
            th = category_table.find('th')
            category_name = th.get_text(strip=True) if th else ""

            for row in category_table.find_all('tr')[1:]:
                cols = row.find_all('td')
                if len(cols) < 2:
                    continue
                name_cell, price_cell = cols[0], cols[1]

                temp_cell = BeautifulSoup(str(name_cell), 'html.parser')
                codes: set[str] = set()
                for span in temp_cell.find_all('span', class_='attributes'):
                    span_text = span.get_text(strip=True)
                    if span_text:
                        codes.update(c.strip() for c in span_text.split(','))
                    span.decompose()
                for abbr in temp_cell.find_all('abbr'):
                    abbr.decompose()

                lines = [l.strip() for l in temp_cell.get_text("\n", strip=True).split('\n') if l.strip()]
                if not lines:
                    continue
                name_text = re.sub(r'\([^)]*\)$', '', re.sub(r'[1-9][0-9]*[A-Za-z,+\s]*$', '', lines[0]).strip()).strip()
                if not name_text:
                    continue
                description = ' '.join(lines[1:]) if len(lines) > 1 else ""

                if not category_name.strip() and "pizza" in name_text.lower():
                    category_name = "PIZZA"

                items.append({
                    "category": category_name or "Other",
                    "name": name_text,
                    "description": description,
                    "price": price_cell.get_text(strip=True).replace("&euro;", "€").strip(),
                    "codes": codes,
                })
        return items
    except Exception as e:
        logging.warning("fetch_todays_menu_items: parse failed: %s", e)
        return []


MEAT_CODES = {"G", "R", "L", "W", "F", "Fi"}  # Poultry (incl. turkey — no separate code for it), Beef, Lamb, Game, Fish (F and Fi both mean Fish in this Mensa's code table) — used to flag/prioritize meat/fish dishes
MEAL_RECOMMENDATION_BASE_AVOID_CODES = {"S", "Sch", "Su", "A", "RG", "SG"}  # Pork (all 3 codes), Alcohol, Beef/Pork Gelatin
MEAL_RECOMMENDATION_BASE_AVOID_KEYWORDS = {"wine"}

MEAL_RECOMMENDATION_SYSTEM_PROMPT = """You are picking exactly ONE recommended dish for a student's lunch from today's Mensa Counter/Culinarium options, from the given candidate list only — everything on it is already filtered to exclude what they don't eat, so don't second-guess that. Weigh three things together: (1) prioritize a meat, chicken, turkey, or fish dish when one is available — the [tag] next to a candidate flags this; (2) how nutritious/filling it likely is, judged from its name and description (lean protein, vegetables, a balanced plate > something clearly just a light side or dessert-like); (3) price — a student budget, so don't default to the most expensive option without reason. None of these mechanically overrides the others; weigh the actual tradeoff for today's specific candidates.

Respond with strict JSON only, no other text: {"dish": "<the exact dish name, copied verbatim from the candidate list>", "reasoning": "<1-2 warm, casual sentences naming the dish naturally and explaining the pick (you can mention price/value or how filling it is if that's genuinely part of why) — this will be read aloud as a voice message, so plain spoken-friendly text only: no markdown, no lists, no emojis>"}"""

MENSA_PHOTO_AJAX_URL = "https://sw-oldenburg-spl56.maxmanager.xyz/inc/ajax-php_konnektor.inc.php"
MENSA_PHOTO_BASE_URL = "https://sw-oldenburg-spl56.maxmanager.xyz/"


async def fetch_dish_photo_candidates(target_date=None, loc_id: str = "2") -> list:
    """Fetch every dish photo available for `target_date` (defaults to
    today) on the Studierendenwerk Oldenburg's public Speiseplan widget
    (studierendenwerk-oldenburg.de/.../speiseplan-uhlhornsweg) — a
    different source than the Stud.IP mensa widget the rest of this bot's
    menu features scrape, but the same underlying canteen data, so dish
    names match closely enough for fuzzy matching. loc_id="2" is
    Uhlhornsweg, matching fetch_todays_menu_items' default sub_path="2/".
    One fetch covers the whole day's menu, so callers matching several
    dish names against the same day should call this once and reuse the
    result rather than looking each dish up individually.
    Returns a list of (title, absolute_image_url) tuples, or [] on any
    failure (unreachable, no photos for that day, parse error)."""
    date_str = (target_date or berlin_now().date()).strftime("%Y-%m-%d")
    try:
        async with shared_http() as session:
            async with session.post(
                MENSA_PHOTO_AJAX_URL,
                data={
                    "func": "make_spl",
                    "locId": loc_id,
                    "date": date_str,
                    "lang": "en",
                    "startThisWeek": date_str,
                    "startNextWeek": date_str,
                },
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                html = await resp.text()
    except Exception as e:
        logging.warning("fetch_dish_photo_candidates: request failed: %s", e)
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for meal in soup.select(".splMeal"):
            title_span = meal.select_one("span[style*='font-size:24px']")
            img = meal.select_one("img.smallFoto")
            if not title_span or not img or not img.get("src"):
                continue
            if "dummies/platzhalter" in img["src"]:
                continue
            title = (title_span.contents[0] if title_span.contents else "")
            title = str(title).strip()
            if not title:
                continue
            candidates.append((title, MENSA_PHOTO_BASE_URL + tag_attr(img, "src").lstrip("/")))
        return candidates
    except Exception as e:
        logging.warning("fetch_dish_photo_candidates: parse failed: %s", e)
        return []


async def fetch_dish_photo_url(dish_name: str, loc_id: str = "2", target_date=None) -> Optional[str]:
    """Look up a photo for a single `dish_name` (see
    fetch_dish_photo_candidates) — convenience wrapper for callers that
    only need one dish; callers matching several dishes for the same day
    should call fetch_dish_photo_candidates once and match against it
    directly instead, to avoid a redundant fetch per dish."""
    from unibot.voice import _best_title_match
    candidates = await fetch_dish_photo_candidates(target_date=target_date, loc_id=loc_id)
    return _best_title_match(dish_name, candidates, min_score=0.55)


def _format_menu_item_html(item: dict) -> str:
    """Build the HTML caption/message text for one dish_items entry (see
    get_todays_menu_enhanced) — name, which Counter/Culinarium it's at,
    description, allergens, and price, in the compact layout used for
    both a photo caption (1024-char Telegram limit) and the no-photo
    text fallback."""
    star = " ⭐" if item["is_limited"] else ""
    lines = [f"🍽 <b>{html.escape(item['name'])}</b>{star}", f"📍 {html.escape(item['category'])}"]
    if item["description"]:
        lines.append(html.escape(item["description"]))
    if item["allergens_text"]:
        lines.append(f"{translate_food_codes(item['allergens_text'])} <i>({html.escape(item['allergens_text'])})</i>")
    if item["price"]:
        lines.append(f"💶 {item['price']}")
    return "\n".join(lines)


async def _send_menu_items_individually(sender, dish_items: list, target_date=None, skip_unmatched: bool = False):
    """Send every dish in `dish_items` (see get_todays_menu_enhanced) as
    its own message — name, Counter/Culinarium location, description,
    allergens, and price, with a photo attached when one can be matched
    on the Studierendenwerk's public Speiseplan site.

    skip_unmatched=False (default): a dish with no matched photo still
    gets sent as a plain text message with the same info — use this when
    these messages ARE the menu listing (nothing else already showed the
    text). skip_unmatched=True: dishes with no matched photo are skipped
    entirely instead — use this when the full text menu was already sent
    separately, so this call is purely "here are photos for what could be
    matched", not a second listing.

    Best-effort throughout: a photo-fetch failure just means every dish
    is treated as unmatched (falls back to text, or is skipped, per
    skip_unmatched), never a hard error."""
    from unibot.voice import _best_title_match
    if not dish_items:
        return
    try:
        candidates = await fetch_dish_photo_candidates(target_date=target_date)
    except Exception as e:
        logging.warning("_send_menu_items_individually: photo fetch failed: %s", e)
        candidates = []

    for item in dish_items:
        photo_url = None
        if candidates and len(item["name"].strip()) >= 4:
            photo_url = _best_title_match(item["name"], candidates, min_score=0.55)
        if not photo_url and skip_unmatched:
            continue
        caption = _format_menu_item_html(item)
        try:
            if photo_url:
                await sender.reply_photo(photo=photo_url, caption=caption, parse_mode="HTML")
            else:
                await sender.reply_text(caption, parse_mode="HTML")
        except Exception as e:
            logging.warning("_send_menu_items_individually: send failed for %r: %s", item['name'], e)


def _friendly_menu_category_label(category: str) -> str:
    """Human-friendly label for a raw Mensa category name, e.g. "COUNTER
    TWO" -> "Counter 2"; Culinarium categories are already reasonably
    formatted and pass through as-is."""
    counter_labels = {
        "COUNTER ONE": "Counter 1", "COUNTER TWO": "Counter 2",
        "COUNTER THREE": "Counter 3", "COUNTER FOUR": "Counter 4",
    }
    return counter_labels.get(category, category)


async def build_food_recommendation() -> Optional[dict]:
    """Fetch today's Mensa menu, filter out anything the student avoids
    (their food_preferences.json settings, unioned with a fixed
    pork/alcohol/wine/gelatine floor per their stated restriction), keep
    only Counter/Culinarium items, and ask the LLM to pick ONE dish,
    weighing meat/chicken/turkey/fish priority, likely nutritional value,
    and price together (see MEAL_RECOMMENDATION_SYSTEM_PROMPT) — not a
    random pick, a real judged one.

    Returns {"dish", "category" (raw, e.g. "COUNTER TWO" — pass through
    _friendly_menu_category_label for display), "reasoning"}, or None on
    any failure (no menu data reachable). If the menu loaded but nothing
    survived filtering, "category" is None and "reasoning" carries an
    explanatory message instead of a real pick — callers should still
    just speak/show "reasoning" either way.
    """
    from unibot.voice import _best_title_match
    try:
        session = await login_studip()
        items = await fetch_todays_menu_items(session)
    except Exception as e:
        logging.warning("build_food_recommendation: menu fetch failed: %s", e)
        return None
    if not items:
        return None

    prefs = load_food_preferences()
    avoid_codes = set(prefs["avoid_codes"]) | MEAL_RECOMMENDATION_BASE_AVOID_CODES
    avoid_keywords = {k.lower() for k in prefs["avoid_keywords"]} | MEAL_RECOMMENDATION_BASE_AVOID_KEYWORDS

    candidates = []
    for it in items:
        category_lower = it["category"].lower()
        if "counter" not in category_lower and "culinarium" not in category_lower:
            continue
        if it["codes"] & avoid_codes:
            continue
        haystack = f"{it['name']} {it['description']}".lower()
        if any(kw in haystack for kw in avoid_keywords):
            continue
        candidates.append(it)

    if not candidates:
        return {
            "dish": None,
            "category": None,
            "reasoning": "Couldn't find anything on today's Counter or Culinarium menu that fits your preferences — check the full menu instead, there might be something elsewhere on it.",
        }

    lines = []
    lookup = []
    for it in candidates:
        meat_tags = sorted({ALLERGEN_CODE_NAMES.get(c, c) for c in it["codes"] if c in MEAT_CODES})
        tag_note = f" [{', '.join(meat_tags)}]" if meat_tags else ""
        price_part = f" — {it['price']}" if it["price"] else ""
        desc_part = f": {it['description']}" if it["description"] else ""
        lines.append(f"- {it['name']} ({it['category']}){tag_note}{price_part}{desc_part}")
        lookup.append((it["name"], it))

    raw = await _call_openrouter(
        MEAL_RECOMMENDATION_SYSTEM_PROMPT,
        "Today's candidates:\n" + "\n".join(lines),
        temperature=0.9,
    )
    parsed = _extract_json_object(raw)
    chosen = None
    if parsed and parsed.get("dish"):
        chosen = _best_title_match(parsed["dish"], lookup, min_score=0.5)
    if chosen is None:
        meat_candidates = [c for c in candidates if c["codes"] & MEAT_CODES]
        chosen = random.choice(meat_candidates or candidates)

    reasoning = ((parsed or {}).get("reasoning") or "").strip() if chosen is not None else ""
    reasoning = reasoning or f"I'd recommend the {chosen['name']} today."

    return {"dish": chosen["name"], "category": chosen["category"], "reasoning": reasoning}


async def _deliver_food_recommendation(sender, result: dict):
    """Send a build_food_recommendation() result the way it's meant to be
    read: the Counter/Culinarium location + dish name as its own short text
    message (quick to glance at, no need to replay the audio for it), a
    photo of the dish when one can be matched on the Studierendenwerk's
    public Speiseplan site, and the actual pick + reasoning spoken as a
    full voice message — falling back to text if TTS/ffmpeg is
    unavailable, so it's never silently dropped."""
    from unibot.morning import _try_send_voice
    if result["category"] and result["dish"]:
        await sender.reply_text(f"📍 {_friendly_menu_category_label(result['category'])} — {result['dish']}")

        photo_url = await fetch_dish_photo_url(result["dish"])
        if photo_url:
            try:
                await sender.reply_photo(photo=photo_url)
            except Exception as e:
                logging.warning("_deliver_food_recommendation: photo send failed: %s", e)

    spoken_text = result["reasoning"]
    await _show_typing(sender, action=ChatAction.RECORD_VOICE)
    if not await _try_send_voice(sender, spoken_text):
        await sender.reply_text(f"🍽️ {spoken_text}")


# Display order of the Mensa's counters/categories (unknown ones go last).
_MENU_CATEGORY_PRIORITY = {
    "COUNTER ONE": 1, "COUNTER TWO": 2, "COUNTER THREE": 3, "COUNTER FOUR": 4,
    "Main Dishes": 5, "PIZZA": 10,
    "Culinarium Main Dishes": 20, "Culinarium Side Dishes": 21,
    "Culinarium Salads": 22, "Culinarium Desserts": 23,
    "Soup": 30, "Side Dishes": 40, "Salads": 50, "Desserts": 60,
}
_MENU_CATEGORY_DISPLAY = {
    "Main Dishes": "🍴 MAIN DISHES",
    "Soup": "🍲 SOUPS",
    "Side Dishes": "🥗 SIDE DISHES",
    "Salads": "🥗 SALADS",
    "Desserts": "🍮 DESSERTS",
    "COUNTER ONE": "🍴 COUNTER 1",
    "COUNTER TWO": "🍴 COUNTER 2",
    "COUNTER THREE": "🍴 COUNTER 3",
    "COUNTER FOUR": "🍴 COUNTER 4",
    "Culinarium Main Dishes": "👨‍🍳 CULINARIUM MAIN",
    "Culinarium Side Dishes": "👨‍🍳 CULINARIUM SIDE",
    "Culinarium Salads": "👨‍🍳 CULINARIUM SALAD",
    "Culinarium Desserts": "👨‍🍳 CULINARIUM DESSERT",
    "PIZZA": "🍕 PIZZA",
}
_MENU_SEPARATOR = "━━━━━━━━━━━━━━━━━━\n"
_TRAILING_CODES_RE = re.compile(r"[1-9][0-9]*[A-Za-z,+\s]*$")
_TRAILING_PARENS_RE = re.compile(r"\([^)]*\)$")


def _menu_effective_ts(sub_path: str) -> int:
    """Noon timestamp of the day a menu sub path refers to (today by default)."""
    effective_ts = int(datetime.now(TZ_BERLIN).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
    ts_match = re.search(r"/(\d{10})(?:/|$)", sub_path)
    if ts_match:
        base_ts = int(ts_match.group(1))
        if "/next" in sub_path:
            effective_ts = base_ts + 86400
        elif "/previous" in sub_path:
            effective_ts = base_ts - 86400
        else:
            effective_ts = base_ts
    return effective_ts


def _menu_date_text(soup, effective_ts: int) -> str:
    """The page's own date heading when it has one (rare in fragments)."""
    date_element = soup.find("h2")
    if date_element:
        date_page_text = date_element.get_text(strip=True)
        if re.search(r"\d{2}\.\d{2}\.\d{4}", date_page_text):
            return date_page_text
    return datetime.fromtimestamp(effective_ts).strftime("%A, %d.%m.%Y")


def _menu_category_name(category_table) -> str:
    """Category heading; an unnamed table of pizzas becomes "PIZZA"."""
    category_name = category_table.find("th").get_text(strip=True)
    if not category_name.strip():
        for row in category_table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) >= 2 and "pizza" in cols[0].get_text(strip=True).lower():
                return "PIZZA"
    return category_name


def _clean_menu_price(price: str) -> str:
    clean_price = price.replace("&euro;", "€").strip().replace(".", ",")
    if not clean_price or clean_price == "€":
        return ""
    if "€" not in clean_price:
        clean_price += "€"
    return clean_price


def _strip_trailing_codes(text: str) -> str:
    text = _TRAILING_CODES_RE.sub("", text).strip()
    return _TRAILING_PARENS_RE.sub("", text).strip()


def _parse_menu_row(row) -> tuple[set, Optional[dict]]:
    """(allergen codes seen in the row, dish) for one menu table row.

    dish is None when the row has no name. Its keys: name, description,
    allergens_text, price (raw). The codes are returned even then, since the
    allergen guide lists everything printed on the page.
    """
    cols = row.find_all("td")
    if len(cols) < 2:
        return set(), None
    temp_cell = BeautifulSoup(str(cols[0]), "html.parser")
    codes: set[str] = set()
    allergens_text = ""
    for span in temp_cell.find_all("span", class_="attributes"):
        span_text = span.get_text(strip=True)
        if span_text:
            allergens_text = span_text
            codes.update(a.strip() for a in span_text.split(","))
        span.decompose()
    for abbr in temp_cell.find_all("abbr"):
        abbr.decompose()
    lines = [line.strip() for line in temp_cell.get_text("\n", strip=True).split("\n") if line.strip()]
    if not lines:
        return codes, None
    description = " ".join(lines[1:]) if len(lines) > 1 else ""
    return codes, {
        "name": _strip_trailing_codes(lines[0]),
        "description": _strip_trailing_codes(description) if description else "",
        "allergens_text": allergens_text,
        "price": cols[1].get_text(strip=True),
    }


def _is_dish_hidden(dish: dict, avoid_codes: set, avoid_keywords: list) -> bool:
    item_codes = {a.strip() for a in dish["allergens_text"].split(",")} if dish["allergens_text"] else set()
    haystack = f"{dish['name']} {dish['description']}".lower()
    return bool(item_codes & avoid_codes) or any(kw in haystack for kw in avoid_keywords)


def _format_menu_dish(name: str, description: str, allergens_text: str, price: str, is_limited: bool) -> str:
    star = " ⭐" if is_limited and "⭐" not in name else ""
    text = f"• <b>{html.escape(name)}</b>{star}\n"
    if description:
        text += f"  {html.escape(description)}\n"
    if allergens_text:
        text += f"  {translate_food_codes(allergens_text)}\n"
        text += f"  <i>({html.escape(allergens_text)})</i>\n"
    text += f"  💶 {price}\n\n" if price else "\n"
    return text


def _format_allergen_guide(codes_used: set, hidden_count: int) -> str:
    """Allergen guide for the codes actually printed on today's menu."""
    text = _MENU_SEPARATOR + "📋 <b>ALLERGEN GUIDE</b>\n" + _MENU_SEPARATOR
    guide_lines = [
        f"{translate_food_codes(code)} <b>{code}</b> - {ALLERGEN_CODE_NAMES[code]}"
        for code in sorted(codes_used) if code in ALLERGEN_CODE_NAMES
    ]
    text += "\n".join(guide_lines) if guide_lines else "No allergens listed in today's menu"
    text += "\n\n⭐ <b>Limited availability</b>"
    if hidden_count:
        text += f"\n🚫 {hidden_count} dish(es) hidden based on your food preferences"
    return text


async def get_todays_menu_enhanced(session, sub_path="2/", avoid_codes=None, avoid_keywords=None):
    """Enhanced menu fetching with dynamic allergen guide and navigation links.

    avoid_codes/avoid_keywords (from food_preferences.json, see
    load_food_preferences) hide dishes matching the user's stated food
    preferences: avoid_codes against each dish's parsed allergen/ingredient
    codes (exact), avoid_keywords as a case-insensitive substring match
    against the dish's name/description text.

    Returns (menu_text, prev_link, next_link, effective_date, dish_items,
    header_text). effective_date lets a caller fetch dish photos for the
    same day being displayed (see fetch_dish_photo_candidates); dish_items
    is the menu structured per-dish (name/category/description/
    allergens_text/price/is_limited, category-priority order) for callers
    that send each dish as its own message (see
    _send_menu_items_individually). header_text (just the date/title
    line, no per-category listing) is meant to pair with that path, so
    the day isn't described twice. On failure, dish_items is [] and
    effective_date/header_text are None."""
    avoid_codes = set(avoid_codes or [])
    avoid_keywords = [kw.lower() for kw in (avoid_keywords or [])]
    try:
        # If sub_path is a full URL, extract the part after menu/
        if "mensawidget/menu/" in sub_path:
            sub_path = sub_path.split("mensawidget/menu/")[-1]
        url = f"{BASE_URL}/plugins.php/mensawidget/menu/{sub_path}"
        async with await session.get(url) as r:
            html_content = await r.text()
        soup = BeautifulSoup(html_content, "html.parser")

        effective_ts = _menu_effective_ts(sub_path)
        # Navigation links step relative to the effective date, so paging never ends
        prev_link = f"2/{effective_ts}/previous"
        next_link = f"2/{effective_ts}/next"
        header_text = f"🍽️ <b>{html.escape(_menu_date_text(soup, effective_ts))}</b> 🍽️\n🏛️ Mensa Uni Oldenburg"

        categories = soup.find_all("table", class_="default")
        if not categories:
            return "🍽️ <b>Mensa Uni Oldenburg</b>\n\n❌ No dishes found for this date. The Mensa might be closed.", prev_link, next_link, None, [], None

        dish_items = []
        categories_data = []
        codes_used = set()
        hidden_count = 0
        for category_table in categories:
            category_name = _menu_category_name(category_table)
            display_name = _MENU_CATEGORY_DISPLAY.get(category_name, f"🍴 {category_name}")
            priority = _MENU_CATEGORY_PRIORITY.get(category_name, 100)
            cat_chunk = _MENU_SEPARATOR + f"{display_name}\n" + _MENU_SEPARATOR
            items_found = False
            for row in category_table.find_all("tr")[1:]:
                codes, dish = _parse_menu_row(row)
                codes_used |= codes
                if dish is None:
                    continue
                if _is_dish_hidden(dish, avoid_codes, avoid_keywords):
                    hidden_count += 1
                    continue
                if not dish["name"]:
                    continue
                items_found = True
                price = _clean_menu_price(dish["price"])
                is_limited = "⭐" in dish["name"] or "limited" in dish["name"].lower()
                dish_items.append({
                    "priority": priority,
                    "name": dish["name"],
                    "category": display_name,
                    "description": dish["description"],
                    "allergens_text": dish["allergens_text"],
                    "price": price,
                    "is_limited": is_limited,
                })
                cat_chunk += _format_menu_dish(dish["name"], dish["description"], dish["allergens_text"], price, is_limited)
            if items_found:
                categories_data.append((priority, cat_chunk))

        categories_data.sort(key=lambda x: x[0])
        menu_text = header_text + "\n\n" + "".join(chunk for _, chunk in categories_data)
        menu_text += _format_allergen_guide(codes_used, hidden_count)

        dish_items.sort(key=lambda d: d["priority"])
        return menu_text, prev_link, next_link, datetime.fromtimestamp(effective_ts).date(), dish_items, header_text

    except Exception as e:
        logging.error("Enhanced menu fetch error: %s", e)
        return "❌ Menu could not be loaded. Please try again later.", None, None, None, [], None


# ── menu commands ────────────────────────────────────────────────────────────

def get_menu_navigation_keyboard(prev_path=None, next_path=None, current_path="2/"):
    """Create navigation keyboard for Mensa menu. `current_path` is the
    sub_path of the day currently being shown (matching menu_nav's own
    path format) — needed so the "📸 Show Dish Photos" button re-fetches
    photos for the right day rather than always today's."""
    row = []
    if prev_path:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"menu_nav|{prev_path}"))

    row.append(InlineKeyboardButton("Today", callback_data="menu_nav|2/"))

    if next_path:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"menu_nav|{next_path}"))

    photos_row = [InlineKeyboardButton("📸 Show Dish Photos", callback_data=f"menu_photos|{current_path}")]
    prefs_row = [InlineKeyboardButton("⚙️ Food Preferences", callback_data="menu_prefs")]
    recommend_row = [InlineKeyboardButton("🍽️ What should I eat?", callback_data="menu_recommend")]

    return InlineKeyboardMarkup([row, photos_row, prefs_row, recommend_row])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily food menu in the requested format"""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except TelegramError:
            pass

    message = update.effective_message
    if not message:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None or not is_user_allowed(user_id):
        await message.reply_text("Not authorized to use this bot.")
        return

    try:
        # Session check
        await login_studip()

        # Fetch menu, filtered per any saved food preferences
        prefs = load_food_preferences()
        menu_text, prev, next_, menu_date, dish_items, header_text = await get_todays_menu_enhanced(
            state.global_session, avoid_codes=prefs["avoid_codes"], avoid_keywords=prefs["avoid_keywords"]
        )

        # Dish photos are opt-in via the "📸 Show Dish Photos" button below,
        # not sent automatically — see handle_menu_photos_button.
        await message.reply_text(
            menu_text,
            parse_mode="HTML",
            reply_markup=get_menu_navigation_keyboard(prev, next_)
        )

    except Exception as e:
        error_msg = f"❌ Error loading menu:\n{str(e)}"
        await message.reply_text(
            error_msg,
            reply_markup=get_main_keyboard()
        )
        logging.error("Menu command error: %s", e)


FOOD_PREF_CATEGORIES = {
    "meat": {
        "title": "🥩 Meat & Animal Products",
        "keys": ["pork", "beef", "poultry", "lamb", "game", "fish", "pork_gelatin", "beef_gelatin", "calf_rennet"],
    },
    "allergens": {
        "title": "🥜 Common Allergens",
        "keys": ["milk", "eggs", "peanuts", "crustaceans", "molluscs", "soy", "mustard", "sesame", "celery", "lupin", "sulfites"],
    },
    "gluten": {
        "title": "🌾 Gluten & Grains",
        "keys": ["wheat", "rye", "barley", "oats", "spelt", "kamut", "hybrid"],
    },
    "nuts": {
        "title": "🌰 Tree Nuts",
        "keys": ["hazelnuts", "walnuts", "almonds", "cashews", "pistachios", "brazil_nuts", "pecans", "macadamia"],
    },
    "additives": {
        "title": "🧪 Additives & Other",
        "keys": ["alcohol", "garlic", "colorant", "preservative", "antioxidant", "enhancer", "sulfured", "sweeteners", "caffeine", "phosphate"],
    },
}

FOOD_PREF_LEGEND = {
    # Meat & Animal Products
    "pork": {"name": "🐷 Pork", "codes": {"S", "Sch", "Su"}, "category": "meat"},
    "beef": {"name": "🐄 Beef", "codes": {"R"}, "category": "meat"},
    "poultry": {"name": "🐔 Poultry", "codes": {"G"}, "category": "meat"},
    "lamb": {"name": "🐑 Lamb", "codes": {"L"}, "category": "meat"},
    "game": {"name": "🦌 Game / Venison", "codes": {"W"}, "category": "meat"},
    "fish": {"name": "🐟 Fish", "codes": {"F", "Fi"}, "category": "meat"},
    "pork_gelatin": {"name": "🍮 Pork Gelatin", "codes": {"SG"}, "category": "meat"},
    "beef_gelatin": {"name": "🍮 Beef Gelatin", "codes": {"RG"}, "category": "meat"},
    "calf_rennet": {"name": "🧀 Calf Rennet", "codes": {"KL"}, "category": "meat"},

    # Allergens
    "milk": {"name": "🥛 Milk / Lactose", "codes": {"Mi"}, "category": "allergens"},
    "eggs": {"name": "🥚 Eggs", "codes": {"Ei"}, "category": "allergens"},
    "peanuts": {"name": "🥜 Peanuts", "codes": {"En"}, "category": "allergens"},
    "crustaceans": {"name": "🦐 Crustaceans", "codes": {"Kr"}, "category": "allergens"},
    "molluscs": {"name": "🐙 Molluscs", "codes": {"Wt"}, "category": "allergens"},
    "soy": {"name": "🌱 Soy", "codes": {"So"}, "category": "allergens"},
    "mustard": {"name": "🌭 Mustard", "codes": {"Sf"}, "category": "allergens"},
    "sesame": {"name": "🌾 Sesame", "codes": {"Se"}, "category": "allergens"},
    "celery": {"name": "🥬 Celery", "codes": {"Sl"}, "category": "allergens"},
    "lupin": {"name": "🫘 Lupin", "codes": {"Lu"}, "category": "allergens"},
    "sulfites": {"name": "🍷 Sulfites", "codes": {"Sw"}, "category": "allergens"},

    # Gluten
    "wheat": {"name": "🌾 Wheat", "codes": {"We"}, "category": "gluten"},
    "rye": {"name": "🌾 Rye", "codes": {"Ro"}, "category": "gluten"},
    "barley": {"name": "🌾 Barley", "codes": {"Ge"}, "category": "gluten"},
    "oats": {"name": "🌾 Oats", "codes": {"Ha"}, "category": "gluten"},
    "spelt": {"name": "🌾 Spelt", "codes": {"Di"}, "category": "gluten"},
    "kamut": {"name": "🌾 Kamut", "codes": {"Ka"}, "category": "gluten"},
    "hybrid": {"name": "🌾 Hybrid Strains", "codes": {"Hy"}, "category": "gluten"},

    # Tree Nuts
    "hazelnuts": {"name": "🌰 Hazelnuts", "codes": {"Hn"}, "category": "nuts"},
    "walnuts": {"name": "🌰 Walnuts", "codes": {"Wa"}, "category": "nuts"},
    "almonds": {"name": "🌰 Almonds", "codes": {"Ma"}, "category": "nuts"},
    "cashews": {"name": "🌰 Cashews", "codes": {"Cn"}, "category": "nuts"},
    "pistachios": {"name": "🌰 Pistachios", "codes": {"Pi"}, "category": "nuts"},
    "brazil_nuts": {"name": "🌰 Brazil Nuts", "codes": {"Pa"}, "category": "nuts"},
    "pecans": {"name": "🌰 Pecans", "codes": {"Pn"}, "category": "nuts"},
    "macadamia": {"name": "🌰 Macadamia", "codes": {"Mn"}, "category": "nuts"},

    # Additives & Other
    "alcohol": {"name": "🍷 Alcohol", "codes": {"A"}, "category": "additives"},
    "garlic": {"name": "🧄 Garlic", "codes": {"Kn"}, "category": "additives"},
    "colorant": {"name": "🎨 Colorant (1)", "codes": {"1"}, "category": "additives"},
    "preservative": {"name": "🧪 Preservative (2)", "codes": {"2"}, "category": "additives"},
    "antioxidant": {"name": "🛡️ Antioxidant (3)", "codes": {"3"}, "category": "additives"},
    "enhancer": {"name": "🧂 Enhancer (4)", "codes": {"4"}, "category": "additives"},
    "sulfured": {"name": "💨 Sulfured (5)", "codes": {"5"}, "category": "additives"},
    "sweeteners": {"name": "🍬 Sweeteners (8)", "codes": {"8"}, "category": "additives"},
    "phosphate": {"name": "⚗️ Phosphate (10)", "codes": {"10"}, "category": "additives"},
    "caffeine": {"name": "☕ Caffeine (11)", "codes": {"11"}, "category": "additives"},
}


def _food_preferences_management_view(category="main"):
    """Build the (text, reply_markup) pair for the food-preferences management
    screen. Supports interactive Mensa legend categories (meat, allergens,
    gluten, nuts, additives) so users can toggle avoid/allow states with
    single taps, alongside typed or voice input."""
    prefs = load_food_preferences()
    avoid_codes_set = set(prefs.get("avoid_codes", []))
    avoid_keywords = prefs.get("avoid_keywords", [])

    if category in FOOD_PREF_CATEGORIES:
        cat_info = FOOD_PREF_CATEGORIES[category]
        cat_title = cat_info["title"]
        keys = cat_info["keys"]

        text = (
            f"🚫 <b>Food Preferences: {cat_title}</b>\n\n"
            f"Tap an ingredient to toggle between allowed and avoided:\n"
            f"• 🚫 = Currently Avoided (Filtered from menu)\n"
            f"• ⚪ = Currently Allowed"
        )

        buttons = []
        for key in keys:
            item = FOOD_PREF_LEGEND[key]
            is_avoided = bool(avoid_codes_set & set(item["codes"]))
            prefix = "🚫 " if is_avoided else "⚪ "
            buttons.append(
                InlineKeyboardButton(
                    f"{prefix}{item['name']}",
                    callback_data=f"foodpref_toggle|{category}|{key}"
                )
            )

        # 2 buttons per row
        keyboard = []
        for i in range(0, len(buttons), 2):
            keyboard.append(buttons[i:i + 2])

        keyboard.append([
            InlineKeyboardButton("◀️ All Categories", callback_data="foodpref_cat|main"),
            InlineKeyboardButton("🍽️ Mensa Menu", callback_data="menu_nav|2/"),
        ])
        return text, InlineKeyboardMarkup(keyboard)

    # Main category overview
    avoided_items = []
    accounted_codes: set[str] = set()
    for item in FOOD_PREF_LEGEND.values():
        if avoid_codes_set & set(item["codes"]):
            avoided_items.append(f"🚫 {item['name']}")
            accounted_codes.update(item["codes"])

    for code in avoid_codes_set - accounted_codes:
        code_name = ALLERGEN_CODE_NAMES.get(code, code)
        avoided_items.append(f"🚫 {code} ({code_name})")

    for kw in avoid_keywords:
        avoided_items.append(f"🚫 \"{kw}\"")

    text = "🚫 <b>Mensa Food Preferences</b>\n\n"
    if avoided_items:
        text += f"<b>Currently avoiding ({len(avoided_items)} items):</b>\n"
        text += "\n".join(f"• {it}" for it in avoided_items)
        text += "\n\n"
    else:
        text += "<b>Currently avoiding:</b> None (all dishes shown)\n\n"

    text += (
        "Select a category below to browse the Mensa legend and toggle ingredients to avoid. "
        "Dishes containing avoided ingredients will be filtered out automatically."
    )

    keyboard = [
        [
            InlineKeyboardButton("🥩 Meat & Gelatin", callback_data="foodpref_cat|meat"),
            InlineKeyboardButton("🥜 Common Allergens", callback_data="foodpref_cat|allergens"),
        ],
        [
            InlineKeyboardButton("🌾 Gluten & Grains", callback_data="foodpref_cat|gluten"),
            InlineKeyboardButton("🌰 Tree Nuts", callback_data="foodpref_cat|nuts"),
        ],
        [
            InlineKeyboardButton("🧪 Additives & Other", callback_data="foodpref_cat|additives"),
        ],
    ]

    # If there are custom keywords, add remove buttons for them
    for kw in avoid_keywords:
        keyboard.append([InlineKeyboardButton(f"❌ Remove \"{kw}\"", callback_data=f"foodpref_remove|{kw}")])

    if avoided_items:
        keyboard.append([InlineKeyboardButton("🗑 Clear All Preferences", callback_data="foodpref_clear_ask")])

    keyboard.append([InlineKeyboardButton("🍽️ Back to Mensa Menu", callback_data="menu_nav|2/")])
    return text, InlineKeyboardMarkup(keyboard)


async def handle_menu_preferences_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu_prefs callback — show the interactive legend management view."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    text, keyboard = _food_preferences_management_view(category="main")
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_menu_photos_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu_photos|<sub_path> callback ("📸 Show Dish Photos") — opt-in
    photos for the day currently being viewed, re-fetching that day's menu
    to get dish_items/menu_date (not carried in the button itself) and
    sending one labeled message per dish that has a matched photo."""
    query = update.callback_query
    await query.answer("Loading photos...")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    parts = query.data.split("|")
    sub_path = parts[1] if len(parts) > 1 else "2/"

    try:
        session = await login_studip()
        prefs = load_food_preferences()
        _, _, _, menu_date, dish_items, _ = await get_todays_menu_enhanced(
            session, sub_path=sub_path, avoid_codes=prefs["avoid_codes"], avoid_keywords=prefs["avoid_keywords"]
        )
        if not dish_items:
            await query.message.reply_text("❌ No dishes to show photos for.")
            return
        await _send_menu_items_individually(query.message, dish_items, menu_date, skip_unmatched=True)
    except Exception as e:
        logging.error("handle_menu_photos_button error: %s", e)
        await query.message.reply_text(f"❌ Error loading photos: {str(e)[:200]}")


async def handle_menu_recommend_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu_recommend callback ("🍽️ What should I eat?") — ask the
    chat LLM to pick one recommended dish from today's Counter/Culinarium
    options via build_food_recommendation()."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    await _show_typing(query)
    status_msg = await query.message.reply_text("🍽️ Picking something for you...", disable_notification=True)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Picking something for you...")
    try:
        result = await build_food_recommendation()
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    if not result:
        await query.message.reply_text("❌ Couldn't reach the Mensa menu right now — try again in a bit.")
        return
    await _deliver_food_recommendation(query.message, result)


async def handle_foodpref_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle foodpref_cat|<cat_name> callback — navigate to a legend category or back to main."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    cat_name = (query.data or "").split("|")[1] if "|" in (query.data or "") else "main"
    text, keyboard = _food_preferences_management_view(category=cat_name)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_foodpref_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle foodpref_toggle|<cat_name>|<item_key> callback — toggle avoidance of an ingredient."""
    query = update.callback_query

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    parts = query.data.split("|")
    cat_name = parts[1] if len(parts) > 1 else "main"
    item_key = parts[2] if len(parts) > 2 else ""

    item = FOOD_PREF_LEGEND.get(item_key)
    if not item:
        await query.answer("Item not found.")
        return

    prefs = load_food_preferences()
    current_codes = set(prefs.get("avoid_codes", []))
    item_codes = set(item["codes"])

    is_avoided = bool(current_codes & item_codes)
    if is_avoided:
        new_codes = sorted(list(current_codes - item_codes))
    else:
        new_codes = sorted(list(current_codes | item_codes))
    # save before any await, so two quick taps can't overwrite each other
    save_food_preferences(new_codes, prefs.get("avoid_keywords", []))
    await query.answer(f"✅ Allowed {item['name']}" if is_avoided else f"🚫 Now avoiding {item['name']}")

    text, keyboard = _food_preferences_management_view(category=cat_name)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_foodpref_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tapped "Remove" — remove that one code/keyword and re-render
    the management view in place."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    value = query.data.split("|", 1)[1]
    prefs = load_food_preferences()
    codes = [c for c in prefs["avoid_codes"] if c != value]
    keywords = [k for k in prefs["avoid_keywords"] if k != value]
    save_food_preferences(codes, keywords)

    text, keyboard = _food_preferences_management_view(category="main")
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_foodpref_clear_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, clear all", callback_data="foodpref_clear_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="foodpref_clear_cancel"),
    ]])
    await query.edit_message_text("🗑 Clear all food preferences? You'll see every dish again until you set new ones.", reply_markup=keyboard)


async def handle_foodpref_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cleared")
    save_food_preferences([], [])
    text, keyboard = _food_preferences_management_view(category="main")
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_foodpref_clear_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelled")
    text, keyboard = _food_preferences_management_view(category="main")
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
