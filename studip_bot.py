import asyncio
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import pyotp
import uuid
import hashlib
from urllib.parse import unquote, urljoin
import errno
from bs4 import BeautifulSoup
import html
import re
import tempfile
import aiohttp
import logging
import os
import json
import psutil
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo
from telegram.constants import ChatAction
from asyncio import CancelledError
from studip_session import StudIPSession

TZ_BERLIN = ZoneInfo("Europe/Berlin")

# ── env / asyncio ──────────────────────────────────────────────────────────────
load_dotenv()

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_IDS_ENV = os.getenv("ALLOWED_USER_IDS", "").strip()
BASE_URL = "https://elearning.uni-oldenburg.de"
STUDIP_URL = "https://elearning.uni-oldenburg.de/dispatch.php/my_courses"
last_full_check_time = None
FILE_WATCHER_INTERVAL = 2 * 60 * 60
# ── global runtime state ───────────────────────────────────────────────────────
global_session = None
global_watcher_paused = False
unified_watcher_task = None
watcher_controller_running = False
global_message_watcher = None
global_announcement_watcher = None
# navigation & cache
link_cache = {}  # short_id -> { url, cid, name?, action?, user_id?, current_url?, ts }
nav_stack = {}  # user_id -> [url1, url2, ...]
nav_names = {}  # user_id -> [name1, name2, ...]  (for breadcrumb)
user_courses = {}  # user_id -> cid
courses_map = {}  # cid -> course_name
watch_tasks = {}  # chat_id -> asyncio.Task
start_in_progress = set()  # chat_id currently running /start
START_MENU_DEDUP_SECONDS = 30
check_in_progress: set[int] = set()  # chat_id set to prevent concurrent checks

# Concurrency locks
cache_lock = asyncio.Lock()


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
            logging.warning(f"Invalid user id in ALLOWED_USER_IDS: {p}")
    return ids


ALLOWED_USER_IDS = _parse_allowed_user_ids(ALLOWED_USER_IDS_ENV)


def is_user_allowed(user_id: int) -> bool:
    # If allowlist is empty, allow everyone (backward compatibility) — but warn
    if not ALLOWED_USER_IDS:
        logging.warning("⚠️ ALLOWED_USER_IDS is empty — bot is open to everyone.")
        return True
    return user_id in ALLOWED_USER_IDS


# link_cache limits
LINK_CACHE_TTL_SECONDS = 3600  # 1 hour
LINK_CACHE_MAX_ITEMS = 2000


def cleanup_link_cache():
    """Remove expired entries and cap the cache size."""
    try:
        now_ts = datetime.now().timestamp()
        # Expire by TTL
        expired_keys = [k for k, v in link_cache.items() if (now_ts - v.get("ts", now_ts)) > LINK_CACHE_TTL_SECONDS]
        for k in expired_keys:
            link_cache.pop(k, None)
        # Cap size by removing oldest
        if len(link_cache) > LINK_CACHE_MAX_ITEMS:
            # Sort by ts ascending (oldest first)
            sorted_keys = sorted(link_cache.keys(), key=lambda k: link_cache[k].get("ts", 0.0))
            for k in sorted_keys[: max(0, len(link_cache) - LINK_CACHE_MAX_ITEMS)]:
                link_cache.pop(k, None)
    except Exception:
        # Do not allow cleanup to crash the bot
        pass


def load_message_cache() -> list[dict]:
    """Load message cache from JSON file."""
    CACHE_FILE = "messages_cache.json"
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"load_message_cache failed: {e}")
    return []


def clean_for_html(text: str) -> str:
    """Clean text for HTML parse mode."""
    if not text:
        return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.strip()


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


