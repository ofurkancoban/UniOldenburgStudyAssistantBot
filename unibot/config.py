"""Environment, constants and logging setup. Imported first by every module.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import logging
import os
import secrets
import tempfile
from logging.handlers import RotatingFileHandler


# Every cache/state file below is a relative path; anchor them to the bot's
# own directory so starting it from another working directory (PM2, cron,
# a shell elsewhere) never scatters or loses state.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── env / asyncio ──────────────────────────────────────────────────────────────
ALLOWED_USER_IDS_ENV = os.getenv("ALLOWED_USER_IDS", "").strip()
STUDIP_URL = "https://elearning.uni-oldenburg.de/dispatch.php/my_courses"


# ── user permissions ──────────────────────────────────────────────────────────
def _parse_allowed_user_ids(env_value: str) -> set[int]:
    if not env_value:
        return set()
    parts = [p.strip() for p in env_value.replace(";", ",").split(",") if p.strip()]
    ids: set[int] = set()
    for p in parts:
        try:
            ids.add(int(p))
        except Exception:
            logging.warning("Invalid user id in ALLOWED_USER_IDS: %s", p)
    return ids


ALLOWED_USER_IDS = _parse_allowed_user_ids(ALLOWED_USER_IDS_ENV)

ADMIN_USER_IDS_ENV = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = _parse_allowed_user_ids(ADMIN_USER_IDS_ENV)
if not ADMIN_USER_IDS and ALLOWED_USER_IDS:
    ADMIN_USER_IDS = set(ALLOWED_USER_IDS)


def _first_user_id(*env_values: str):
    for env_value in env_values:
        for p in env_value.replace(";", ",").split(","):
            try:
                return int(p.strip())
            except ValueError:
                continue
    return None


# The account the single-user era files (studip_cookies.json, ...) belong to:
# the first admin listed in the environment, which keeps its order.
PRIMARY_ADMIN_ID = _first_user_id(ADMIN_USER_IDS_ENV, ALLOWED_USER_IDS_ENV)


# ── logging setup ──────────────────────────────────────────────────────────────
# BOT_LOG_FILE lets tests and one-off scripts keep their output out of the live log
LOG_FILE = os.getenv("BOT_LOG_FILE", "watch_log.txt")
# SQLite database holding every cache/settings document (see unibot/db.py)
DB_FILE = os.getenv("BOT_DB_FILE", "bot_data.sqlite3")
try:
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
except (PermissionError, OSError):
    _temp_log = os.path.join(tempfile.gettempdir(), LOG_FILE)
    print(f"⚠️ Permission denied for {LOG_FILE}, logging to {_temp_log} instead.")
    _file_handler = RotatingFileHandler(_temp_log, maxBytes=5 * 1024 * 1024, backupCount=3)

# Exactly one console and one file handler with one format (basicConfig plus
# an extra StreamHandler used to print every line twice in two formats).
_LOG_FORMAT = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
_console_handler = logging.StreamHandler()
for _handler in (_file_handler, _console_handler):
    _handler.setFormatter(_LOG_FORMAT)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for _old_handler in list(root_logger.handlers):
    root_logger.removeHandler(_old_handler)
root_logger.addHandler(_file_handler)
root_logger.addHandler(_console_handler)
# httpx logs every request URL at INFO, and Telegram's URLs carry the bot
# token (api.telegram.org/bot<token>/...), so it would end up in the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
# pypdf warns about every malformed object in a course PDF ("Multiple
# definitions in dictionary ..."); the text is still extracted fine.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# ── WhatsApp microservice ─────────────────────────────────────────────────────
# The Node service only listens on 127.0.0.1 and rejects requests without this
# shared secret. When WA_SERVICE_TOKEN isn't set in .env, a random one is made
# per run and handed to the service through the environment it inherits when
# start_whatsapp_microservice() spawns it.
WA_SERVICE_PORT = int(os.getenv("PORT", "3838"))
WA_SERVICE_URL = f"http://127.0.0.1:{WA_SERVICE_PORT}"
WA_SERVICE_TOKEN = os.getenv("WA_SERVICE_TOKEN", "").strip() or secrets.token_urlsafe(32)
os.environ["WA_SERVICE_TOKEN"] = WA_SERVICE_TOKEN
WA_HEADERS = {"X-WA-Service-Token": WA_SERVICE_TOKEN}


# ════ Settings read from the environment (.env) ════════════════════════════════
# All module-level settings live here so they are read once, in one place,
# and checked at startup by validate_settings().

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ── AI providers (OpenRouter, GWDG) ───────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "cohere/north-mini-code:free")
# Dedicated text-to-speech models (a separate OpenRouter API surface from
# chat/completions — see synthesize_speech). deepgram/flux-tts:free requires
# an explicit "voice"; fish-audio/s2.1-pro-free:free doesn't need one.
OPENROUTER_TTS_MODEL = os.getenv("OPENROUTER_TTS_MODEL", "fish-audio/s2.1-pro-free:free")
OPENROUTER_TTS_VOICE = os.getenv("OPENROUTER_TTS_VOICE", "")  # fish-audio/s2.1-pro-free:free has no selectable voice param - OpenRouter's previous free TTS model (deepgram/flux-tts:free) was discontinued and this replaced it; passing a "voice" value this model doesn't support (e.g. the old "flux-elise-en") causes a 400
# GWDG's academic AI service (SAIA, free with an academic account). One
# quota of 2 requests/10 s, 400/day and 3000/month covers text, vision and
# image calls together, so every GWDG call falls back to the previous
# provider (OpenRouter, Cloudflare, the other try-on models) when it fails
# or the quota runs out. Picked after a live comparison on the bot's own
# prompts: Gemma 4 was right on every text task and answered in under a
# second every time, and tagged item photos best per second (~1 s, 14/15
# types right). DeepSeek V4 Flash made the best outfit picks (2-7 s) but
# sometimes stalls for 15-30 s, so it only gets the outfit pick, which can
# wait that long.
GWDG_API_KEY = os.getenv("GWDG_API_KEY", "")
GWDG_API_URL = os.getenv("GWDG_API_URL", "https://chat-ai.academiccloud.de/v1")
GWDG_TEXT_MODEL = os.getenv("GWDG_TEXT_MODEL", "gemma-4-31b-it")
GWDG_IMAGE_EDIT_SERVICE = os.getenv("GWDG_IMAGE_EDIT_SERVICE", "image-edit-2511")
GWDG_WHISPER_MODEL = os.getenv("GWDG_WHISPER_MODEL", "whisper-large-v3")

# ── AI wardrobe (outfit model, try-on providers) ──────────────────────────────
# Outfit picks need strict rule-following over a long item list without a
# slow thinking phase; in a live comparison this model got the layering
# right every time in 2-3 s, where the default model broke the rules.
OUTFIT_OPENROUTER_MODEL = os.getenv("OUTFIT_OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
# Free-tier providers for the "👔 My Wardrobe" daily outfit feature — kept
# separate from OpenRouter since neither vision tagging nor image generation
# runs on OpenRouter's free text models; both of these have a genuinely free
# tier instead (Cloudflare Workers AI's daily Neuron allowance, HF's shared
# ZeroGPU Spaces), see tag_wardrobe_item/generate_outfit_tryon_image.
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_VISION_MODEL = os.getenv("CLOUDFLARE_VISION_MODEL", "@cf/mistralai/mistral-small-3.1-24b-instruct")
CLOUDFLARE_IMAGE_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-dev")
HF_SPACE_VTON = os.getenv("HF_SPACE_VTON", "levihsu/OOTDiffusion")
HF_SPACE_ACCESSORIES = os.getenv("HF_SPACE_ACCESSORIES", "black-forest-labs/FLUX.1-Kontext-Dev")
HF_TOKEN = os.getenv("HF_TOKEN", "")
# fal.ai (paid). Primary try-on is Google's Gemini image model "Nano Banana
# 2" (~$0.06/image): one call dresses the reference photo in every garment
# photo at once, shoes and headwear included, and a live side-by-side
# comparison showed it matches the real items (cropped jeans, beanie shape
# and logo) better than FASHN, at ~40% of the cost and a third of the time.
# Set FAL_GEMINI_TRYON_MODEL to empty to skip it. FASHN (~$0.075/image,
# chained upper + lower) and the free HF Spaces below remain as fallbacks.
# See generate_outfit_tryon_image for the fallback order.
FAL_KEY = os.getenv("FAL_KEY", "")
FAL_GEMINI_TRYON_MODEL = os.getenv("FAL_GEMINI_TRYON_MODEL", "fal-ai/nano-banana-2/edit")
FAL_TRYON_MODEL = os.getenv("FAL_TRYON_MODEL", "fal-ai/fashn/tryon/v1.6")
GWDG_OUTFIT_MODEL = os.getenv("GWDG_OUTFIT_MODEL", "deepseek-v4-flash-0731")
GWDG_VISION_MODEL = os.getenv("GWDG_VISION_MODEL", "gemma-4-31b-it")

# ── study plan (course material search index) ─────────────────────────────────
GWDG_EMBEDDING_MODEL = os.getenv("GWDG_EMBEDDING_MODEL", "e5-mistral-7b-instruct")

# ── weather ───────────────────────────────────────────────────────────────────
# University of Oldenburg's coordinates, for the daily-plan weather snapshot
# (see get_weather_snapshot) — configurable in case the bot is ever reused
# for a different campus.
WEATHER_LATITUDE = os.getenv("WEATHER_LATITUDE", "53.1435")
WEATHER_LONGITUDE = os.getenv("WEATHER_LONGITUDE", "8.2146")

# ── /login Mini App ───────────────────────────────────────────────────────────
LOGIN_PUBLIC_URL = os.getenv("LOGIN_PUBLIC_URL", "").strip()
CLOUDFLARE_TUNNEL = os.getenv("CLOUDFLARE_TUNNEL", "").strip().lower()
# token of a named tunnel (CLOUDFLARE_TUNNEL=named); the hostname is LOGIN_PUBLIC_URL
CLOUDFLARE_TUNNEL_TOKEN = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()


# ── WhatsApp target group ─────────────────────────────────────────────────────
# Unlike the settings above this one changes at runtime (picked from inside
# the bot and written back to .env), so it is always read live.
def get_wa_group_id() -> str:
    return os.getenv("WA_GROUP_ID", "").strip()


def set_wa_group_id(group_id: str) -> None:
    """Persist the group ID to .env and make it visible to this process."""
    import dotenv
    dotenv.set_key(os.path.join(PROJECT_ROOT, ".env"), "WA_GROUP_ID", group_id)
    os.environ["WA_GROUP_ID"] = group_id


def validate_settings() -> list[str]:
    """Check the environment once at startup.

    Returns human readable warnings for optional settings that look wrong and
    raises SystemExit for anything the bot cannot run without, so a missing
    token fails right away with a clear message instead of deep inside a
    handler later.
    """
    if not (TELEGRAM_TOKEN or "").strip():
        raise SystemExit("TELEGRAM_TOKEN is not set (add it to .env).")
    warnings = []
    if not ALLOWED_USER_IDS:
        warnings.append("ALLOWED_USER_IDS is empty or invalid: every Telegram user will be rejected.")
    for name, value, low, high in (
        ("WEATHER_LATITUDE", WEATHER_LATITUDE, -90, 90),
        ("WEATHER_LONGITUDE", WEATHER_LONGITUDE, -180, 180),
    ):
        try:
            if not low <= float(value) <= high:
                raise ValueError
        except (TypeError, ValueError):
            warnings.append(f"{name}={value!r} is not a valid coordinate.")
    if CLOUDFLARE_TUNNEL not in ("", "quick", "named"):
        warnings.append(f"CLOUDFLARE_TUNNEL={CLOUDFLARE_TUNNEL!r} is not supported (use 'quick', 'named' or leave it empty).")
    if CLOUDFLARE_TUNNEL == "named" and not (CLOUDFLARE_TUNNEL_TOKEN and LOGIN_PUBLIC_URL.startswith("https://")):
        warnings.append("CLOUDFLARE_TUNNEL=named needs CLOUDFLARE_TUNNEL_TOKEN and an https:// LOGIN_PUBLIC_URL.")
    if not OPENROUTER_API_KEY and not GWDG_API_KEY:
        warnings.append("Neither OPENROUTER_API_KEY nor GWDG_API_KEY is set: AI features are disabled.")
    return warnings