def get_deterministic_hash(text: str) -> str:
    """Return a deterministic hex hash of the given string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_date_safe(date_str: str | None) -> datetime:
    """Safely parse various date formats into a datetime object."""
    if not date_str:
        return datetime.min
    date_str = str(date_str).strip()

    formats = [
        "%d/%m/%y %H:%M", "%d/%m/%Y %H:%M",
        "%d.%m.%y %H:%M", "%d.%m.%Y %H:%M",
        "%d/%m/%y", "%d/%m/%Y",
        "%d.%m.%y", "%d.%m.%Y"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return datetime.min


def _parse_tooltip_fields(tip_html: str) -> dict:
    """Extract Title, Beginning, End, and Location from tooltip/aria text."""
    if not tip_html:
        return {}
    
    # Unescape and clean
    tip_html = html.unescape(tip_html)
    
    res = {}
    # Try as HTML first
    if "<" in tip_html and ">" in tip_html:
        try:
            tip = BeautifulSoup(tip_html, "html.parser")
            h4 = tip.find("h4")
            if h4:
                res["title"] = h4.get_text(" ", strip=True)

            # Location (HTML)
            loc_div = None
            for div in tip.find_all("div"):
                if div:
                    b = div.find("b")
                    if b and b.get_text(strip=True).lower() in {"location:", "ort:", "room:", "yer:"}:
                        loc_div = div
                        break
            if loc_div:
                b = loc_div.find("b")
                if b and b.next_sibling:
                    loc_text = str(b.next_sibling).strip()
                else:
                    loc_text = loc_div.get_text(" ", strip=True)
                    loc_text = re.sub(r"^(Location|Ort|Room|Yer)\s*:\s*", "", loc_text, flags=re.I).strip()
                res["location"] = loc_text
        except Exception as e:
            logging.debug(f"HTML tooltip parsing failed: {e}")
    
    def _try_parse_dt(s: str) -> datetime | None:
        if not s:
            return None
        s = re.sub(r"\b(CEST|CET|UTC|GMT)\b", "", s or "", flags=re.I).strip()
        fmts = [
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%a %d %b %Y %H:%M:%S", "%a %d %b %Y %H:%M",
            "%A %d %B %Y %H:%M:%S", "%A %d %B %Y %H:%M",
        ]
        s = re.sub(r"^[A-Za-z]+,\s*", "", s).strip()
        for fmt in fmts:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    # Text-based fallback
    try:
        soup_text = BeautifulSoup(tip_html, "html.parser")
        plain_text = soup_text.get_text("\n", strip=True)
        
        if not res.get("title"):
            lines = [l.strip() for l in plain_text.split("\n") if l.strip()]
            if lines:
                res["title"] = lines[0]

        # Beginning / End
        m_b = re.search(r"(Beginning|Beginn|Start)\s*:\s*([^\n,]+)", plain_text, flags=re.I)
        m_e = re.search(r"(End|Ende|Finish)\s*:\s*([^\n,]+)", plain_text, flags=re.I)
        if m_b:
            res["start"] = _try_parse_dt(m_b.group(2))
        if m_e:
            res["end"] = _try_parse_dt(m_e.group(2))
            
        # Location (Plain Text)
        if not res.get("location"):
            m_l = re.search(r"(Location|Ort|Room|Yer)\s*:\s*([^\n]+)", plain_text, flags=re.I)
            if m_l:
                res["location"] = m_l.group(2).strip()
    except Exception as e:
        logging.debug(f"Text tooltip parsing failed: {e}")

    return res


def _breadcrumb(user_id: int) -> str:
    """Format the navigation breadcrumb for the user."""
    parts = nav_names.get(user_id, [])
    if not parts:
        return "📂 *Root*"
    if len(parts) == 1:
        return f"📂 *{parts[0]}*"
    else:
        chain = " › ".join(parts)
        return f"📂 *{chain}*"


def _status_emoji(status: str) -> str:
    """Return an emoji based on the event status."""
    return {"ongoing": "⏳", "upcoming": "⏰", "past": "✅"}.get(status, "⚪")


def _safe_loc(loc: str) -> str:
    """Clean and return the location string with a fallback."""
    return (loc or "Unknown").strip()


def _get_day_emoji(day_name: str) -> str:
    """Return an emoji for the given day of the week."""
    day_emojis = {
        "Monday": "📅", "Tuesday": "🔥", "Wednesday": "🌍",
        "Thursday": "🚀", "Friday": "🎉", "Saturday": "🌞", "Sunday": "🌟"
    }
    return day_emojis.get(day_name, "📌")


# ── cache ─────────────────────────────────────────────────────────────────────

FILES_CACHE_PATH = "files_cache.json"
REMINDERS_CACHE_PATH = "reminders_cache.json"
GENERAL_CACHE_PATH = "general_cache.json"


def load_files_cache():
    """Safely load JSON cache of previous file states."""
    if os.path.exists(FILES_CACHE_PATH):
        try:
            with open(FILES_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load file cache: {e}")
    return {}


def save_files_cache(data):
    """Safely save JSON cache of file states."""
    try:
        tmp = FILES_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, FILES_CACHE_PATH)
        logging.info("✅ files_cache.json successfully updated.")
    except Exception as e:
        logging.error(f"Failed to save file cache: {e}")


def load_reminders_cache():
    if os.path.exists(REMINDERS_CACHE_PATH):
        try:
            with open(REMINDERS_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load reminders cache: {e}")
    return {}


def save_reminders_cache(data):
    try:
        tmp = REMINDERS_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REMINDERS_CACHE_PATH)
    except Exception as e:
        logging.error(f"Could not save reminders cache: {e}")


def load_general_cache():
    if os.path.exists(GENERAL_CACHE_PATH):
        try:
            with open(GENERAL_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load general cache: {e}")
    return {}


def save_general_cache(data):
    try:
        tmp = GENERAL_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, GENERAL_CACHE_PATH)
    except Exception as e:
        logging.error(f"Could not save general cache: {e}")


# ── logging setup ──────────────────────────────────────────────────────────────
LOG_FILE = "watch_log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
try:
    _file_handler = logging.FileHandler(LOG_FILE)
except (PermissionError, OSError):
    _temp_log = os.path.join("/tmp", LOG_FILE)
    print(f"⚠️ Permission denied for {LOG_FILE}, logging to {_temp_log} instead.")
    _file_handler = logging.FileHandler(_temp_log)

_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
root_logger = logging.getLogger()
root_logger.addHandler(_file_handler)
root_logger.addHandler(_console_handler)

# ── single-instance lock ───────────────────────────────────────────────────────
LOCK_FILE = ".bot_instance.lock"


def acquire_instance_lock():
    """Ensure only one process instance runs (prevents getUpdates 409)."""
    try:
        # Check if lock exists and if the PID is still running
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r") as f:
                    pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    logging.error(f"❌ Another bot instance is actually running (PID {pid}). Exiting.")
                    return False
                else:
                    logging.warning(f"⚠️ Stale lock file found (PID {pid} not running). Removing it.")
                    os.remove(LOCK_FILE)
            except Exception as e:
                logging.warning(f"⚠️ Failed to read/validate lock file: {e}. Attempting to overwrite.")
                try: os.remove(LOCK_FILE)
                except: pass

        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            logging.error("Another bot instance is already running (concurrency race).")
            return False
        raise


def release_instance_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# ── helpers ────────────────────────────────────────────────────────────────────
# ── broadcast helper ──────────────────────────────────────────────────────────
async def broadcast(bot, text, parse_mode="HTML", disable_notification=True, reply_markup=None):
    """Send a message to all allowed users with optional reply markup."""
    for uid in ALLOWED_USER_IDS:
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                reply_markup=reply_markup
            )
            await asyncio.sleep(0.3)  # Telegram rate limit safety
        except Exception as e:
            logging.warning(f"Failed to send broadcast to {uid}: {e}")


def _normalize_url(u: str | None) -> str | None:
    """Ensure URLs from Stud.IP HTML/JSON are valid."""
    if not u:
        return None
    u = u.strip()
    # If it's just a 32-char hex ID, it's not a valid URL yet
    if len(u) == 32 and re.match(r"^[a-f0-9]{32}$", u.lower()):
        return None

    # normalize schema slashes
    u = re.sub(r"^https:/*", "https://", u)
    # fix double host concatenations
    if "https://elearning.uni-oldenburg.dehttps://" in u:
        u = u.split("https://elearning.uni-oldenburg.de")[-1]
        if not u.startswith("https://"):
            u = "https://elearning.uni-oldenburg.de" + u
    # prefix host if relative
    if not u.startswith("http://") and not u.startswith("https://"):
        if not u.startswith("/"):
            u = "/" + u
        u = f"https://elearning.uni-oldenburg.de{u}"
    return u


# ── selectors (IdP flow) ─────────────────────────────────────────────────────────
SELECTORS = {
    "start": "main#content-wrapper a[href*='login']",
    "user": "#Ecom_User_ID",
    "user_btn": "#loginButton2",
    "pass": "#nffc, input[type='password']",
    "pass_btn": "#loginButton2",
    "otp": "#nffc, input[name*='otp'], input[name*='token']",
    "otp_btn": "#loginButton2",
}


# ── Stud.IP login ──────────────────────────────────────────────────────────────
async def login_studip(notify=None):
    global global_session
    
    if global_session is None:
        global_session = StudIPSession(USERNAME, PASSWORD, TOTP_SECRET)
    
    if await global_session.is_logged_in():
        return global_session

    if notify:
        await notify("Starting login...")
    
    success = await global_session.login()
    
    if success:
        if notify:
            await notify("Login successful.")
        logging.info("Logged in successfully (browser-less session).")
        return global_session
    else:
        if notify:
            await notify("Login failed.")
        raise RuntimeError("Failed to log in to Stud.IP.")


# ── unified watcher controller ─────────────────────────────────────────────────

async def unified_watcher_controller(app):
    """Centrally manage all watchers - with a single task"""
    global watcher_controller_running, global_watcher_paused, global_session

    logging.info("🟢 Unified Watcher Controller STARTED")
    watcher_controller_running = True

    # Wait 2 minutes for initial check (for bot to fully start)
    await asyncio.sleep(120)

    # Determine admin user
    admin_id = next(iter(ALLOWED_USER_IDS), None)
    if not admin_id:
        logging.error("❌ No allowed users found for watcher")
        return

    cycle_count = 0

    while True:
        try:
            if global_watcher_paused:
                logging.info("⏸️ Watcher controller paused")
                await asyncio.sleep(30)
                continue

            # Ensure session and login
            try:
                session = await login_studip()
                if not session:
                    logging.warning("🔑 Failed to acquire logged-in session, retrying...")
                    await asyncio.sleep(60)
                    continue
            except Exception as e:
                logging.error(f"❌ Login error in watcher: {e}")
                await asyncio.sleep(60)
                continue

            cycle_count += 1
            logging.info(f"🔄 Watcher cycle #{cycle_count} started")

            # 0️⃣ CALENDAR REMINDERS & MORNING SUMMARY
            now = datetime.now(TZ_BERLIN)
            
            # Check for morning summary (Daily at 07:00)
            if now.hour >= 7:
                cache = load_general_cache()
                today_str = now.strftime("%Y-%m-%d")
                if cache.get("last_morning_summary") != today_str:
                    try:
                        logging.info("🌞 Sending morning summary...")
                        await send_morning_summary(app.bot, ALLOWED_USER_IDS)
                        cache["last_morning_summary"] = today_str
                        save_general_cache(cache)
                    except Exception as e:
                        logging.error(f"❌ Morning summary failed: {e}")

            # Regular calendar reminders
            try:
                # Send to all allowed users
                for uid in ALLOWED_USER_IDS:
                    await check_calendar_reminders(app.bot, uid, silent=True)
            except Exception as e:
                logging.error(f"❌ Calendar reminder check failed: {e}")

            # 1️⃣ MESSAGE CHECK
            try:
                logging.info("📨 Checking messages...")
                await check_new_messages(app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Message check failed: {e}")

            await asyncio.sleep(30)

            # 2️⃣ ANNOUNCEMENT CHECK
            try:
                logging.info("📢 Checking announcements...")
                await check_new_announcements_parallel(app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Announcement check failed: {e}")

            await asyncio.sleep(30)

            # 3️⃣ FILE CHECK
            if cycle_count % 2 == 0:
                try:
                    logging.info("📁 Checking files...")
                    await check_new_files_parallel(app.bot, admin_id, silent=True)
                except Exception as e:
                    logging.error(f"❌ File check failed: {e}")

            # 4️⃣ FORUM CHECK (Every cycle now)
            try:
                logging.info("💬 Checking forum posts...")
                await check_new_forum_posts_parallel(app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Forum check failed: {e}")

            logging.info(f"✅ Watcher cycle #{cycle_count} completed")
            await asyncio.sleep(90)

        except Exception as e:
            logging.error(f"❌ Watcher controller error: {e}")
            await asyncio.sleep(120)


async def start_unified_watcher(app):
    """Start the unified watcher"""
    global unified_watcher_task, watcher_controller_running

    if watcher_controller_running:
        logging.info("⚠️ Watcher controller already running")
        return

    logging.info("🚀 Starting unified watcher controller...")
    unified_watcher_task = asyncio.create_task(unified_watcher_controller(app))

    # Check initial state
    await asyncio.sleep(5)
    if watcher_controller_running:
        logging.info("✅ Unified watcher controller started successfully")
    else:
        logging.error("❌ Failed to start watcher controller")


async def stop_unified_watcher():
    """Stop the unified watcher"""
    global unified_watcher_task, watcher_controller_running

    if unified_watcher_task and not unified_watcher_task.done():
        unified_watcher_task.cancel()
        try:
            await unified_watcher_task
        except asyncio.CancelledError:
            pass

    watcher_controller_running = False
    logging.info("🛑 Unified watcher controller stopped")


# ── course listing ────────────────────────────────────────────────────────────
async def list_courses():
    """
    Extract courses from the 'my_courses' page.
    Uses multi-method strategy: Vuex Store analysis (modern) and DOM Scrapy (legacy).
    """
    session = await login_studip()
    if session is None:
        logging.error("❌ Failed to get session for list_courses")
        return []

    all_courses_dict = {}
    
    # Try multiple attempts or varying URLs if needed
    urls = [
        "https://elearning.uni-oldenburg.de/dispatch.php/my_courses",
        "https://elearning.uni-oldenburg.de/dispatch.php/my_courses?semester_filter=all"
    ]
    
    for url in urls:
        logging.info(f"Attempting course extraction from: {url}")
        try:
            async with await session.get(url) as r:
                html_text = await r.text()
        except Exception as e:
            logging.error(f"Failed to fetch {url}: {e}")
            continue
            
        # --- Method 1: JSON-based Vuex state (Comprehensive) ---
        # Stud.IP often embeds a JSON payload for its Vue.js components
        json_scripts = re.findall(r'<script\s+type="application/json"[^>]*>(.*?)</script>', html_text, re.S)
        for script_content in json_scripts:
            try:
                content = script_content.strip()
                # Sometimes it's wrapped in a JS call, sometimes raw
                if 'JSON.parse(' in content:
                    inner_match = re.search(r'JSON\.parse\(\s*(["\'])(.*?)\1\s*\)', content, re.S)
                    if inner_match:
                        # Handle escaped JSON string
                        json_str = inner_match.group(2).encode().decode('unicode_escape')
                        full_data = json.loads(json_str)
                    else: continue
                else:
                    full_data = json.loads(content)

                # Direct deep-dive into known store paths
                # Path 1: vuexStoreData -> mycourses
                if isinstance(full_data, dict):
                    store_data = full_data.get("vuexStoreData", {})
                    mycourses = store_data.get("mycourses", {})
                    
                    # setCourses contains the flat map of all courses
                    courses_payload = mycourses.get("setCourses", {})
                    if courses_payload:
                        for cid, info in courses_payload.items():
                            if isinstance(info, dict) and "name" in info:
                                name = info.get("name", "").strip()
                                if name:
                                    all_courses_dict[cid] = name
                                    courses_map[cid] = name
                                    
                    # Fallback: Recursive search in this specific JSON block
                    if not courses_payload:
                        def find_courses_recursive(obj):
                            if not isinstance(obj, (dict, list)): return
                            if isinstance(obj, dict):
                                if "setCourses" in obj and isinstance(obj["setCourses"], dict):
                                    for k, v in obj["setCourses"].items():
                                        if isinstance(v, dict) and "name" in v:
                                            all_courses_dict[k] = v.get("name")
                                elif "id" in obj and "name" in obj and len(str(obj["id"])) == 32:
                                    all_courses_dict[str(obj["id"])] = obj["name"]
                                for v in obj.values(): find_courses_recursive(v)
                            elif isinstance(obj, list):
                                for v in obj: find_courses_recursive(v)
                        find_courses_recursive(full_data)
            except Exception as e:
                logging.debug(f"JSON extract failed for one script: {e}")
                continue

        # --- Method 2: BeautifulSoup Scraper (Legacy/Traditional) ---
        soup = BeautifulSoup(html_text, "html.parser")
        
        # Pattern 1: Table rows with data-course-id (Common in desktop view)
        for row in soup.find_all(["tr", "div", "li"], attrs={"data-course-id": True}):
            cid = row.get("data-course-id")
            # Try to find a name in the text contents or a title
            name = row.get("title") or row.get_text(" ", strip=True)
            if cid and name and len(name) > 3:
                all_courses_dict[cid] = name
                courses_map[cid] = name

        # Pattern 2: Any link containing a course ID pattern
        course_links = soup.find_all("a", href=re.compile(r"(?:course_id=|to=|details\.php\?id=)([a-f0-9]{32})"))
        for a in course_links:
            href = a.get("href", "")
            match = re.search(r"(?:course_id=|to=|details\.php\?id=)([a-f0-9]{32})", href)
            if match:
                cid = match.group(1)
                name = a.get_text(strip=True)
                if name and len(name) > 3:
                    if cid not in all_courses_dict:
                        all_courses_dict[cid] = name
                        courses_map[cid] = name
        
        # If we found courses on the first URL, we might still want to try the second 
        # to ensure we get "all" if the first was restricted to one semester.
    
    if all_courses_dict:
        logging.info(f"✅ Extracted {len(all_courses_dict)} unique courses.")
        # Ensure the map is updated for other functions
        for cid, name in all_courses_dict.items():
            courses_map[cid] = name
        # The bot expects (name, cid) tuples
        return [(name, cid) for cid, name in all_courses_dict.items()]

    logging.warning("⚠️ No courses found during extraction.")
    return []




# ── menu functions ─────────────────────────────────────────────────
FOOD_CODES = {
    # Allergens
    "1": "🎨",  # with colorant
    "2": "🧪",  # with preservative
    "3": "🛡️",  # with antioxidant
    "Ei": "🥚",  # Eggs
    "Mi": "🥛",  # Milk/Lactose
    "So": "🌱",  # Soy
    "We": "🌾",  # Wheat
    "Ha": "🥜",  # Hazelnuts
    "Di": "🫘",  # Lupin
    "Ge": "🥥",  # Coconut
    "Kn": "🧄",  # Garlic
    "Ro": "🌹",  # Rose
    "Sl": "🥬",  # Celery
    "Sf": "🌭",  # Mustard
    "Sw": "🍷",  # Sulfites
    "Fi": "🐟",  # Fish
    "Wt": "🐌",  # Molluscs

    # Meat types
    "S": "🐷",  # Pork
    "Sch": "🐷",  # Pork
    "Su": "🐷",  # Pork
    "G": "🐔",  # Poultry
    "R": "🐄",  # Beef
    "L": "🐑",  # Lamb
    "W": "🦌",  # Game
    "F": "🐠",  # Fish

    # Dietary
    "V": "🥗",  # Vegetarian
    "V+": "🌿",  # Vegan
}


def translate_food_codes(text):
    """Translate food codes to emojis with proper handling"""
    if not text:
        return ""

    # Handle special cases first
    text = text.replace("V+", "🌿")
    text = text.replace("V", "🥗")

    # Code mappings - only versions without parentheses
    code_map = {
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

    # Process longer codes first (more specific ones)
    sorted_codes = sorted(code_map.keys(), key=len, reverse=True)

    for code in sorted_codes:
        text = text.replace(code, code_map[code])

    return text


async def get_todays_menu_enhanced(session, sub_path="2/"):
    """Enhanced menu fetching with dynamic allergen guide and navigation links"""
    try:
        # If sub_path is a full URL, extract the part after menu/
        if "mensawidget/menu/" in sub_path:
            sub_path = sub_path.split("mensawidget/menu/")[-1]
            
        url = f"{BASE_URL}/plugins.php/mensawidget/menu/{sub_path}"

        async with await session.get(url) as r:
            html_content = await r.text()

        soup = BeautifulSoup(html_content, "html.parser")

        # Determine the effective date of the menu being requested
        effective_ts = int(datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
        ts_match = re.search(r'/(\d{10})(?:/|$)', sub_path)
        if ts_match:
            base_ts = int(ts_match.group(1))
            if "/next" in sub_path:
                effective_ts = base_ts + 86400
            elif "/previous" in sub_path:
                effective_ts = base_ts - 86400
            else:
                effective_ts = base_ts

        # Fallback: Check if the page actually specifies a date (rare in fragments)
        date_element = soup.find('h2')
        if date_element:
            date_page_text = date_element.get_text(strip=True)
            if re.search(r'\d{2}\.\d{2}\.\d{4}', date_page_text):
                date_text = date_page_text
            else:
                date_text = datetime.fromtimestamp(effective_ts).strftime("%A, %d.%m.%Y")
        else:
            date_text = datetime.fromtimestamp(effective_ts).strftime("%A, %d.%m.%Y")

        # Navigation links: increment/decrement relative to effective date for infinite stepping
        prev_link = f"2/{effective_ts}/previous"
        next_link = f"2/{effective_ts}/next"

        menu_text = f"🍽️ <b>{html.escape(date_text)}</b> 🍽️\n"
        menu_text += "🏛️ Mensa Uni Oldenburg\n\n"

        # Process all categories
        categories = soup.find_all('table', class_='default')
        if not categories:
            return "🍽️ <b>Mensa Uni Oldenburg</b>\n\n❌ No dishes found for this date. The Mensa might be closed.", prev_link, next_link

        categories_data = []
        all_allergens_used = set()

        category_priority = {
            "COUNTER ONE": 1, "COUNTER TWO": 2, "COUNTER THREE": 3, "COUNTER FOUR": 4,
            "Main Dishes": 5, "PIZZA": 10,
            "Culinarium Main Dishes": 20, "Culinarium Side Dishes": 21,
            "Culinarium Salads": 22, "Culinarium Desserts": 23,
            "Soup": 30, "Side Dishes": 40, "Salads": 50, "Desserts": 60
        }

        for category_table in categories:
            category_name = category_table.find('th').get_text(strip=True)
            original_category_name = category_name # Keep for sorting

            # Map category names to display names
            category_map = {
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
                "Culinarium Desserts": "👨‍🍳 CULINARIUM DESSERT"
            }

            # Handle empty category names - check for pizza items
            if not category_name.strip():
                items = category_table.find_all('tr')[1:]
                has_pizza = False
                for item in items:
                    cols = item.find_all('td')
                    if len(cols) >= 2:
                        name = cols[0].get_text(strip=True)
                        if 'pizza' in name.lower():
                            has_pizza = True
                            break
                if has_pizza:
                    category_name = "PIZZA"
                    original_category_name = "PIZZA"

            display_name = category_map.get(category_name, f"🍴 {category_name}")
            if category_name == "PIZZA": display_name = "🍕 PIZZA"

            cat_chunk = "━━━━━━━━━━━━━━━━━━\n"
            cat_chunk += f"{display_name}\n"
            cat_chunk += "━━━━━━━━━━━━━━━━━━\n"

            items = category_table.find_all('tr')[1:]
            items_found = False

            for item in items:
                cols = item.find_all('td')
                if len(cols) >= 2:
                    items_found = True
                    name_cell = cols[0]
                    price = cols[1].get_text(strip=True)

                    temp_cell = BeautifulSoup(str(name_cell), 'html.parser')
                    allergen_spans = temp_cell.find_all('span', class_='attributes')
                    allergens_text = ""
                    for span in allergen_spans:
                        span_text = span.get_text(strip=True)
                        if span_text:
                            allergens_text = span_text
                            all_allergens_used.update([a.strip() for a in span_text.split(',')])
                        span.decompose()

                    for abbr in temp_cell.find_all('abbr'): abbr.decompose()
                    clean_text = temp_cell.get_text("\n", strip=True)
                    lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                    
                    if lines:
                        name_text = lines[0]
                        description = ' '.join(lines[1:]) if len(lines) > 1 else ""
                        name_text = re.sub(r'[1-9][0-9]*[A-Za-z,+\s]*$', '', name_text).strip()
                        name_text = re.sub(r'\([^)]*\)$', '', name_text).strip()
                        if name_text:
                            cat_chunk += f"• <b>{html.escape(name_text)}</b>"
                            if '⭐' in name_text or 'limited' in name_text.lower(): cat_chunk += " ⭐"
                            cat_chunk += "\n"
                            if description:
                                description = re.sub(r'[1-9][0-9]*[A-Za-z,+\s]*$', '', description).strip()
                                description = re.sub(r'\([^)]*\)$', '', description).strip()
                                if description: cat_chunk += f"  {html.escape(description)}\n"
                            if allergens_text:
                                cat_chunk += f"  {translate_food_codes(allergens_text)}\n"
                                cat_chunk += f"  <i>({html.escape(allergens_text)})</i>\n"
                            
                            clean_price = price.replace("&euro;", "€").replace("€", "€").strip().replace('.', ',')
                            if clean_price and clean_price != "€":
                                if "€" not in clean_price: clean_price += "€"
                                cat_chunk += f"  💶 {clean_price}\n\n"
                            else: cat_chunk += "\n"

            if items_found:
                priority = category_priority.get(original_category_name, 100)
                categories_data.append((priority, cat_chunk))

        # Sort by priority and append to menu_text
        categories_data.sort(key=lambda x: x[0])
        for _, chunk in categories_data:
            menu_text += chunk

        # Create COMPLETE allergen guide based on what's actually used today
        menu_text += "━━━━━━━━━━━━━━━━━━\n"
        menu_text += "📋 <b>ALLERGEN GUIDE</b>\n"
        menu_text += "━━━━━━━━━━━━━━━━━━\n"

        # Define COMPLETE allergen mappings
        allergen_guide = {
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
            "V+": "Vegan"
        }

        # Add only the allergens that were actually used in today's menu
        used_guide_lines = []
        for allergen_code in sorted(all_allergens_used):
            if allergen_code in allergen_guide:
                emoji = translate_food_codes(allergen_code)
                # Add in EMOJI + CODE + DESCRIPTION format
                used_guide_lines.append(f"{emoji} <b>{allergen_code}</b> - {allergen_guide[allergen_code]}")

        # Add the used allergens to the menu
        if used_guide_lines:
            menu_text += "\n".join(used_guide_lines)
        else:
            menu_text += "No allergens listed in today's menu"

        menu_text += "\n\n⭐ <b>Limited availability</b>"

        return menu_text, prev_link, next_link

    except Exception as e:
        logging.error(f"Enhanced menu fetch error: {e}")
        return "❌ Menu could not be loaded. Please try again later.", None, None


# ── menu commands ────────────────────────────────────────────────────────────

def get_menu_navigation_keyboard(prev_path=None, next_path=None):
    """Create navigation keyboard for Mensa menu"""
    row = []
    if prev_path:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"menu_nav|{prev_path}"))
    
    row.append(InlineKeyboardButton("Today", callback_data="menu_nav|2/"))
    
    if next_path:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"menu_nav|{next_path}"))
    
    return InlineKeyboardMarkup([row])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily food menu in the requested format"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return

    try:
        # Session check
        session = await login_studip()

        # Fetch menu
        menu_text, prev, next_ = await get_todays_menu_enhanced(global_session)

        # Send menu with navigation buttons
        await update.message.reply_text(
            menu_text,
            parse_mode="HTML",
            reply_markup=get_menu_navigation_keyboard(prev, next_)
        )

    except Exception as e:
        error_msg = f"❌ Error loading menu:\n{str(e)}"
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_keyboard()
        )
        logging.error(f"Menu command error: {e}")


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for menu navigation items"""
    query = update.callback_query
    # Split data by | to support menu_nav|path
    parts = query.data.split("|")
    sub_path = parts[1] if len(parts) > 1 else "2/"

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.answer("Not authorized.")
        return

    try:
        # Show specific loading status for the button pressed
        direction = "..."
        if "next" in sub_path: direction = "Next day..."
        elif "previous" in sub_path: direction = "Previous day..."
        elif sub_path == "2/": direction = "Today..."
        
        await query.answer(f"Loading {direction}")
        
        # Check if we should send a NEW message instead of editing
        # (Useful for morning summary where we want to keep the schedule)
        should_reply = len(parts) > 2 and parts[2] == "new"

        # Session check
        session = await login_studip()

        menu_text, prev, next_ = await get_todays_menu_enhanced(session, sub_path=sub_path)
        
        if should_reply:
            await query.message.reply_text(
                menu_text,
                parse_mode="HTML",
                reply_markup=get_menu_navigation_keyboard(prev, next_)
            )
        else:
            await query.edit_message_text(
                menu_text, 
                parse_mode="HTML",
                reply_markup=get_menu_navigation_keyboard(prev, next_)
            )

    except Exception as e:
        error_str = str(e)
        if "Message is not modified" in error_str:
            # Not a real error, just the same menu content
            try:
                await query.answer("All current information is already displayed.")
            except:
                pass
            return

        error_msg = f"❌ Error loading menu:\n{error_str}"
        # If edit fails (e.g. same text), don't crash
        try:
            await query.edit_message_text(error_msg)
        except:
            pass
        logging.error(f"Menu button navigation error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
#  FILE LISTING + ZIP CREATION + WATCHER
# ──────────────────────────────────────────────────────────────────────────────


async def list_files(cid, folder_url=None):
    """Extract files/folders from Stud.IP file table browser-less."""
    global global_session
    url = _normalize_url(folder_url) if folder_url else f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
    logging.info(f">>> list_files called with cid={cid}, folder_url={folder_url} => {url}")
    
    try:
        async with await global_session.get(url) as resp:
            html_text = await resp.text()
    except Exception as e:
        logging.error(f"Failed to fetch files: {e}")
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    
    # --- Method 1: JSON-based extraction (Robust) ---
    form = soup.select_one("form#files_table_form")
    if form:
        try:
            data_files_raw = form.get("data-files")
            data_folders_raw = form.get("data-folders")
            
            data_files = json.loads(data_files_raw) if data_files_raw else []
            data_folders = json.loads(data_folders_raw) if data_folders_raw else []
            
            # Process folders
            for folder in data_folders:
                name = folder.get("name", "Unknown Folder")
                furl = folder.get("url")
                if furl: furl = urljoin(BASE_URL, furl)
                
                # Modified date
                chdate = folder.get("chdate")
                modified = "-"
                modified_iso = None
                if chdate:
                    try:
                        dt = datetime.fromtimestamp(int(chdate), tz=timezone.utc).astimezone(TZ_BERLIN)
                        modified = dt.strftime("%d.%m.%Y %H:%M")
                        modified_iso = dt.isoformat()
                    except: pass
                
                items.append({
                    "type": "folder",
                    "name": name,
                    "url": furl,
                    "size": "-",
                    "modified": modified,
                    "modified_iso": modified_iso
                })
            
            # Process files
            for f in data_files:
                name = f.get("name", "Unknown File")
                durl = f.get("download_url")
                if durl: durl = urljoin(BASE_URL, durl)
                
                # Size formatting
                raw_size = f.get("size")
                size_str = "-"
                if raw_size:
                    try:
                        rs = int(raw_size)
                        if rs > 1024*1024: size_str = f"{rs/(1024*1024):.1f} MB"
                        elif rs > 1024: size_str = f"{rs/1024:.1f} KB"
                        else: size_str = f"{rs} B"
                    except: size_str = str(raw_size)
                
                # Modified date
                chdate = f.get("chdate")
                modified = "-"
                modified_iso = None
                if chdate:
                    try:
                        dt = datetime.fromtimestamp(int(chdate), tz=timezone.utc).astimezone(TZ_BERLIN)
                        modified = dt.strftime("%d.%m.%Y %H:%M")
                        modified_iso = dt.isoformat()
                    except: pass
                    
                items.append({
                    "type": "file",
                    "name": name,
                    "url": durl,
                    "size": size_str,
                    "modified": modified,
                    "modified_iso": modified_iso
                })
                
            if items:
                logging.info(f"✅ Extracted {len(items)} items via JSON from {url}")
                return items
        except Exception as json_err:
            logging.warning(f"JSON file extraction failed, falling back to BS4: {json_err}")

    # --- Method 2: BeautifulSoup Fallback (Legacy) ---
    table = soup.select_one("table.documents")
    if not table:
        return []

    # Subfolders
    for row in table.select("tbody.subfolders tr"):
        link_tag = row.select_one("td:nth-child(3) a")
        if not link_tag: continue
        name = link_tag.get_text(strip=True)
        href = link_tag.get("href")
        if href:
            href = urljoin(BASE_URL, href)
        
        time_tag = row.find("time")
        modified = time_tag.get_text(strip=True) if time_tag else "-"
        modified_iso = time_tag.get("datetime") if time_tag else None
        
        items.append({
            "type": "folder",
            "name": name,
            "url": href,
            "size": "-",
            "modified": modified,
            "modified_iso": modified_iso
        })

    # Files
    for row in table.select("tbody.files tr"):
        name_tag = row.select_one("td:nth-child(3) a span") or row.select_one("td:nth-child(3) a")
        if not name_tag: continue
        name = name_tag.get_text(strip=True)
        
        download_btn = row.select_one("a[title^='Download file'], a[href*='download/file']")
        href = download_btn.get("href") if download_btn else None
        if not href:
            link_tag = row.select_one("td:nth-child(3) a")
            href = link_tag.get("href") if link_tag else None
            
        if href:
            href = urljoin(BASE_URL, href)
            
        time_tag = row.find("time")
        modified = time_tag.get_text(strip=True) if time_tag else "-"
        modified_iso = time_tag.get("datetime") if time_tag else None
        size = row.select_one("td:nth-child(4) span").get_text(strip=True) if row.select_one("td:nth-child(4) span") else "-"
        
        items.append({
            "type": "file",
            "name": name,
            "url": href,
            "size": size,
            "modified": modified,
            "modified_iso": modified_iso
        })

    return items


async def get_fresh_file_url(cid, filename, current_url=None):
    """Return a fresh, valid download URL for a given file name browser-less."""
    try:
        logging.info(f"🔄 Getting fresh URL for: {filename} in course {cid}")
        if not current_url:
            current_url = f"{BASE_URL}/dispatch.php/course/files?cid={cid}"

        files = await list_files(cid, current_url)

        # Find file with exact match
        for f in files:
            if f["type"] == "file" and f["name"] == filename:
                url = f["url"]
                if url:
                    # URL'yi normalize et
                    normalized_url = _normalize_url(url)
                    logging.info(f"✅ Found URL for {filename}: {normalized_url}")
                    return normalized_url

        # If exact match not found, check root page
        logging.info(f"🔍 File not found in current location, checking root...")
        root_url = f"{BASE_URL}/dispatch.php/course/files?cid={cid}"
        if current_url != root_url:
            files = await list_files(cid, root_url)

            for f in files:
                if f["type"] == "file" and f["name"] == filename:
                    url = f["url"]
                    if url:
                        normalized_url = _normalize_url(url)
                        logging.info(f"✅ Found URL in root for {filename}: {normalized_url}")
                        return normalized_url

        logging.warning(f"❌ File not found anywhere: {filename}")
        return None

    except Exception as e:
        logging.error(f"❌ Error getting fresh URL for {filename}: {e}")
        return None


# ── recursive ZIP (downloads all files + subfolders) ───────────────────────────
async def create_recursive_zip(cid, base_url, root_name: str, progress_callback=None):
    """Download all files (including subfolders) recursively browser-less."""
    from io import BytesIO
    import zipfile
    import posixpath
    global global_session

    async def _crawl(url, path_prefix, collected, empty_folders):
        try:
            items = await list_files(cid, url)
        except Exception as e:
            logging.warning(f"Failed to list {path_prefix}: {e}")
            return

        if not items:
            empty_folders.add(path_prefix)
            return

        for it in items:
            if it["type"] == "file":
                collected.append((it, path_prefix))
            elif it["type"] == "folder":
                sub_prefix = posixpath.join(path_prefix, it["name"])
                await _crawl(it["url"], sub_prefix, collected, empty_folders)

    all_files = []
    empty_folders = set()
    await _crawl(base_url, root_name, all_files, empty_folders)
    total = len(all_files)

    if total == 0:
        return BytesIO(), 0, root_name, 0, 0

    buf = BytesIO()
    total_size = 0
    success_count = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zipf:
        for folder in sorted(empty_folders):
            if folder and not folder.endswith("/"):
                zipf.writestr(folder + "/", "")

        for i, (f, folder_path) in enumerate(all_files, start=1):
            filename = f.get("name") or "unnamed"
            zip_path = f"{folder_path}/{filename}"

            # Use URL already found during crawl
            file_url = f.get("url")
            if not file_url:
                logging.warning(f"⚠️ No URL found for {filename}, skipping.")
                continue

            try:
                # Normalize URL just in case
                file_url = _normalize_url(file_url)
                async with await global_session.get(file_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        total_size += len(content)
                        zipf.writestr(zip_path, content)
                        success_count += 1
                    else:
                        logging.warning(f"⚠️ Failed to download {filename} (Status: {resp.status})")
            except Exception as e:
                logging.warning(f"❌ Failed to download {filename}: {e}")

            if progress_callback:
                await progress_callback(total, i)

    buf.seek(0)
    return buf, total, root_name, success_count, total_size


# ── new and updated file detection ─────────────────────────────────────────────

def _parse_file_date_v2(modified_text: str | None, modified_iso: str | None):
    """
    Parses file modification date from ISO string or text.
    Supports:
    - ISO format (from datetime attribute)
    - German/English standard dates (dd.mm.yyyy, etc.)
    - Relative times (x min ago, vor x Min)
    - Time only (HH:MM -> assumes today)
    """
    # 1. ISO format priority
    if modified_iso:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d"):
            try:
                return datetime.strptime(modified_iso, fmt)
            except Exception:
                pass

    # 2. Text fallback
    s = (modified_text or "").strip()
    if not s:
        return datetime.min

    now = datetime.now()

    # A) Relative times (German & English)
    # "vor 5 Minuten", "vor 2 Std.", "10 minutes ago", "just now", "gerade eben"

    # Simple regex for "x unit ago" pattern
    # Matches: "vor 10 Min", "10 min ago", "vor 1 Std", "1 hour ago"
    m_rel = re.search(r'(?:vor|ago)?\s*(\d+)\s*(?:Min|min|Std|hour|Stunde|M|h)', s, re.IGNORECASE)
    if m_rel:
        try:
            val = int(m_rel.group(1))
            unit = m_rel.group(0).lower()
            if 'h' in unit or 'std' in unit or 'stunde' in unit:
                return now - timedelta(hours=val)
            else:
                return now - timedelta(minutes=val)
        except:
            pass

    # "Just now" / "Gerade eben"
    if any(k in s.lower() for k in ["gerade", "just now", "now", "soeben"]):
        return now

    # B) Time only (HH:MM) -> Assume today
    # e.g. "14:30"
    if re.match(r'^\d{1,2}:\d{2}$', s):
        try:
            t = datetime.strptime(s, "%H:%M").time()
            return datetime.combine(now.date(), t)
        except:
            pass

    # C) Standard Date Formats
    fmts = (
        "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M",
        "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M",
        "%d.%m.%Y", "%d.%m.%y",
        "%d/%m/%Y", "%d/%m/%y",
        "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d. %b %Y", "%d %b %Y",  # 12. Jan 2025
    )
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass

    return datetime.min


async def watch_loop(chat_id, context):
    """Legacy watch loop - now delegates to unified watcher (which uses context.application)"""
    if hasattr(context, "application"):
        await unified_watcher_controller(context.application)
    else:
        # Fallback if context doesn't have application
        logging.warning("watch_loop called without application context")


def get_show_last_keyboard():
    """Show Last keyboard with menu button"""
    kb = [
        [InlineKeyboardButton("📢 Show Last 5 Announcements", callback_data="show_last_announcements")],
        [InlineKeyboardButton("📨 Show Last 5 Messages", callback_data="show_last_messages")],
        [InlineKeyboardButton("📁 Show Last 5 Files", callback_data="show_last_files")],
        [InlineKeyboardButton("💬 Show Last 5 Forum Posts", callback_data="show_last_forum_posts")],
    ]
    return InlineKeyboardMarkup(kb)


async def show_last_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 files with labeled header."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    FILES_CACHE_PATH = "files_cache.json"

    if not os.path.exists(FILES_CACHE_PATH):
        await query.edit_message_text("⚠️ No file history found.")
        return

    with open(FILES_CACHE_PATH, "r", encoding="utf-8") as f:
        cache = json.load(f)

    all_files = []
    for cid, files in cache.items():
        if not isinstance(files, dict):
            continue
        for name, info in files.items():
            if not isinstance(info, dict):
                continue
            course = info.get("course") or courses_map.get(cid) or cid
            ts = info.get("modified_ts")
            dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.min
            all_files.append({
                "cid": cid,
                "course": course,
                "name": name,
                "size": info.get("size", "-"),
                "modified": info.get("modified", "-"),
                "dt": dt
            })

    if not all_files:
        await query.edit_message_text("⚠️ No files in cache.")
        return

    all_files.sort(key=lambda x: (x["dt"], x["course"], x["name"]))
    last5 = all_files[-5:]

    await query.edit_message_text("📁 <b>Last 5 Files</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for f in last5:
        sid = _short_id()
        link_cache[sid] = {"cid": f["cid"], "name": f["name"], "ts": datetime.now().timestamp()}

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>📁 File</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>📚 Course:</b> {html.escape(f['course'])}\n"
            f"<b>📄 Name:</b> {html.escape(f['name'])}\n"
            f"<b>📅 Date:</b> {html.escape(f['modified'])}\n"
            f"<b>💾 Size:</b> {html.escape(str(f['size']))}\n"
            "━━━━━━━━━━━━━━━━━"
        )

        btn = InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")
        markup = InlineKeyboardMarkup([[btn]])

        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


def _parse_ann_date(s: str) -> datetime:
    """Safely convert Stud.IP announcement date string (default: very old)."""
    if not s:
        return datetime.min
    s = s.strip()
    fmts = [
        "%d/%m/%y", "%d/%m/%Y",  # 17/09/25
        "%d.%m.%y", "%d.%m.%Y",  # 17.09.2025
        "%d %b %y", "%d %b %Y",  # 17 Sep 25
        "%d.%m.%y %H:%M", "%d.%m.%Y %H:%M",
        "%d/%m/%y %H:%M", "%d/%m/%Y %H:%M",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    # If text comes and contains 2-digit year, rough estimate:
    try:
        parts = re.findall(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s)
        if parts:
            d, m, y = parts[0]
            y = int(y)
            if y < 100:  # 25 -> 2025 assumption
                y += 2000
            return datetime(int(y), int(m), int(d))
    except Exception:
        pass
    return datetime.min


async def check_new_announcements_parallel(bot, chat_id, silent: bool = False):
    """Parallel announcement checking for new items"""
    global global_session, courses_map
    session = global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📢 Checking announcements...", disable_notification=True)

    try:
        CACHE_PATH = "announcement_cache.json"
        cache = {"seen": [], "history": []}
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
        seen = set(cache.get("seen", []))

        if not courses_map:
            courses_list = await list_courses()
            courses = courses_list
        else:
            courses = [(name, cid) for cid, name in courses_map.items()]

        all_found = []
        semaphore = asyncio.Semaphore(3)

        async def check_single_course_announcements(course_name, cid):
            async with semaphore:
                try:
                    url = f"{BASE_URL}/dispatch.php/course/overview?cid={cid}"
                    async with await session.get(url) as r:
                         html_text = await r.text()

                    if "loginform" in html_text:
                        logging.warning(f"🔐 Session expired while accessing {course_name}.")
                        await session.login(force=True)
                        return course_name, cid, []

                    soup = BeautifulSoup(html_text, "html.parser")
                    ann_container = None
                    for art in soup.select("article.studip"):
                        h = art.select_one("header h1")
                        if h:
                            h_text = h.get_text(strip=True)
                            if "Announcements" in h_text or "Ankündigungen" in h_text:
                                ann_container = art
                                break

                    if not ann_container:
                        return course_name, cid, []

                    course_announcements = []
                    for art in ann_container.select("article.studip.toggle"):
                        ann_id = art.get("id") or ""
                        # Adjust selectors for direct HTML
                        title_tag = art.select_one("header h1 a") or art.select_one("header h1")
                        sender_tag = art.select_one(".news_user")
                        date_tag = art.select_one(".news_date")
                        body_tag = art.select_one(".formatted-content.ck-content")

                        title = title_tag.get_text(strip=True) if title_tag else "(No subject)"
                        sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
                        date_s = date_tag.get_text(strip=True) if date_tag else ""
                        body = body_tag.get_text("\n", strip=True) if body_tag else "(No content)"

                        key = f"{cid}:{ann_id}" if ann_id else f"{cid}:{title}:{date_s}"
                        dt = _parse_ann_date(date_s)

                        announcement = {
                            "key": key, "cid": cid, "course": course_name, "subject": title,
                            "sender": sender, "date": date_s, "dt": dt.isoformat() if dt else None, "body": body,
                        }
                        course_announcements.append(announcement)
                    return course_name, cid, course_announcements
                except Exception as e:
                    logging.warning(f"Announcement parse error in {course_name}: {e}")
                    return course_name, cid, []

        tasks = [check_single_course_announcements(name, cid) for name, cid in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, tuple) and len(result) == 3:
                all_found.extend(result[2])

        if all_found:
            all_found.sort(key=lambda x: (x["dt"] or "", x["date"]))
        new_items = [a for a in all_found if a["key"] not in seen]

        if new_items:
            for ann in new_items:
                text = (
                    "🔥 <b>NEW ANNOUNCEMENT</b> 🔥\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"🏫 <b>Course:</b> {ann['course']}\n"
                    f"👤 <b>From:</b> {ann['sender']}\n"
                    f"📘 <b>Subject:</b> {ann['subject']}\n"
                    f"🕒 <b>Date:</b> {ann['date']}\n\n"
                    f"{ann['body']}\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
                await broadcast(bot, text[:4000], parse_mode="HTML", reply_markup=markup)
        elif not silent:
            await bot.send_message(chat_id=chat_id, text="☑️ No new announcements found.", disable_notification=True)

        seen.update(a["key"] for a in all_found)
        cache["seen"] = list(seen)
        merged = cache.get("history", [])
        existing_keys = {h.get("key") for h in merged}
        for a in all_found:
            if a["key"] not in existing_keys:
                merged.append(a)
        cache["history"] = merged[-100:]

        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"check_new_announcements_parallel fatal: {e}")


async def fetch_message_body(session, message_url):
    """Fetch message body from detail page with improved parsing."""
    try:
        async with await session.get(message_url) as resp:
            if resp.status != 200:
                return f"[HTTP Error: {resp.status}]"
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        # Try different content containers
        content_selectors = [
            "div.formatted-content",
            "div.message-content",
            "div.news-content",
            "div.mail_content",
            ".formatted-content.ck-content"
        ]

        for selector in content_selectors:
            content = soup.select_one(selector)
            if content:
                # Remove unnecessary elements
                for elem in content.select("script, style, nav, header, footer"):
                    elem.decompose()

                text = content.get_text("\n", strip=True)
                if text and len(text.strip()) > 10:  # Meaningful content check
                    return text

        # Fallback: extract text from entire page
        main_content = soup.select_one("main") or soup.select_one("article") or soup
        text = main_content.get_text("\n", strip=True)

        # Truncate if too long
        if len(text) > 3000:
            text = text[:3000] + "..."

        return text if text.strip() else "No content found."

    except Exception as e:
        logging.error(f"fetch_message_body failed for {message_url}: {e}")
        return f"[Error fetching content: {str(e)[:100]}]"



async def forward_to_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward message to WhatsApp group via Node.js microservice."""
    query = update.callback_query
    await query.answer("Sending to WhatsApp...")
    
    text = query.message.text or query.message.caption or "No text found"
    group_name = os.getenv("WHATSAPP_GROUP_NAME", "StudIP Alerts")  # Group name from .env
    
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            payload = {"text": text, "groupName": group_name}
            async with session.post("http://localhost:3838/send", json=payload, timeout=10) as resp:
                if resp.status == 200:
                    await query.edit_message_reply_markup(reply_markup=None) # Remove button after sending
                    await context.bot.send_message(chat_id=query.message.chat_id, text="✅ Sent to WhatsApp successfully!")
                else:
                    data = await resp.json()
                    await context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Failed to send to WhatsApp: {data.get('error')}")
    except Exception as e:
        logging.error(f"WhatsApp forward error: {e}")
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Error connecting to WhatsApp service: {e}")


async def show_last_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 announcements (with labeled header)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    CACHE_PATH = "announcement_cache.json"

    if not os.path.exists(CACHE_PATH):
        await query.edit_message_text("⚠️ No announcement history available.")
        return

    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        cache = json.load(f)

    history = cache.get("history", [])
    if not isinstance(history, list) or not history:
        await query.edit_message_text("⚠️ No announcements found in history.")
        return

    history.sort(key=lambda x: x.get("dt", ""))
    last5 = history[-5:]

    await query.edit_message_text("📢 <b>Last 5 Announcements</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for ann in last5:
        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>📢 Announcement</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>🏫 Course:</b> {html.escape(ann.get('course', 'Unknown'))}\n"
            f"<b>📘 Subject:</b> {html.escape(ann.get('subject', '(No subject)'))}\n"
            f"<b>👤 From:</b> {html.escape(ann.get('sender', 'Unknown'))}\n"
            f"<b>🕒 Date:</b> {html.escape(ann.get('date', '-'))}\n\n"
            f"{html.escape(ann.get('body', ''))}\n"
            "━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


async def show_last_forum_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 forum posts (with labeled header)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    CACHE_PATH = "forum_cache.json"

    if not os.path.exists(CACHE_PATH):
        await query.edit_message_text("⚠️ No forum history available.")
        return

    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        await query.edit_message_text("⚠️ Error reading forum history.")
        return

    # Extract all posts from cache
    all_posts = []
    for key, post in cache.items():
        all_posts.append(post)

    if not all_posts:
        await query.edit_message_text("⚠️ No forum posts found in history.")
        return

    # Sort by date (text sort is not ideal but sufficient for now, or use 'key' if it has timestamp)
    # The key format is cid:thread_id:post_id_or_date.
    # Let's try to trust the order they were added or just reverse check
    # We don't have a reliable timestamp in the dict unless we parsed it.
    # But for "Show Last", usually showing the most recently added to cache is good.
    # Python dicts preserve insertion order in recent versions.
    # So taking the last 5 items might work if we append new ones.
    # But let's just show them.

    last5 = list(all_posts)[-5:]
    last5.reverse()  # Newest first

    await query.edit_message_text("💬 <b>Last 5 Forum Posts</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    for post in last5:
        # Get post data with fallbacks
        course = post.get('course', 'Unknown Course')
        thread = post.get('thread', 'Unknown Thread')
        author = post.get('author', 'Unknown')
        date = post.get('date') or post.get('timestamp', '-')
        body = post.get('body', '')

        # No blind truncation here because body contains HTML formatting.
        # It's better to manage length dynamically within the scraper.

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>💬 Forum Post</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"🏫 <b>Course:</b> {html.escape(course)}\n"
            f"📂 <b>Thread:</b> {html.escape(thread)}\n"
            f"👤 <b>By:</b> {html.escape(author)}\n"
            f"🕒 <b>Date:</b> {html.escape(date)}\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"{body}\n"
            "━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(0.3)

    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


async def check_new_files(bot, chat_id, silent: bool = False):
    """Wrapper for parallel file checking"""
    return await check_new_files_parallel(bot, chat_id, silent)


async def check_new_files_parallel(bot, chat_id, silent: bool = False):
    """Parallel file checking for better performance"""
    global global_session, courses_map
    session = global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📁 Checking files...", disable_notification=True)

    try:
        cache = load_files_cache()
        updated = False
        all_collected = []

        if not courses_map:
            courses = await list_courses()
        else:
            courses = [(name, cid) for cid, name in courses_map.items()]

        if not courses:
            return

        semaphore = asyncio.Semaphore(3)

        async def check_single_course(course_name, cid):
            async with semaphore:
                try:
                    files = await list_files(cid)
                    if not files:
                        return course_name, cid, [], []

                    current_files = {}
                    for f in files:
                        if f.get("type") != "file":
                            continue
                        name = f.get("name")
                        if not name:
                            continue
                        dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
                        f["modified_ts"] = None if dt == datetime.min else int(dt.timestamp())
                        current_files[name] = f

                    old_files = cache.get(cid, {})

                    # New files
                    new_files = [f for name, f in current_files.items() if name not in old_files]

                    # Changed files detection
                    changed_files = []
                    for name, f in current_files.items():
                        if name not in old_files:
                            continue
                        old = old_files.get(name, {})

                        old_ts = old.get("modified_ts")
                        cur_ts = f.get("modified_ts")

                        old_size = str(old.get("size", "-")).strip()
                        cur_size = str(f.get("size", "-")).strip()

                        # 1. Size Check
                        size_changed = (old_size != cur_size)

                        # 2. Timestamp logic
                        ts_changed = False

                        if size_changed:
                            # If size changed, we trust the update
                            ts_changed = True
                        else:
                            # Size is SAME. Be strict about timestamp changes.
                            # Case A: One is None, other is Value -> Ignore (likely format resolution)
                            if (old_ts is None and cur_ts is not None) or (old_ts is not None and cur_ts is None):
                                ts_changed = False

                            # Case B: Both meaningful
                            elif old_ts is not None and cur_ts is not None:
                                diff = abs(cur_ts - old_ts)
                                # If difference is small (e.g. < 24h), and size is same, assume same file (re-upload or date fix)
                                # Actually, < 60s is jitter. < 24h might be the daily date resolution we struggle with.
                                # Let's say: If size is SAME, we only alert if TS changed by > 24 hours.
                                if diff > 86400:
                                    ts_changed = True
                                else:
                                    ts_changed = False

                            # Case C: Both None -> No change
                            else:
                                ts_changed = False

                        if ts_changed:
                            changed_files.append(f)

                    # Collect results
                    collected = []
                    for kind, flist in (("new", new_files), ("changed", changed_files)):
                        for f in flist:
                            dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
                            collected.append({
                                "type": kind,
                                "cid": cid,
                                "course": course_name,
                                "name": f.get("name"),
                                "size": f.get("size", "-"),
                                "modified": f.get("modified", "-"),
                                "modified_iso": f.get("modified_iso"),
                                "dt": dt,
                                "url": f.get("url"),
                            })

                    # Update cache if anything new or changed
                    # IMPORTANT: Always update cache with fresh values, even if we didn't trigger a notification!
                    # This ensures we have the latest timestamp for future comparisons.
                    if current_files != old_files:
                        new_snapshot = {}
                        for name, info in current_files.items():
                            new_snapshot[name] = {
                                **info,
                                "course": course_name,
                                "modified_ts": info.get("modified_ts"),
                            }
                        cache[cid] = new_snapshot
                        # Only return 'needs_update=True' (notification) if we actually collected items
                        return course_name, cid, collected, True

                    return course_name, cid, collected, False

                except Exception as e:
                    logging.warning(f"Error while checking files for {course_name}: {e}")
                    return course_name, cid, [], False

        # Scan all courses in parallel
        tasks = [check_single_course(name, cid) for name, cid in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logging.error(f"Course check failed: {result}")
                continue

            if isinstance(result, tuple) and len(result) == 4:
                course_name, cid, collected, needs_update = result
                all_collected.extend(collected)
                if needs_update:
                    updated = True

        # Send notifications in chronological order WITH DOWNLOAD BUTTONS
        try:
            if all_collected:
                all_collected.sort(key=lambda x: (x["dt"], x["course"], x["name"]))

                for it in all_collected:
                    try:
                        sid = _short_id()

                        # Cache'e dosya bilgilerini kaydet
                        link_cache[sid] = {
                            "cid": it["cid"],
                            "name": it["name"],
                            "url": it.get("url"),
                            "ts": datetime.now().timestamp(),
                            "user_id": next(iter(ALLOWED_USER_IDS), None)
                        }

                        # Title and message content
                        title = "🔥 🆕 <b>NEW FILE ADDED</b> 🔥" if it["type"] == "new" else "🔥 ♻️ <b>FILE UPDATED</b> 🔥"
                        text = (
                            f"{title}\n"
                            "                 \n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            f"📚 <b>Course:</b> {it['course']}\n"
                            f"📄 <b>File:</b> {it['name']}\n"
                            f"📅 <b>Date:</b> {it['modified']}\n"
                            f"💾 <b>Size:</b> {it['size']}\n"
                            "━━━━━━━━━━━━━━━━━━"
                        )

                        # Create download button
                        download_button = InlineKeyboardButton("📥 Download", callback_data=f"file|{sid}")
                        keyboard = InlineKeyboardMarkup([[download_button]])

                        # Send message with button
                        await broadcast(bot, text, parse_mode="HTML", reply_markup=keyboard)
                        await asyncio.sleep(0.3)  # Rate limiting
                    except Exception as e:
                        logging.error(f"Error sending notification for file {it.get('name')}: {e}")

            else:
                if not silent:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="☑️ No new or updated files found.",
                        disable_notification=True
                    )
                logging.info("📁 No new or updated files found.")

        finally:
            # Ensure cache is saved even if something fails during notification
            if updated:
                save_files_cache(cache)

    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Error during file check:\n{e}")
        logging.error(f"check_new_files_parallel fatal: {e}")


async def send_morning_summary(bot, user_ids):
    """Fetch today's schedule and send to users with a button for the menu."""
    global global_session
    session = global_session
    if not session:
        return

    now = datetime.now(TZ_BERLIN)
    today_date = now.date()
    
    # 1. Get Schedule
    try:
        week_start = today_date - timedelta(days=today_date.weekday())
        events = await get_calendar_events(session=session, week_start=week_start)
        today_events = [ev for ev in events if ev["date_key"] == today_date]
    except Exception as e:
        logging.error(f"Summary schedule fetch error: {e}")
        today_events = []

    # 2. Format Schedule
    if not today_events:
        schedule_text = "📅 <b>No classes today!</b> Enjoy your day. ✨"
    else:
        lines = ["📅 <b>Today's Schedule:</b>"]
        course_blocks = []
        for ev in today_events:
            course_icon = _get_course_icon(ev.get("title", ""))
            display_title = clean_course_title(ev.get("title", ""))
            duration = ev["time"]
            loc_icon = _get_location_emoji(ev.get("location", ""))
            
            block = (
                f"{course_icon} <b>{display_title}</b>\n"
                f"   🕒 <code>{duration}</code>\n"
                f"   {loc_icon} {_safe_loc(ev['location'])}"
            )
            course_blocks.append(block)
        lines.append("\n\n".join(course_blocks))
        schedule_text = "\n".join(lines)

    # 3. Final Message
    header = (
        "☀️ <b>GOOD MORNING!</b> ☀️\n"
        f"🗓️ <b>Today:</b> {now:%d %B %Y, %A}\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    final_text = f"{header}{schedule_text}\n\n━━━━━━━━━━━━━━━━━━"
    
    # 4. Keyboard for Mensa Menu
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍴 Today's Menu", callback_data="menu_nav|2/|new")]
    ])

    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid, 
                text=final_text, 
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Failed to send summary to {uid}: {e}")



async def check_calendar_reminders(bot, chat_id, silent: bool = False):
    """Check for upcoming classes and send reminders 30 minutes before."""
    global global_session
    session = global_session
    try:
        if not silent:
            logging.info("⏰ Checking calendar reminders...")

        reminders = load_reminders_cache()
        updated = False

        today = datetime.now(TZ_BERLIN).date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=session, week_start=week_start)
        if not events:
            return

        now = datetime.now(TZ_BERLIN)

        for event in events:
            if not event.get("start_dt") or not event.get("title") or event["title"] == "Untitled":
                continue

            title = event["title"]
            start_dt = event["start_dt"]
            location = event.get("location", "Unknown location")
            # Create a unique key for this reminder (Title + Timestamp)
            event_key = f"{title}|{int(start_dt.timestamp())}"

            if event_key in reminders:
                continue

            diff = start_dt - now
            minutes_left = diff.total_seconds() / 60

            # Reminder window: 25-35 minutes before the lesson starts
            if 25 < minutes_left <= 35:
                text = (
                    "🔔 <b>LECTURE REMINDER</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>{title}</b>\n"
                    f"🕒 Starting in <b>{int(minutes_left)} minutes</b>\n"
                    f"📍 {location}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    logging.info(f"🔔 Sent reminder for {title} to {chat_id}")
                    reminders[event_key] = int(now.timestamp())
                    updated = True
                except Exception as e:
                    logging.error(f"Failed to send reminder for {title}: {e}")

        # Cleanup old reminders (older than 24 hours)
        cutoff = now.timestamp() - 86400
        new_cache = {k: v for k, v in reminders.items() if v > cutoff}
        if len(new_cache) != len(reminders):
            updated = True
        if updated:
            save_reminders_cache(new_cache)
    except Exception as e:
        logging.error(f"❌ Error in check_calendar_reminders: {e}")



async def check_new_messages(bot, chat_id, silent: bool = False):
    """Fetch Stud.IP messages fully (content included) and send in order (newest last)."""
    global global_session
    session = global_session
    if not silent:
        await bot.send_message(chat_id=chat_id, text="📨 Checking messages...", disable_notification=True)

    try:
        url = f"{BASE_URL}/dispatch.php/messages/overview"
        async with await session.get(url) as resp:
            html_text = await resp.text()

        soup = BeautifulSoup(html_text, "html.parser")
        rows = soup.select("table#messages tbody tr[id^='message_']")
        all_messages = []
        for tr in rows:
            link_tag = tr.select_one("td.title a[href*='dispatch.php/messages/read/']")
            if not link_tag: continue
            href = link_tag["href"].strip()
            title = link_tag.get_text(strip=True)
            sender_tag = tr.select_one("td:nth-of-type(3)")
            sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
            date_tag = tr.select_one("td:nth-of-type(4)")
            date = date_tag.get_text(strip=True) if date_tag else "Unknown"

            if not href.startswith('http'):
                href = f"{BASE_URL}/{href.lstrip('/')}"

            all_messages.append({
                "id": tr["id"].replace("message_", ""), "title": title, "sender": sender, "date": date, "url": href,
            })

        old_messages = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    old_messages = json.load(f)
            except: pass

        old_ids = {m["id"] for m in old_messages}
        new_messages = [m for m in all_messages if m["id"] not in old_ids]

        if not new_messages:
            if not silent:
                await bot.send_message(chat_id=chat_id, text="☑️ No new messages found.", disable_notification=True)
            return False

        # Use the existing fetch_message_body with our instance
        for msg in new_messages:
            msg["body"] = await fetch_message_body(session, msg["url"])

        # Update cache with new messages prepended or appended
        # Best to just store all recent ones
        all_messages_combined = (new_messages + old_messages)[:100]
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_messages_combined, f, indent=2, ensure_ascii=False)

        new_messages_sorted = sorted(new_messages, key=lambda m: parse_date_safe(m.get("date", "")))
        for msg in new_messages_sorted:
            body_text = msg.get("body", "📭 Content error").strip()
            text = (
                "🔥 📩 <b>NEW MESSAGE</b> 🔥\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>From:</b> {msg.get('sender', 'Unknown')}\n"
                f"📘 <b>Subject:</b> {msg.get('title', '(No subject)')}\n"
                f"🕒 <b>Date:</b> {msg.get('date', '-')}\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"{body_text}\n"
                "━━━━━━━━━━━━━━━━━"
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
            await broadcast(bot, text[:4000], parse_mode="HTML", reply_markup=markup)
        return True
    except Exception as e:
        logging.error(f"check_new_messages failed: {e}")
        return False


# ── forum checking ─────────────────────────────────────────────────────────────

async def check_new_forum_posts_parallel(bot, chat_id, silent: bool = False):
    """Parallel forum checking for new posts browser-less"""
    global global_session, courses_map
    session = global_session

    if not silent:
        await bot.send_message(chat_id=chat_id, text="💬 Checking forum posts...",
                               disable_notification=True)

    try:
        CACHE_PATH = "forum_cache.json"

        # ── Load cache safely ────────────────────────────────────────────────
        # Structure: { key: { cid, thread_id, thread_title, post_id, author, date, body_snippet } }
        cache = {}
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                logging.warning("⚠️ Failed to load forum cache, using empty.")

        # ── Ensure courses are loaded ───────────────────────────────────────
        if not courses_map:
            # Try to load courses if empty
            courses_list = await list_courses()
            courses_map = {cid: name for name, cid in courses_list}

        courses = [(name, cid) for cid, name in courses_map.items()]
        new_posts_found = []

        # ── Parallel scraping setup ─────────────────────────────────────────
        semaphore = asyncio.Semaphore(3)  # Don't overload the server

        async def check_single_course_forum(course_name, cid):
            async with semaphore:
                try:
                    # Use the modern JSON API for Vue forums
                    url = f"{BASE_URL}/jsonapi.php/v1/courses/{cid}/forum-discussions"
                    async with await session.get(url, headers={"Accept": "application/vnd.api+json"}) as r:
                        if r.status != 200:
                            return []
                        data = await r.json()
                        
                    discussions = data.get("data", [])
                    course_posts = []
                    
                    # Limit to checking top 10 most recently updated discussions to save requests
                    for disc in discussions[:10]:
                        title = disc.get("attributes", {}).get("title", "Unknown")
                        disc_id = disc.get("id")
                        unread_count = disc.get("meta", {}).get("unread-postings-count", 0)
                        
                        # We consider it "new" if unread-postings-count > 0 
                        is_new_ind = unread_count > 0

                        # Fetch the postings for this discussion
                        posts_url = f"{BASE_URL}/jsonapi.php/v1/forum-discussions/{disc_id}/postings?include=author"
                        async with await session.get(posts_url, headers={"Accept": "application/vnd.api+json"}) as r2:
                            if r2.status != 200:
                                continue
                            posts_data = await r2.json()
                            
                        posts = posts_data.get("data", [])
                        if not posts: continue
                        
                        # Helper to map author ID to name from included data
                        def get_author_name(aid):
                            if aid and "included" in posts_data:
                                for inc in posts_data["included"]:
                                    if inc.get("type") == "forum-members" and inc.get("id") == aid:
                                        return inc.get("attributes", {}).get("name", "Unknown")
                            return "Unknown"
                            
                        def friendly_date(iso_str):
                            if not iso_str: return "Unknown date"
                            try:
                                dt = datetime.fromisoformat(iso_str)
                                return dt.strftime("%Y-%m-%d %H:%M")
                            except Exception:
                                return str(iso_str)
                            
                        history_bodies = []
                        # Take ALL posts to show full conversation context
                        for post in posts:
                            p_author_id = post.get("relationships", {}).get("author", {}).get("data", {}).get("id")
                            p_author = get_author_name(p_author_id)
                            p_html = post.get("attributes", {}).get("content-html", "")
                            p_body = BeautifulSoup(p_html, "html.parser").get_text(separator=' ', strip=True)[:400] if p_html else "No content"
                            p_date = friendly_date(post.get("attributes", {}).get("mkdate", ""))
                            history_bodies.append(f"👤 <b>{html.escape(p_author)}</b> <i>({p_date})</i>:\n{html.escape(p_body)}")
                            
                        # If the thread is super long, just take the last 15 messages so Telegram won't reject it (4096 char limit)
                        if len(history_bodies) > 15:
                            history_bodies = ["<i>... (older messages omitted) ...</i>"] + history_bodies[-15:]
                            
                        last_post = posts[-1]
                        main_author_id = last_post.get("relationships", {}).get("author", {}).get("data", {}).get("id")
                        author = get_author_name(main_author_id)
                        time_str = friendly_date(last_post.get("attributes", {}).get("mkdate", datetime.now().isoformat()))
                        
                        if len(history_bodies) > 1:
                            final_body = f"💬 <b>Conversation History:</b>\n\n" + "\n\n".join(history_bodies)
                        else:
                            content_html = last_post.get("attributes", {}).get("content-html", "")
                            body = BeautifulSoup(content_html, "html.parser").get_text(separator=' ', strip=True)[:500] if content_html else "No content"
                            final_body = f"📩 <b>Last message:</b>\n{html.escape(body)}"
                        
                        course_posts.append({
                            "course": course_name,
                            "thread": title,
                            "author": author,
                            "date": time_str,
                            "body": final_body,
                            "key": f"{cid}:{title}:{disc_id}",
                            "is_new": is_new_ind,
                            "timestamp": time_str
                        })
                        
                    return course_posts
                except Exception as e:
                    logging.warning(f"Error while checking forum API for {course_name}: {e}")
                    return []

        # Scan all courses in parallel
        tasks = [check_single_course_forum(name, cid) for name, cid in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, list):
                for p in res:
                    post_key = p["key"]
                    # Add to notification list if really new OR if it was marked with new indicator
                    if post_key not in cache and p.get("is_new"):
                        new_posts_found.append(p)
                    
                    # ALWAYS update/add to cache so "Show Last 5" works immediately
                    cache[post_key] = p

        if new_posts_found:
            for p in new_posts_found:
                text = (
                    "🔥 💬 <b>NEW FORUM POST</b> 🔥\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"🏫 <b>Course:</b> {html.escape(p.get('course', ''))}\n"
                    f"📂 <b>Thread:</b> {html.escape(p.get('thread', ''))}\n"
                    f"👤 <b>By:</b> {html.escape(p.get('author', ''))}\n"
                    f"🕒 <b>Date:</b> {html.escape(p.get('date', '-'))}\n"
                    "━━━━━━━━━━━━━━━━━\n"
                    f"{p['body']}\n"
                    "━━━━━━━━━━━━━━━━━"
                )
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
                await broadcast(bot, text, parse_mode="HTML", reply_markup=markup)

        # Always save cache to bootstrap the "Last 5" feature
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
            
        if not new_posts_found and not silent:
            await bot.send_message(chat_id=chat_id, text="☑️ No new forum posts found (cache updated).", disable_notification=True)

    except Exception as e:
        logging.error(f"check_new_forum_posts_parallel fatal: {e}")



async def check_new_forum_posts(bot, chat_id, silent: bool = False):
    """Wrapper"""
    return await check_new_forum_posts_parallel(bot, chat_id, silent)


# ── watcher loop ───────────────────────────────────────────────────────────────


CACHE_FILE = "messages_cache.json"

# ───────────────────────────────────────────────
# MAIN MESSAGE CHECK (same logic as file watcher)
# ───────────────────────────────────────────────

# Simple helpers for messages_cache.json file
MSG_CACHE_FILE = "messages_cache.json"
MAX_CACHE_SIZE = 200  # upper limit to avoid bloating cache

import re
from datetime import datetime
from bs4 import BeautifulSoup


def _get_course_icon(title: str) -> str:
    """Emoji based on course type."""
    if not title:
        return "📘"
    patterns = {
        r"\b(lecture|vorlesung)\b": "🎓",
        r"\b(exercise|übung|tutorial)\b": "🧩",
        r"\b(seminar)\b": "🗣️",
        r"\b(language|sprachkurs|deutsch|english)\b": "🗺️",
        r"\b(meeting|besprechung)\b": "🏫",
        r"\b(lab|praktikum)\b": "🔬",
    }
    for pat, icon in patterns.items():
        if re.search(pat, title, flags=re.IGNORECASE):
            return icon
    return "📘"


def clean_course_title(raw_title: str) -> str:
    """Remove course type (e.g. Vorlesung:) and course code (e.g. 2.02.861)."""
    if not raw_title:
        return ""
    if ":" in raw_title:
        # Split by first colon, take the right part
        _, rest = raw_title.split(":", 1)
        rest = rest.strip()
        # Remove course code if it exists (e.g. "2.02.861 Name")
        match = re.match(r'^[\d\.]+\b\s*(.*)$', rest)
        if match:
            return match.group(1).strip()
        return rest
    return raw_title.strip()


# NOTE: get_calendar_events is defined further below in the Calendar helpers section.



def _get_location_emoji(location: str) -> str:
    """Return emoji based on location."""
    if not location:
        return "📍"
    location_lower = location.lower()

    if any(word in location_lower for word in ['online', 'web', 'virtual', 'zoom', 'teams']):
        return "🌐"
    elif any(word in location_lower for word in ['library', 'bibliothek']):
        return "📚"
    elif any(word in location_lower for word in ['lab', 'labor', 'experiment']):
        return "🔬"
    elif any(word in location_lower for word in ['cafe', 'café', 'restaurant']):
        return "☕"
    elif any(word in location_lower for word in ['sport', 'gym', 'fitness']):
        return "🏃"
    elif 'hörsaal' in location_lower:
        return "🏛️"
    elif any(word in location_lower for word in ['a01', 'a02', 'a03', 'a04', 'a05']):
        return "🏫"
    elif any(word in location_lower for word in ['a14', 'a15']):
        return "🎓"

    return "📍"





async def schedule_reminders(events, chat_id, bot):
    """Schedule silent reminders 1 hour before each event."""
    now = datetime.now()
    for e in events:
        reminder_time = e["start"] - timedelta(hours=1)
        delay = (reminder_time - now).total_seconds()
        if delay <= 0:
            continue  # skip past events

        async def send_reminder(ev=e):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ Reminder:\n"
                    f"“{ev['title']}” starts in 1 hour ({ev['start']:%H:%M})\n"
                    f"📍 {ev['location']}"
                ),
                disable_notification=True,
            )

        asyncio.get_event_loop().call_later(delay, lambda: asyncio.create_task(send_reminder()))


# ───────────────────────────────────────────────
# SHOW LAST 5 MESSAGES (from JSON cache)
# ───────────────────────────────────────────────
from datetime import datetime


async def show_last_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the last 5 messages with full content including message body."""
    query = update.callback_query
    await query.answer()

    # First show "Loading..." message
    await query.edit_message_text("📨 Loading last 5 messages...")

    chat_id = query.message.chat_id

    # Load message cache
    if not os.path.exists(CACHE_FILE):
        await query.edit_message_text("⚠️ No message history available.")
        return

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            all_messages = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load message cache: {e}")
        await query.edit_message_text("⚠️ Error loading message history.")
        return

    if not all_messages:
        await query.edit_message_text("ℹ️ No messages found in history.")
        return

    # Sort messages by date (oldest to newest)
    all_messages_sorted = sorted(all_messages, key=lambda m: parse_date_safe(m.get("date", "")))

    # Get last 5 messages (newest 5 messages)
    last_five = all_messages_sorted[-5:] if len(all_messages_sorted) >= 5 else all_messages_sorted

    # Show header message
    await query.edit_message_text("📨 <b>Last 5 Messages</b>\n━━━━━━━━━━━━━━━━━", parse_mode="HTML")

    # Show messages in correct order (1=old, 5=new)
    for i, msg in enumerate(last_five, 1):
        sender = clean_for_html(msg.get("sender", "Unknown"))
        subject = clean_for_html(msg.get("title", "(No subject)"))
        date = clean_for_html(msg.get("date", "-"))

        # Get message content
        body = msg.get("body", "")
        if not body or "[Error" in body:
            body = "📭 <i>Message content not available</i>"
        else:
            body = clean_for_html(body.strip())
            if len(body) > 1500:  # Truncate if too long
                body = body[:1500] + "...\n\n📄 <i>Message truncated</i>"

        text = (
            f"📨 <b>Message {i}</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"<b>👤 Sender:</b> {sender}\n"
            f"<b>📘 Subject:</b> {subject}\n"
            f"<b>🕒 Date:</b> {date}\n"
            "                   \n"
            f"\n{body}\n"
            "━━━━━━━━━━━━━━━━━"
        )

        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(0.3)  # Rate limiting

    # Show Quick Access button
    await context.bot.send_message(
        chat_id=chat_id,
        text="📌 <b>Quick Access</b>",
        parse_mode="HTML",
        reply_markup=get_show_last_keyboard()
    )


# ──────────────────────────────────────────────────────────────────────────────
#  TELEGRAM UI + MENU + FOLDER VIEW + INSTANT CHECK
# ──────────────────────────────────────────────────────────────────────────────

def _breadcrumb(user_id: int) -> str:
    parts = nav_names.get(user_id, [])
    if not parts:
        return "📂 *Root*"
    if len(parts) == 1:
        return f"📂 *{parts[0]}*"
    else:
        chain = " › ".join(parts)
        return f"📂 *{chain}*"


async def _send_courses_menu(query_or_message, courses):
    """Send main course list as buttons."""
    import html as html_lib
    
    if not courses:
        text = "⚠️ <b>No courses found for the current semester.</b>\nPlease check your semester filter settings on the Stud.IP website."
        keyboard = [[InlineKeyboardButton("🏠 Main Menu", callback_data="show_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return

    # Safety limit to avoid "Reply markup is too long" Telegram error
    MAX_COURSES = 80
    if len(courses) > MAX_COURSES:
        logging.warning(f"Too many courses ({len(courses)}), limiting to {MAX_COURSES}")
        courses = courses[:MAX_COURSES]
        text = f"📚 <b>Select a course (Showing first {MAX_COURSES}):</b>"
    else:
        text = "📚 <b>Select a course to browse its files:</b>"
    
    logging.info(f"Sending courses menu with {len(courses)} buttons.")
    
    keyboard = []
    for name, cid in courses:
        # Avoid extremely long names just in case
        clean_name = html_lib.escape(name[:64])
        keyboard.append([InlineKeyboardButton(f"📘 {clean_name}", callback_data=f"course|{cid}")])



    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await query_or_message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        if "Reply markup is too long" in str(e):
            logging.error(f"FATAL: Keyboard still too long for {len(courses)} courses! {e}")
            # Dynamic fallback: send even fewer
            if len(courses) > 20:
                await _send_courses_menu(query_or_message, courses[:20])
            else:
                if hasattr(query_or_message, "reply_text"):
                    await query_or_message.reply_text("⚠️ Too many courses to display in one menu. Please select a specific semester on the website.")
        else:
            raise e


async def send_folder(query, files, cid, user_id, current_url=None):
    """Display folder content with improved cache handling."""
    keyboard = []

    # Do cache cleanup first
    cleanup_link_cache()

    # Process in single list preserving original order
    for f in files:
        sid = _short_id()

        # Cache folder and file info - CRITICAL FIX
        cache_entry = {
            "cid": cid,
            "name": f["name"],
            "ts": datetime.now().timestamp(),
            "user_id": user_id,
        }

        # URL'yi normalize et ve cache'e kaydet
        if f.get("url"):
            cache_entry["url"] = _normalize_url(f["url"])
            logging.info(f"💾 Caching {f['type']}: {f['name']} -> {cache_entry['url']}")

        link_cache[sid] = cache_entry

        if f["type"] == "folder":
            keyboard.append([InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"folder|{sid}")])
        elif f["type"] == "file":
            meta = f" ({f['size']}, {f['modified']})"
            keyboard.append([InlineKeyboardButton(f"📄 {f['name']}{meta}", callback_data=f"file|{sid}")])

    # Clean cache (remove old entries)
    cleanup_link_cache()

    # Navigation buttons
    nav_btns = []
    stack_len = len(nav_stack.get(user_id, []))

    if stack_len <= 1:
        courses_id = _short_id()
        link_cache[courses_id] = {
            "cid": cid,
            "user_id": user_id,
            "action": "courses",
            "ts": datetime.now().timestamp()
        }
        nav_btns.append(InlineKeyboardButton("📚 All Courses", callback_data=f"nav|{courses_id}"))
    else:
        back_id = _short_id()
        home_id = _short_id()
        courses_id = _short_id()
        now_ts = datetime.now().timestamp()

        link_cache[back_id] = {"cid": cid, "user_id": user_id, "action": "back", "ts": now_ts}
        link_cache[home_id] = {"cid": cid, "user_id": user_id, "action": "home", "ts": now_ts}
        link_cache[courses_id] = {"cid": cid, "user_id": user_id, "action": "courses", "ts": now_ts}

        nav_btns.extend([
            InlineKeyboardButton("⬅️ Back", callback_data=f"nav|{back_id}"),
            InlineKeyboardButton("🏠 Course Root", callback_data=f"nav|{home_id}"),
            InlineKeyboardButton("📚 All Courses", callback_data=f"nav|{courses_id}")
        ])

    keyboard.append(nav_btns)

    # ZIP download button (only if valid URL exists)
    if current_url:
        zip_id = _short_id()
        link_cache[zip_id] = {
            "cid": cid,
            "user_id": user_id,
            "action": "zip",
            "current_url": current_url,
            "ts": datetime.now().timestamp()
        }
        keyboard.append([InlineKeyboardButton("📦 Download Entire Folder (ZIP)", callback_data=f"nav|{zip_id}")])

    # Son cache cleanup
    cleanup_link_cache()

    # Title (breadcrumb)
    title = _breadcrumb(user_id)
    folder_count = len([f for f in files if f["type"] == "folder"])
    file_count = len([f for f in files if f["type"] == "file"])
    caption = f"{title}\n\n📁 *{folder_count} folders* | 📄 *{file_count} files*"

    await query.edit_message_text(
        caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def _normalize_button_text(s: str) -> str:
    # Clean leading/trailing spaces and repeated whitespace
    return " ".join((s or "").strip().split())


async def _run_check_now(update, context):
    """Manual full check: messages → announcements → files → forum posts"""
    chat_id = update.effective_chat.id
    global global_watcher_paused, last_full_check_time

    if chat_id in check_in_progress:
        await update.message.reply_text("⚠️ Another check is already running.")
        return

    try:
        check_in_progress.add(chat_id)
        global_watcher_paused = True  # ✅ Pause global watchers

        # Send a single "Checking..." message
        status_msg = await update.message.reply_text(
            "🔍 Starting full browser-less check...\n\n"
            "📨 Checking messages...\n"
            "📢 Checking announcements...\n"
            "📁 Checking files...\n"
            "💬 Checking forum posts...",
            disable_notification=True
        )

        # ♻️ Ensure session is alive
        if not global_session or not await global_session.is_logged_in():
            await status_msg.edit_text("🔄 Session expired, re-logging...")
            await login_studip()

        # ✅ Load courses ONCE if needed
        if not courses_map:
            await list_courses()

        results = []

        # ✉️ 1. Check messages
        try:
            await status_msg.edit_text("🔍 Checking messages...")
            has_new_messages = await check_new_messages(context.bot, chat_id, silent=True)
            results.append(("messages", has_new_messages))
        except Exception as e:
            results.append(("messages", f"error: {e}"))

        # 📢 2. Check announcements
        try:
            await status_msg.edit_text("🔍 Checking announcements...")
            has_new_announcements = await check_new_announcements_parallel(context.bot, chat_id, silent=True)
            results.append(("announcements", has_new_announcements))
        except Exception as e:
            results.append(("announcements", f"error: {e}"))

        # 📁 3. Check files
        try:
            await status_msg.edit_text("🔍 Checking files...")
            has_new_files = await check_new_files(context.bot, chat_id, silent=True)
            results.append(("files", has_new_files))
        except Exception as e:
            results.append(("files", f"error: {e}"))

        # 💬 4. Check forum
        try:
            await status_msg.edit_text("🔍 Checking forum posts...")
            has_new_forum = await check_new_forum_posts(context.bot, chat_id, silent=True)
            results.append(("forum", has_new_forum))
        except Exception as e:
            results.append(("forum", f"error: {e}"))

        # Summary with Last 5 buttons
        summary = "🔍 Full check completed:\n\n"
        for check, res in results:
            icon = "✅" if res is True or (isinstance(res, list) and len(res) > 0) else ("❌" if "error" in str(res) else "☑️")
            label = {"messages": "Messages", "announcements": "Announcements", "files": "Files", "forum": "Forum"}[check]
            summary += f"{icon} {label}\n"

        await status_msg.edit_text(summary, reply_markup=get_show_last_keyboard())
        last_full_check_time = datetime.now()

    except Exception as e:
        await update.message.reply_text(f"❌ Fatal error: {e}")
        logging.exception("_run_check_now failed")
    finally:
        check_in_progress.discard(chat_id)
        global_watcher_paused = False


# ──────────────────────────────────────────────────────────────────────────────
#  CALLBACK ROUTER + REPLY KEYBOARD + MAIN BOT LOOP
# ──────────────────────────────────────────────────────────────────────────────
# Separate pause control for each watcher:
message_watcher_paused = False
announcement_watcher_paused = False
file_watcher_paused = False


async def handle_status_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "start_watchers":
        global message_watcher_paused, announcement_watcher_paused, file_watcher_paused
        message_watcher_paused = False
        announcement_watcher_paused = False
        file_watcher_paused = False
        await query.edit_message_text("▶️ All watchers resumed.")

    elif query.data == "stop_watchers":
        message_watcher_paused = True
        announcement_watcher_paused = True
        file_watcher_paused = True
        await query.edit_message_text("⏸️ All watchers paused.")

    elif query.data == "request_wa_qr":
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("http://localhost:3838/request_qr") as resp:
                    data = await resp.json()
                    await query.message.reply_text(f"ℹ️ {data.get('message', 'QR request processed.')}")
        except Exception as e:
            logging.error(f"Failed to request WA QR: {e}")
            await query.message.reply_text("❌ WhatsApp service unreachable. Make sure it's running.")

    elif query.data == "change_wa_group":
        from telegram import ForceReply
        await query.message.reply_text("Please type the new WhatsApp group name:", reply_markup=ForceReply(selective=True))

    elif query.data == "change_ical_link":
        from telegram import ForceReply
        await query.message.reply_text("Please type the new iCal link (STUDIP_ICAL_URL):", reply_markup=ForceReply(selective=True))

async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    # ── course selection ────────────────────────────────────────────────
    if data[0] == "course":
        cid = data[1]
        user_id = query.from_user.id
        user_courses[user_id] = cid
        course_name = courses_map.get(cid, f"Course {cid[:6]}")
        root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
        nav_stack[user_id] = [root_url]
        nav_names[user_id] = [course_name]
        files = await list_files(cid, root_url)
        await send_folder(query, files, cid, user_id, current_url=root_url)


    # ── folder open ─────────────────────────────────────────────────────
    elif data[0] == "folder":
        sid = data[1]
        info = link_cache.get(sid, {})
        folder_url = info.get("url")
        cid = info.get("cid")
        folder_name = info.get("name", "Folder")

        if not folder_url or not cid:
            await query.message.reply_text("⚠️ Folder link not found or expired.")
            return

        # URL'yi normalize et
        folder_url = _normalize_url(folder_url)

        # Update cache (refresh TTL)
        link_cache[sid] = {
            **info,
            "ts": datetime.now().timestamp(),
            "url": folder_url  # normalized URL'yi kaydet
        }
        cleanup_link_cache()

        nav_stack.setdefault(user_id, []).append(folder_url)
        nav_names.setdefault(user_id, []).append(folder_name)

        files = await list_files(cid, folder_url)
        await send_folder(query, files, cid, user_id, current_url=folder_url)

    # ── file download ───────────────────────────────────────────────────
    elif data[0] == "file":
        sid = data[1]
        info = link_cache.get(sid, {})
        cid = info.get("cid")
        fname = info.get("name")
        user_id_from_cache = info.get("user_id")

        logging.info(
            f"📥 Download request: sid={sid}, cid={cid}, file={fname}, cache_user={user_id_from_cache}, current_user={user_id}")

        if not cid or not fname:
            await query.message.reply_text("⚠️ Invalid file reference. Cache may have expired.")
            logging.error(f"❌ Invalid file reference: cid={cid}, fname={fname}")
            return

        # Mevcut URL'yi kontrol et (watch fonksiyonundan geliyorsa)
        temp_url = info.get("url")
        current_url = info.get("current_url")

        # Try cached URL first
        if temp_url:
            logging.info(f"🔄 Trying cached URL: {temp_url}")
            try:
                async with await global_session.get(temp_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        if len(content) > 48 * 1024 * 1024:
                            await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
                            return

                        temp_path = os.path.join(tempfile.gettempdir(), fname)
                        with open(temp_path, "wb") as f:
                            f.write(content)

                        await query.message.reply_document(
                            document=open(temp_path, "rb"),
                            filename=fname
                        )
                        os.remove(temp_path)
                        logging.info(f"✅ Downloaded via cached URL: {fname}")

                        # Update cache (refresh TTL)
                        link_cache[sid] = {
                            **info,
                            "ts": datetime.now().timestamp()
                        }
                        return
                    else:
                        logging.warning(f"⚠️ Cached URL returned HTTP {resp.status}, getting fresh URL")
            except Exception as e:
                logging.warning(f"⚠️ Cached URL failed, getting fresh URL: {e}")

        # If cached URL doesn't work, get fresh URL
        await query.edit_message_text(f"🔍 Getting fresh download link for:\n📄 {fname}")

        url = await get_fresh_file_url(cid, fname, current_url)
        if not url:
            await query.message.reply_text(f"⚠️ Could not get download link for:\n{fname}")
            logging.error(f"❌ No download URL found for: {fname}")
            return

        # Save URL to cache (for future use)
        link_cache[sid] = {
            **info,
            "url": url,
            "ts": datetime.now().timestamp(),
            "user_id": user_id  # Save current user
        }
        cleanup_link_cache()

        temp_path = os.path.join(tempfile.gettempdir(), fname)

        try:
            async with await global_session.get(url) as resp:
                if resp.status != 200:
                    await query.message.reply_text(f"⚠️ Download failed (HTTP {resp.status}).")
                    logging.error(f"❌ Download failed: HTTP {resp.status} for {url}")
                    return

                content = await resp.read()
                if len(content) > 48 * 1024 * 1024:
                    await query.message.reply_text("⚠️ File too large for Telegram (>48MB).")
                    return

                with open(temp_path, "wb") as f:
                    f.write(content)

                await query.message.reply_document(
                    document=open(temp_path, "rb"),
                    filename=fname,
                    caption=f"✅ {fname}"
                )
                os.remove(temp_path)
                logging.info(f"✅ Successfully downloaded: {fname}")

        except Exception as e:
            logging.error(f"❌ Download failed for {fname}: {e}")
            await query.message.reply_text(f"⚠️ Download failed:\n{str(e)[:200]}")

    # ── navigation (back, home, courses, zip) ────────────────────────────
    elif data[0] == "nav":
        sid = data[1]
        info = link_cache.get(sid, {})
        action = info.get("action")
        cid = info.get("cid")

        if action == "back":
            if len(nav_stack.get(user_id, [])) > 1:
                nav_stack[user_id].pop()
                nav_names[user_id].pop()

            if nav_stack.get(user_id):
                back_url = nav_stack[user_id][-1]
                files = await list_files(cid, back_url)
                await send_folder(query, files, cid, user_id, current_url=back_url)
            else:
                # Fallback: course root'a git
                root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
                nav_stack[user_id] = [root_url]
                nav_names[user_id] = [courses_map.get(cid, f"Course {cid[:6]}")]
                files = await list_files(cid, root_url)
                await send_folder(query, files, cid, user_id, current_url=root_url)

        elif action == "home":
            root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
            nav_stack[user_id] = [root_url]
            nav_names[user_id] = [courses_map.get(cid, f"Course {cid[:6]}")]
            files = await list_files(cid, root_url)
            await send_folder(query, files, cid, user_id, current_url=root_url)

        elif action == "courses":
            courses = await list_courses()
            await _send_courses_menu(query, courses)

        elif action == "zip":
            current_url = info.get("current_url") or (
                nav_stack.get(user_id, [None])[-1] if nav_stack.get(user_id) else None)
            if not current_url:
                await query.message.reply_text("⚠️ No folder selected for ZIP download.")
                return

            root_name = nav_names.get(user_id, ["course"])[-1]

            msg = await query.message.reply_text("📦 Preparing ZIP... (0%)")

            async def progress(total, current):
                pct = int((current / total) * 100) if total else 100
                try:
                    await msg.edit_text(f"📦 Preparing ZIP... ({pct}%)")
                except Exception:
                    pass

            buf, total, folder_name, success_count, total_size = await create_recursive_zip(
                cid, current_url, root_name=root_name, progress_callback=progress
            )

            if total == 0:
                await msg.edit_text("⚠️ No downloadable files found.")
                return

            zip_name = f"{root_name}.zip"

            def human_size(bytes_val):
                for unit in ["B", "KB", "MB", "GB"]:
                    if bytes_val < 1024:
                        return f"{bytes_val:.1f} {unit}"
                    bytes_val /= 1024
                return f"{bytes_val:.1f} TB"

            now_str = datetime.now().strftime("%d %b %Y %H:%M")
            summary = (
                "━━━━━━━━━━━━━━━━━━\n"
                "✅ <b>ZIP CREATED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Folder:</b> {folder_name}\n"
                f"📄 <b>Files:</b> {success_count}/{total}\n"
                f"💾 <b>Total Size:</b> {human_size(total_size)}\n"
                f"🕒 <b>Created:</b> {now_str}\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            if len(buf.getvalue()) > 48 * 1024 * 1024:
                await msg.edit_text("⚠️ ZIP too large for Telegram (>48MB).")
                await query.message.reply_text(summary, parse_mode="HTML")
                return

            await msg.edit_text(f"✅ {success_count} files added, sending ZIP...")
            await query.message.reply_document(document=buf, filename=zip_name)
            await msg.delete()
            await query.message.reply_text(summary, parse_mode="HTML")
            link_cache.pop(sid, None)

    # ── message full view (📖 Show Full Message) ─────────────────────────
    elif data[0] == "message":
        sid = data[1]
        info = link_cache.get(sid, {})
        msg_id = info.get("message_id")

        if not msg_id:
            await query.message.reply_text("⚠️ Message ID not found.")
            return
        # ── Step 1: fetch planner page to get CSRF token & API URL ───────
        # This block seems misplaced here, as it's about planner/calendar, not messages.
        # Assuming it's intended for debugging or a future feature related to calendar.
        # It requires 'session' and 'index_url' to be defined in this scope.
        # For now, I'm placing it as requested, but it might cause NameError if not defined.
        # async with await session.get(index_url) as r: # Commented out as session/index_url are not defined here.
        #     page_html = await r.text()

        # csrf_token = ""
        # # Log all discovered event/calendar URLs from the HTML
        # found_urls = _re.findall(r"['\"]([^'\"]*calendar[^'\"]*)['\"]", page_html)
        # found_urls += _re.findall(r"['\"]([^'\"]*events[^'\"]*)['\"]", page_html)
        # logging.info("📅 Discovered URLs in planner HTML:")
        # for u in set(found_urls):
        #     logging.info(f"   - {u}")

        # # Also grab any JS lines defining FullCalendar source
        # lines = [line for line in page_html.split('\n') if 'url' in line.lower() or 'events' in line.lower() or 'source' in line.lower()]
        # logging.info("📅 Discovered Source JS lines:")
        # for i, line in enumerate(lines[:15]):
        #     logging.info(f"   L{i}: {line.strip()[:100]}")

        # # STUDIP.CSRF_TOKEN = { name: '...', value: 'TOKEN' }
        # m = _re.search(r"security_token['\"]?\s*[,:]\s*['\"]([A-Za-z0-9+/=_.\-]{10,})['\"]", page_html)
        # if m:
        #     csrf_token = m.group(1)
        #     logging.info(f"📅 CSRF token: {csrf_token[:12]}...")
        # The above block is commented out as it seems to be for a different context (calendar/planner)
        # and would require `session`, `index_url`, and `_re` to be imported/defined.

        try:
            logging.info(f"📨 Opening message ID {msg_id} via direct URL...")

            msg_url = f"https://elearning.uni-oldenburg.de/dispatch.php/messages/read/{msg_id}/rec"
            async with await global_session.get(msg_url) as resp:
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            # Extract information
            subject_tag = soup.select_one("#ui-id-7, .message-subject, h1")
            subject = subject_tag.get_text(strip=True) if subject_tag else "(No subject)"

            # Try to find elements by labels
            sender_name = "Unknown"
            date_str = "-"
            for row in soup.select("tr"):
                th = row.select_one("th, td:first-child")
                td = row.select_one("td")
                if th and td:
                    label = th.get_text(strip=True).lower()
                    if "from" in label or "von" in label:
                        sender_name = td.get_text(strip=True)
                    elif "date" in label or "datum" in label:
                        date_str = td.get_text(strip=True)

            body_container = soup.select_one("div.formatted-content, .message-content, .formatted-content.ck-content")
            content = body_container.get_text("\n", strip=True) if body_container else "No content found."

            # Send to Telegram
            header = (
                "✉️ <b>FULL MESSAGE</b>\n"
                "                  \n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📬 <b>Subject:</b> {subject}\n"
                f"👤 <b>From:</b> {sender_name}\n"
                f"📅 <b>Date:</b> {date_str}\n"
                "━━━━━━━━━━━━━━━━━━\n"
            )

            chunks = [content[i:i + 4000] for i in range(0, len(content), 4000)]
            if chunks:
                await query.message.reply_text(f"{header}{chunks[0]}", parse_mode="HTML")
                for part in chunks[1:]:
                    await query.message.reply_text(part, parse_mode="HTML")
            else:
                await query.message.reply_text(f"{header}No message content found.", parse_mode="HTML")

            logging.info(f"✅ Message {msg_id} successfully opened directly and sent to Telegram.")

        except Exception as e:
            logging.error(f"Message read failed: {e}")
            await query.message.reply_text(f"⚠️ Could not load message content:\n{str(e)[:200]}")

    # ── show last items ─────────────────────────────────────────────────
    elif data[0] == "show_last":
        action = data[1]
        if action == "messages":
            await show_last_messages(update, context)
        elif action == "announcements":
            await show_last_announcements(update, context)
        elif action == "files":
            await show_last_files(update, context)

    # ── watcher control ─────────────────────────────────────────────────
    elif data[0] in ["start_watchers", "stop_watchers"]:
        await handle_status_buttons(update, context)

    # ── unknown callback ────────────────────────────────────────────────
    else:
        logging.warning(f"Unknown callback data: {query.data}")
        await query.message.reply_text("⚠️ Unknown action.")


# ── reply keyboard handling ────────────────────────────────────────────────
async def handle_settings_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return
    text = update.message.reply_to_message.text
    if "Please type the new WhatsApp group name:" in text:
        new_group = update.message.text.strip()
        import dotenv
        import os
        env_path = ".env"
        dotenv.set_key(env_path, "WHATSAPP_GROUP_NAME", new_group)
        os.environ["WHATSAPP_GROUP_NAME"] = new_group
        await update.message.reply_text(f"✅ WhatsApp group successfully updated: {new_group}")

    elif "Please type the new iCal link" in text:
        new_link = update.message.text.strip()
        import dotenv
        import os
        env_path = ".env"
        dotenv.set_key(env_path, "STUDIP_ICAL_URL", new_link)
        os.environ["STUDIP_ICAL_URL"] = new_link
        await update.message.reply_text(f"✅ iCal link successfully updated!")


async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Support both keyboard and inline callback
    query = update.callback_query
    message = update.message
    data = None

    if query:
        await query.answer()
        data = query.data.split("|")
        chat_id = query.message.chat_id
        sender = query
    else:
        text = (message.text or "").strip()
        # Check all buttons
        if "🍽️" in text or "menu" in text.lower():
            data = ["menu"]
        elif "📅" in text or "calendar" in text.lower():
            data = ["calendar"]
        elif "▶️" in text or "start" in text.lower():
            data = ["start"]
        elif "🔁" in text or "check" in text.lower():
            data = ["check"]
        elif "ℹ️" in text or "status" in text.lower():
            data = ["status"]
        else:
            data = [text.replace("📅", "").strip()] # Fallback for other text commands
        chat_id = message.chat_id
        sender = message

    global watcher_controller_running, global_watcher_paused, check_in_progress
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await sender.reply_text("Not authorized to use this bot.")
        return

    try:
        # Temporary loading message
        status_msg = await sender.reply_text("⏳ Please wait...")

        if data[0] == "menu":
            await menu_command(update, context)
        elif data[0] == "calendar":
            logging.info("📅 Fetching today's schedule...")
            today = datetime.now().date()
            week_start = today - timedelta(days=today.weekday())
            events = await get_calendar_events(session=global_session, week_start=week_start)
            await send_daily_calendar(sender, events, today, week_start)
        elif data[0] == "start":
            await start(update, context)
        elif data[0] == "check":
            await check_command(update, context)
        elif data[0] == "status":
            await status_command(update, context)
        else:
            await sender.reply_text("❓ Unknown command.")

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception:
            pass
    except Exception as e:
        logging.error(f"Button handler error: {e}")
        try:
            await sender.reply_text(f"⚠️ An error occurred:\n`{e}`", parse_mode="Markdown")
        except Exception:
            pass


# ── Calendar helpers ──────────────────────────────────────────────────────────

async def get_calendar_events(session, week_start) -> list:
    """Fetch calendar events from the user's personal Stud.IP iCal export URL."""
    ical_url = os.getenv("STUDIP_ICAL_URL")
    
    if not ical_url:
        logging.error("❌ No STUDIP_ICAL_URL set in environment variables.")
        return []
        
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        logging.error("❌ icalendar or recurring_ical_events not installed. Run: pip install icalendar recurring-ical-events")
        return []

    try:
        logging.info(f"📅 Fetching ICS Calendar from: {ical_url[:50]}...")
        async with await session.get(ical_url, allow_redirects=True) as r:
            if r.status != 200:
                logging.warning(f"ICS Calendar API returned {r.status}")
                return []
            raw_data = await r.read()
            raw_text = raw_data.decode('utf-8', errors='ignore')

        # Parse the calendar
        cal = icalendar.Calendar.from_ical(raw_text)
        
        # Get events for the requested week
        end_date = week_start + timedelta(days=7)
        # Use recurring_ical_events to expand RRULEs automatically and filter by date boundary
        week_events = recurring_ical_events.of(cal).between(week_start, end_date)
        
        events = []
        now = datetime.now(ZoneInfo("Europe/Berlin"))
        for event in week_events:
            title = str(event.get('SUMMARY', 'Unknown Event'))
            location = str(event.get('LOCATION', ''))
            url_link = str(event.get('URL', ''))
            
            start_dt = event['DTSTART'].dt
            end_dt = event['DTEND'].dt
            
            # icalendar returns date or datetime depending on whether it's an all-day event
            if not isinstance(start_dt, datetime):
                # Convert purely 'date' to 'datetime' at midnight
                start_dt = datetime.combine(start_dt, datetime.min.time()).replace(tzinfo=ZoneInfo("Europe/Berlin"))
            if not isinstance(end_dt, datetime):
                end_dt = datetime.combine(end_dt, datetime.min.time()).replace(tzinfo=ZoneInfo("Europe/Berlin"))
            
            # Ensure timezone-aware comparisons by converting all to local time
            try:
                start_dt = start_dt.astimezone(ZoneInfo("Europe/Berlin"))
                end_dt = end_dt.astimezone(ZoneInfo("Europe/Berlin"))
            except ValueError:
                # If naive, just replace tzinfo
                start_dt = start_dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                end_dt = end_dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
            
            # Determine status
            if end_dt < now:
                status = "past"
            elif start_dt <= now <= end_dt:
                status = "ongoing"
            else:
                status = "upcoming"
                
            day_str = start_dt.strftime("%A, %d.%m.%Y")
            time_str = f"{start_dt.strftime('%H:%M')} – {end_dt.strftime('%H:%M')}"
            date_key = start_dt.date()
                
            events.append({
                "title": title,
                "day": day_str,
                "time": time_str,
                "location": location,
                "url": url_link,
                "date_key": date_key,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "status": status
            })

        # Sort by date + time using the real datetime object
        events.sort(key=lambda e: e["start_dt"])
        return events
    except Exception as exc:
        logging.error(f"get_calendar_events error: {exc}")
        return []


async def send_daily_calendar(sender, events: list, target_date, week_start):
    """Send a specific day's schedule with a 'Week Plan' button. Matches backup UI."""
    today_events = [ev for ev in events if ev["date_key"] == target_date]
    
    if not today_events:
        text = (
            "🎉 *No Events Today!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🗓️ *{target_date:%A, %d %B %Y}*"
        )
    else:
        # Header
        header = (
            "✨ *Today's Schedule* ✨\n"
            f"📅 *{target_date:%A, %d %B %Y}*\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        
        lines = [header]
        course_blocks = []
        for ev in today_events:
            original_title = ev.get("title", "")
            status = _status_emoji(ev.get("status"))
            course_icon = _get_course_icon(original_title)
            display_title = clean_course_title(original_title)
            loc_icon = _get_location_emoji(ev.get("location", ""))
            duration = ev["time"] # Already formatted as HH:MM – HH:MM

            block = (
                f"{status} {course_icon} *{display_title}*\n"
                f"   🕒 `{duration}`\n"
                f"   {loc_icon} {_safe_loc(ev['location'])}"
            )
            course_blocks.append(block)
            
        lines.append("\n\n".join(course_blocks))
        lines.append("━━━━━━━━━━━━━━━━━━")
        text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("📆 Week Plan", callback_data="calendar_weekly"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(sender, "edit_message_text"):
        try:
            await sender.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except Exception:
            pass
    await sender.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def send_weekly_calendar(sender, events: list, week_start):
    """Send the weekly schedule with navigation. Matches backup UI."""
    prev_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)
    week_end = week_start + timedelta(days=6)

    if not events:
        text = (
            "🎉 *No Events Planed!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🗓️ *Weekly View*"
        )
    else:
        # Group events by date_key
        events_by_day = {}
        for event in events:
            dk = event["date_key"]
            if dk not in events_by_day:
                events_by_day[dk] = []
            events_by_day[dk].append(event)

        # Header
        header = (
            "✨ *Weekly Schedule* ✨\n"
            f"🗓️ *{week_start:%d %B} - {week_end:%d %B %Y}*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        lines = [header]

        # Process days in order
        for dk in sorted(events_by_day.keys()):
            now_dt = datetime.combine(dk, datetime.min.time())
            day_name = now_dt.strftime("%A")
            day_emoji = _get_day_emoji(day_name)
            
            lines.append(f"\n{day_emoji} *{day_name}, {dk:%d.%m.%Y}*")
            lines.append("━━━━━━━━━━━━━━━━━━")

            day_courses = []
            for event in events_by_day[dk]:
                original_title = event.get("title", "")
                status = _status_emoji(event.get("status"))
                course_icon = _get_course_icon(original_title)
                display_title = clean_course_title(original_title)
                loc_icon = _get_location_emoji(event.get("location", ""))
                duration = event["time"]

                block = (
                    f"{status} {course_icon} *{display_title}*\n"
                    f"   🕒 `{duration}`\n"
                    f"   {loc_icon} {_safe_loc(event['location'])}"
                )
                day_courses.append(block)
            
            lines.append("\n\n".join(day_courses))

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        text = "\n".join(lines)

    # Navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("⬅️ Prev Week", callback_data=f"calendar_week|{prev_week.isoformat()}"),
            InlineKeyboardButton("🗓️ This Week", callback_data=f"calendar_week|{(datetime.now().date() - timedelta(days=datetime.now().weekday())).isoformat()}"),
            InlineKeyboardButton("➡️ Next Week", callback_data=f"calendar_week|{next_week.isoformat()}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(sender, "edit_message_text"):
        try:
            await sender.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
            return
        except Exception:
            pass
    await sender.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_calendar_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_week|YYYY-MM-DD navigation callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        _, date_str = query.data.split("|")
        week_start = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())

    try:
        await query.edit_message_text("⏳ Loading schedule...")
        session = global_session
        events = await get_calendar_events(session=session, week_start=week_start)
        await send_weekly_calendar(query, events, week_start)
    except Exception as e:
        logging.error(f"Calendar week nav error: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)[:200]}")


async def handle_calendar_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_today callback — shows the current day's events."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        await query.edit_message_text("📅 Fetching today's events...")
        session = global_session
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=session, week_start=week_start)
        await send_daily_calendar(query, events, today, week_start)
    except Exception as e:
        logging.error(f"Calendar today error: {e}")
        await query.edit_message_text(f"❌ Error loading today's events: {str(e)[:200]}")


async def handle_calendar_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_weekly callback — shows full week."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    try:
        await query.edit_message_text("🗓️ Fetching weekly schedule...")
        session = global_session
        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        events = await get_calendar_events(session=session, week_start=week_start)
        await send_weekly_calendar(query, events, week_start)
    except Exception as e:
        logging.error(f"Calendar weekly error: {e}")
        await query.edit_message_text(f"❌ Error loading weekly schedule: {str(e)[:200]}")


async def delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete any free text that isn't a valid keyboard command."""
    try:
        if not update.message or not update.message.text:
            return
        text = _normalize_button_text(update.message.text)
        if text.startswith(("▶️", "🔁", "ℹ️", "🍽️", "📅")):
            return
        await update.message.delete()
    except Exception:
        pass


from telegram import ReplyKeyboardMarkup


def get_main_keyboard():
    """Update main reply keyboard with menu button"""
    return ReplyKeyboardMarkup(
        [
            ["▶️ Start", "🔁 Check", "ℹ️ Status"],
            ["🍽️ Menu", "📅 Calendar"]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )


# ── /start command ────────────────────────────────────────────────────────────
# ── /start command ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return

    # Send keyboard IMMEDIATELY - not conditional on anything
    welcome_msg = await update.message.reply_text(
        "🤖 Welcome to Stud.IP Bot!\n\n"
        "▶️ Start - Browse courses\n"
        "🔁 Check - Manual check\n"
        "ℹ️ Status - Bot status\n"
        "🍽️ Menu - Today's meals\n"
        "📅 Calendar - Today's schedule\n",
        reply_markup=get_main_keyboard()
    )

    # Then do other operations
    try:
        courses = await list_courses()
        # Keep keyboard when sending courses
        await _send_courses_menu(update.message, courses)
    except Exception as e:
        logging.warning(f"Course loading failed but keyboard should be visible: {e}")


# ── /check and /watch commands ───────────────────────────────────────────────
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and not is_user_allowed(update.effective_user.id):
        return
    await _run_check_now(update, context)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return
    if chat_id in watch_tasks and not watch_tasks[chat_id].done():
        await update.message.reply_text("Watcher is already running.")
        return
    await update.message.reply_text("👀 Watcher started (checks every 2 hours).", disable_notification=True)
    task = asyncio.create_task(watch_loop(chat_id, context))
    watch_tasks[chat_id] = task


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed bot status"""
    global watcher_controller_running, global_watcher_paused, check_in_progress

    logged_in = "✅ Yes" if global_session and await global_session.is_logged_in() else "❌ No"
    browser_ready = "✅ Browser-less"

    # Watcher durumu
    if watcher_controller_running:
        watcher_status = "🟢 Running"
    else:
        watcher_status = "🔴 Stopped"

    global_paused = "✅ Yes" if global_watcher_paused else "❌ No"
    active_check = "✅ Yes" if check_in_progress else "❌ No"

    text = (
        "━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🔑 Logged in: {logged_in}\n"
        f"🌐 Browser Context: {browser_ready}\n"
        f"👀 Unified Watcher: {watcher_status}\n"
        f"⏸️ Global Paused: {global_paused}\n"
        f"🔁 Active Check: {active_check}"
    )

    # 💾 System resource information
    process = psutil.Process()
    with process.oneshot():
        mem_mb = process.memory_info().rss / 1024 ** 2
        cpu_percent = process.cpu_percent(interval=0.3)

    total_mem = psutil.virtual_memory()
    sys_cpu = psutil.cpu_percent(interval=0.3)

    sys_info = (
        f"💾 <b>Bot Memory:</b> {mem_mb:.1f} MB\n"
        f"💽 <b>System RAM:</b> {total_mem.percent:.1f}%\n"
        f"🧠 <b>System CPU:</b> {sys_cpu:.1f}%"
    )

    # High RAM warning
    if mem_mb > 500:
        sys_info += "\n\n⚠️ <b>High memory usage detected — browser restart recommended.</b>"

    # Last full check time
    global last_full_check_time
    if "last_full_check_time" in globals() and last_full_check_time:
        since = datetime.now() - last_full_check_time
        hours, remainder = divmod(int(since.total_seconds()), 3600)
        minutes = remainder // 60
        text += f"\n🕓 Last Full Check: {last_full_check_time.strftime('%d %b %Y %H:%M')} ({hours}h {minutes}m ago)"
    else:
        text += "\n🕓 Last Full Check: —"

    # Combine and send
    text += f"\n\n{sys_info}"

    keyboard = [
        [InlineKeyboardButton("📲 Request WA QR", callback_data="request_wa_qr")],
        [InlineKeyboardButton("✏️ Change WA Group", callback_data="change_wa_group")],
        [InlineKeyboardButton("📅 Change iCal Link", callback_data="change_ical_link")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


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
        session = await login_studip()
        courses = await list_courses()

        # DELETE TEMPORARY MESSAGE!
        await temp_message.delete()

        # Show course menu
        await _send_courses_menu(message, courses)

    except Exception as e:
        if temp_message:
            try:
                await temp_message.delete()
            except:
                pass
        await message.reply_text(f"❌ Error: {str(e)}")

    finally:
        if chat_id in start_in_progress:
            start_in_progress.discard(chat_id)


# ── MAIN ──────────────────────────────────────────────────────────────────────


async def main():
    logging.info("Telegram bot running (100% browser-less Stud.IP automation)...")

    if not acquire_instance_lock():
        logging.error("❌ Another bot instance is already running. Exiting.")
        return

    app = None
    try:
        global global_session
        global_session = StudIPSession(USERNAME, PASSWORD, TOTP_SECRET)
        
        logging.info("🔐 Logging in to Stud.IP...")
        if not await global_session.login():
            logging.error("❌ Login failed. Please check your USERNAME, PASSWORD, and TOTP_SECRET.")
            return

        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Command & Callback handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("check", check_command))
        app.add_handler(CommandHandler("watch", watch))
        app.add_handler(CommandHandler("status", status_command))
        app.add_handler(CommandHandler("menu", menu_command))
        
        app.add_handler(CallbackQueryHandler(show_last_messages, pattern="^show_last_messages$"))
        app.add_handler(CallbackQueryHandler(forward_to_whatsapp, pattern="^forward_wa$"))
        app.add_handler(CallbackQueryHandler(show_last_announcements, pattern="^show_last_announcements$"))
        app.add_handler(CallbackQueryHandler(show_last_files, pattern="^show_last_files$"))
        app.add_handler(CallbackQueryHandler(show_last_forum_posts, pattern="^show_last_forum_posts$"))
        app.add_handler(CallbackQueryHandler(handle_status_buttons, pattern="^(start_watchers|stop_watchers|request_wa_qr|change_wa_group|change_ical_link)$"))
        app.add_handler(CallbackQueryHandler(handle_calendar_today, pattern="^calendar_today$"))
        app.add_handler(CallbackQueryHandler(handle_calendar_weekly, pattern="^calendar_weekly$"))
        app.add_handler(CallbackQueryHandler(handle_calendar_week, pattern="^calendar_week\|.*$"))
        app.add_handler(CallbackQueryHandler(menu_button_handler, pattern="^menu_nav\|.*$"))
        app.add_handler(CallbackQueryHandler(handle_selection))

        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_settings_reply))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_buttons))

        # Explicitly initialize and start the application
        await app.initialize()
        await app.start()
        
        logging.info("🚀 Starting unified watcher...")
        # Run watcher in background task
        asyncio.create_task(start_unified_watcher(app))

        logging.info("🤖 Starting Telegram bot polling...")
        await app.updater.start_polling(drop_pending_updates=True)

        logging.info("💬 Bot is now online and listening for messages.")

        # Run until the process is stopped
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logging.info("🛑 Stop signal received.")

    except Exception as e:
        logging.error(f"❌ Fatal error in main: {e}", exc_info=True)
    finally:
        logging.info("🧹 Cleaning up...")
        if app:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as se:
                logging.error(f"Error during app shutdown: {se}")
            
        if global_session:
            await global_session.close()
        
        release_instance_lock()
        logging.info("✅ Bot shutdown complete")

if __name__ == "__main__":
    import asyncio
    import subprocess
    import atexit
    import os
    import sys
    
    # Start WhatsApp microservice automatically
    wa_service_dir = os.path.join(os.path.dirname(__file__), "whatsapp_service")
    if os.path.exists(wa_service_dir):
        logging.info("Starting WhatsApp microservice...")
        wa_process = subprocess.Popen("npm start", shell=True, cwd=wa_service_dir)
        
        def cleanup_wa():
            logging.info("Stopping WhatsApp microservice...")
            wa_process.terminate()
            
        atexit.register(cleanup_wa)

    asyncio.run(main())
