import asyncio
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import pyotp
import uuid
import hashlib
from urllib.parse import unquote
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
from datetime import datetime, timedelta
import nest_asyncio
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo
from telegram.constants import ChatAction
from asyncio import CancelledError

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
global_scan_count = 0
FILE_WATCHER_INTERVAL = 2 * 60 * 60
# ── global runtime state ───────────────────────────────────────────────────────
playwright = None
browser = None
browser_context = None
page = None
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
BROWSER_RESTART_INTERVAL = 4 * 60 * 60
check_in_progress: set[int] = set()  # chat_id set to prevent concurrent checks

# Concurrency locks
page_lock = asyncio.Lock()  # All Playwright navigations pass through this
cache_lock = asyncio.Lock()  # course_cache.json read/write serialization


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


def clean_html_text(soup_element) -> str:
    """Safely extracts text from HTML by converting block elements to newlines, but keeping inline elements together."""
    if not soup_element:
        return ""
    
    for br in soup_element.find_all("br"):
        br.replace_with("\n")
    for p in soup_element.find_all(["p", "div"]):
        p.insert_after("\n\n")
    for li in soup_element.find_all("li"):
        li.insert_before("- ")
        li.insert_after("\n")
    for heading in soup_element.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading.insert_after("\n\n")

    text = soup_element.get_text()
    
    # Clean up excessive newlines
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
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


# ── logging setup ──────────────────────────────────────────────────────────────
LOG_FILE = "watch_log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_file_handler = logging.FileHandler(LOG_FILE)
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
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            logging.error("Another bot instance is already running (lock file exists).")
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
    # normalize schema slashes
    u = re.sub(r"^https:/*", "https://", u.strip())
    # fix double host concatenations
    if "https://elearning.uni-oldenburg.dehttps://" in u:
        u = u.split("https://elearning.uni-oldenburg.de")[-1]
        if not u.startswith("https://"):
            u = "https://elearning.uni-oldenburg.de" + u
    # prefix host if relative
    if not u.startswith("http://") and not u.startswith("https://"):
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
    global playwright, browser, browser_context, page
    if page and not page.is_closed():
        return page

    # Validate env
    for key, val in {"USERNAME": USERNAME, "PASSWORD": PASSWORD, "TOTP_SECRET": TOTP_SECRET}.items():
        if not val:
            raise RuntimeError(f"Missing required environment variable: {key}")

    playwright = await async_playwright().start()
    headless = os.getenv("HEADLESS", "false").lower() != "false"
    browser = await playwright.chromium.launch(headless=headless)
    browser_context = await browser.new_context()
    page = await browser_context.new_page()

    async def _retry(step_name, coro, retries=3):
        last_err = None
        for attempt in range(retries):
            try:
                return await coro()
            except Exception as e:
                last_err = e
                logging.warning(f"Retry {step_name} ({attempt + 1}/{retries}): {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err or RuntimeError(f"Failed at step: {step_name}")

    if notify:
        await notify("Starting login...")
    async with page_lock:
        await _retry("goto", lambda: page.goto(STUDIP_URL, wait_until="domcontentloaded"))

        # 1. Click Start Login
        await _retry("click_start_wait", lambda: page.wait_for_selector(SELECTORS['start'], timeout=15000))
        if notify:
            await notify("Opening IdP...")
        await page.locator(SELECTORS['start']).first.click()

        # 2. Username Step
        await _retry("user_field", lambda: page.wait_for_selector(SELECTORS['user'], timeout=15000))
        if notify:
            await notify("Submitting username...")
        await page.locator(SELECTORS['user']).fill(USERNAME)
        await page.locator(SELECTORS['user_btn']).click()

        # 3. Password Step
        await _retry("pass_field", lambda: page.wait_for_selector(SELECTORS['pass'], timeout=15000))
        if notify:
            await notify("Submitting password...")
        # Use first matching element for pass (might be dynamic ID)
        await page.locator(SELECTORS['pass']).first.fill(PASSWORD)
        await page.locator(SELECTORS['pass_btn']).click()

        # 4. OTP Step
        await _retry("otp_field", lambda: page.wait_for_selector(SELECTORS['otp'], timeout=15000))
        if notify:
            await notify("Submitting one-time code...")
        await page.locator(SELECTORS['otp']).first.fill(pyotp.TOTP(TOTP_SECRET).now())
        await page.locator(SELECTORS['otp_btn']).click()

        await page.wait_for_load_state("networkidle")
    if notify:
        await notify("Login successful.")
    logging.info("Logged in successfully (persistent session).")
    return page


# ── unified watcher controller ─────────────────────────────────────────────────

async def unified_watcher_controller(app):
    """Centrally manage all watchers - with a single task"""
    global watcher_controller_running, global_watcher_paused, page
    global global_scan_count, last_full_check_time

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

            # Page check
            if not page or page.is_closed():
                logging.warning("📄 Page not available, waiting...")
                await asyncio.sleep(60)
                continue

            cycle_count += 1
            logging.info(f"🔄 Watcher cycle #{cycle_count} started")

            # 0️⃣ CALENDAR REMINDERS - her cycle'da
            try:
                await check_calendar_reminders(page, app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Calendar reminder check failed: {e}")

            # 1️⃣ MESSAGE CHECK - every cycle (≈2.5 minutes)
            try:
                logging.info("📨 Checking messages...")
                await check_new_messages(page, app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Message check failed: {e}")

            await asyncio.sleep(30)  # 30 saniye bekle

            # 2️⃣ ANNOUNCEMENT CHECK - every cycle (≈3 minutes)
            try:
                logging.info("📢 Checking announcements...")
                await check_new_announcements(page, app.bot, admin_id, silent=True)
            except Exception as e:
                logging.error(f"❌ Announcement check failed: {e}")

            await asyncio.sleep(30)  # 30 saniye bekle

            # 3️⃣ FILE CHECK - every 6th cycle (≈15 minutes)
            if cycle_count % 2 == 0:
                try:
                    logging.info("📁 Checking files...")
                    await check_new_files(page, app.bot, admin_id, silent=True)
                except Exception as e:
                    logging.error(f"❌ File check failed: {e}")
                cycle_count = 0  # Reset cycle counter

            # 4️⃣ FORUM CHECK - every 3rd cycle (≈7-8 minutes)
            if cycle_count % 3 == 0:
                try:
                    logging.info("💬 Checking forum posts...")
                    await check_new_forum_posts_parallel(page, app.bot, admin_id, silent=True)
                except Exception as e:
                    logging.error(f"❌ Forum check failed: {e}")

            logging.info(f"✅ Watcher cycle #{cycle_count} completed")
            global_scan_count += 1
            last_full_check_time = datetime.now()

            # Main wait - total cycle duration ≈2.5-3 minutes
            await asyncio.sleep(90)

        except Exception as e:
            logging.error(f"❌ Watcher controller error: {e}")
            await asyncio.sleep(120)  # Hata durumunda 2 dakika bekle


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
async def list_courses(page):
    for attempt in range(1, 4):
        url = page.url
        title = await page.title()
        logging.info(f"Course extraction attempt {attempt}/3 at {url} ('{title}')...")
        
        # Settle/Wait for content
        if attempt == 1:
            await asyncio.sleep(1.5)
        else:
            await asyncio.sleep(2.5)
            
        # Wait for potential table rendering
        try:
            await page.wait_for_selector("table.mycourses, .empty-courses-message", timeout=2000)
        except:
            pass
            
        html = await page.content()
        courses = []
        
        # --- Method 1: JSON-based Vuex state ---
        json_scripts = re.findall(r'<script\s+type="application/json"[^>]*>(.*?)</script>', html, re.S)
        for script_content in json_scripts:
            try:
                content = script_content.strip()
                if 'JSON.parse(' in content:
                    inner_match = re.search(r'JSON\.parse\((.*)\)', content, re.S)
                    if inner_match:
                        inner_content = inner_match.group(1).strip()
                        if (inner_content.startswith('"') and inner_content.endswith('"')) or \
                           (inner_content.startswith("'") and inner_content.endswith("'")):
                            json_str = json.loads(inner_content)
                            full_data = json.loads(json_str)
                        else:
                            full_data = json.loads(inner_content)
                    else:
                        continue
                else:
                    full_data = json.loads(content)

                mycourses_state = None
                if isinstance(full_data, dict):
                    if "vuexStoreData" in full_data and "mycourses" in full_data["vuexStoreData"]:
                        mycourses_state = full_data["vuexStoreData"]["mycourses"]
                    elif "state" in full_data and "mycourses" in full_data["state"]:
                        mycourses_state = full_data["state"]["mycourses"]
                    elif "mycourses" in full_data:
                        mycourses_state = full_data["mycourses"]

                if mycourses_state and isinstance(mycourses_state, dict):
                    courses_data = mycourses_state.get("setCourses", {})
                    if courses_data:
                        selected_semester = mycourses_state.get("selectedSemester")
                        semester_courses = mycourses_state.get("semesterCourses", {})
                        
                        # Apply Filtering
                        if selected_semester and selected_semester in semester_courses:
                            ordered_cids = semester_courses[selected_semester]
                            if isinstance(ordered_cids, list):
                                for cid in ordered_cids:
                                    info = courses_data.get(cid)
                                    if info:
                                        name = info.get("name", "").strip()
                                        if name:
                                            courses.append((name, cid))
                        
                        if not courses: # Fallback to all in state
                            for cid, info in courses_data.items():
                                name = info.get("name", "").strip()
                                if name:
                                    courses.append((name, cid))
                        
                        if courses:
                            logging.info(f"Extracted {len(courses)} courses via JSON on attempt {attempt}.")
                            for name, cid in courses:
                                courses_map[cid] = name
                            return courses
            except Exception:
                continue

        # --- Method 2: DOM-based Scraper (Reliable for visible content) ---
        try:
            course_rows = await page.query_selector_all("table.mycourses tr[data-course-id]")
            if course_rows:
                dom_courses = []
                for row in course_rows:
                    cid = await row.get_attribute("data-course-id")
                    # Name is usually in the 4th column or has a specific class/link
                    name_link = await row.query_selector("td:nth-child(4) a, td.name a, a[href*='course_id=']")
                    if name_link:
                        name = await name_link.inner_text()
                        name = name.strip()
                        if name and cid:
                            dom_courses.append((name, cid))
                
                if dom_courses:
                    logging.info(f"Extracted {len(dom_courses)} courses via DOM scraping on attempt {attempt}.")
                    for name, cid in dom_courses:
                        courses_map[cid] = name
                    return dom_courses
        except Exception as e:
            logging.debug(f"DOM scraping error: {e}")

        # --- Method 3: window.STUDIP.MyCoursesData fallback ---
        m = re.search(r"window\.STUDIP\.MyCoursesData\s*=\s*(\{.*?\});", html, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                fallback_courses = []
                for cid, info in data.get("courses", {}).items():
                    name = info.get("name", "").strip()
                    if name:
                        fallback_courses.append((name, cid))
                if fallback_courses:
                    logging.info(f"Extracted {len(fallback_courses)} courses via window fallback on attempt {attempt}.")
                    for name, cid in fallback_courses:
                        courses_map[cid] = name
                    return fallback_courses
            except Exception:
                pass

    logging.error("CRITICAL: Failed to extract courses after all attempts.")
    # Diagnostic snippet of body
    try:
        body_text = await page.inner_text("body")
        logging.debug(f"Page body snippet: {body_text[:500]}...")
    except:
        pass
        
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


async def get_todays_menu_enhanced(page):
    """Enhanced menu fetching with dynamic allergen guide - HTML format"""
    try:
        menu_url = "https://elearning.uni-oldenburg.de/plugins.php/mensawidget/menu/2/"

        # Fetch menu page with increased timeout for reliability
        async with page_lock:
            await page.goto(menu_url, wait_until="domcontentloaded", timeout=15000)
            html_content = await page.content()

        soup = BeautifulSoup(html_content, "html.parser")

        # Date
        date_element = soup.find('h2')
        date_text = date_element.get_text(strip=True) if date_element else datetime.now().strftime("%d.%m.%Y")

        menu_text = f"🍽️ <b>{html.escape(date_text)}</b> 🍽️\n"
        menu_text += "🏛️ Mensa Uni Oldenburg\n\n"

        # Process all categories and collect ALL allergens used today
        categories = soup.find_all('table', class_='default')
        all_allergens_used = set()

        for category_table in categories:
            category_name = category_table.find('th').get_text(strip=True)

            # Map category names to display names
            category_map = {
                "Main Dishes": "🍴 COUNTER 1",
                "Soup": "🍲 SOUPS",
                "Side Dishes": "🥗 SIDE DISHES",
                "Salads": "🥗 SALADS",
                "Desserts": "🍮 DESSERTS",
                "COUNTER ONE": "🍴 COUNTER 1",
                "COUNTER THREE": "🍴 COUNTER 3",
                "COUNTER FOUR": "🍴 COUNTER 4",
                "Culinarium Main Dishes": "👨‍🍳 CULINARIUM",
                "Culinarium Side Dishes": "👨‍🍳 CULINARIUM",
                "Culinarium Salads": "👨‍🍳 CULINARIUM",
                "Culinarium Desserts": "👨‍🍳 CULINARIUM"
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

            display_name = category_map.get(category_name, f"🍴 {category_name}")

            # Special pizza category
            if category_name == "PIZZA":
                display_name = "🍕 PIZZA"

            menu_text += "━━━━━━━━━━━━━━━━━━\n"
            menu_text += f"{display_name}\n"
            menu_text += "━━━━━━━━━━━━━━━━━━\n"

            items = category_table.find_all('tr')[1:]  # Skip header row

            for item in items:
                cols = item.find_all('td')
                if len(cols) >= 2:
                    name_cell = cols[0]
                    price = cols[1].get_text(strip=True)

                    # Extract name without allergens
                    name_text = ""
                    description = ""
                    allergens_text = ""

                    # Clone the cell to work with
                    temp_cell = BeautifulSoup(str(name_cell), 'html.parser')

                    # Remove allergen spans first
                    allergen_spans = temp_cell.find_all('span', class_='attributes')
                    for span in allergen_spans:
                        allergens_text = span.get_text(strip=True)
                        # Add allergens to the used set
                        if allergens_text:
                            # Split by comma and add individual allergens
                            allergens_list = [a.strip() for a in allergens_text.split(',')]
                            all_allergens_used.update(allergens_list)
                        span.decompose()

                    # Remove allergen abbr tags
                    allergen_abbrs = temp_cell.find_all('abbr')
                    for abbr in allergen_abbrs:
                        abbr.decompose()

                    # Now get clean text
                    clean_text = temp_cell.get_text("\n", strip=True)

                    # Split by newline to separate name and description
                    lines = [line.strip() for line in clean_text.split('\n') if line.strip()]

                    if lines:
                        name_text = lines[0]
                        if len(lines) > 1:
                            description = ' '.join(lines[1:])

                    # Clean up any remaining allergen codes in name
                    name_text = re.sub(r'[1-9][0-9]*[A-Za-z,+\s]*$', '', name_text).strip()
                    name_text = re.sub(r'\([^)]*\)$', '', name_text).strip()

                    # Clean up description from allergens
                    if description:
                        description = re.sub(r'[1-9][0-9]*[A-Za-z,+\s]*$', '', description).strip()
                        description = re.sub(r'\([^)]*\)$', '', description).strip()

                    # Format the menu item
                    if name_text:
                        menu_text += f"• <b>{html.escape(name_text)}</b>"

                        # Add star for limited availability
                        if '⭐' in name_text or 'limited' in name_text.lower():
                            menu_text += " ⭐"
                        menu_text += "\n"

                        if description:
                            menu_text += f"  {html.escape(description)}\n"

                        # Add allergen emojis and text
                        if allergens_text:
                            # Translate codes to emojis
                            emoji_codes = translate_food_codes(allergens_text)
                            menu_text += f"  {emoji_codes}\n"
                            menu_text += f"  <i>({html.escape(allergens_text)})</i>\n"

                        # PRICE FORMAT
                        clean_price = price.replace("&euro;", "€").replace("€", "€").strip()
                        # Format price properly (e.g.: 2.50 -> 2,50€)
                        if clean_price and clean_price != "€":
                            # Convert dot to comma
                            clean_price = clean_price.replace('.', ',')
                            # Add € symbol if missing
                            if "€" not in clean_price:
                                clean_price = clean_price + "€"
                            menu_text += f"  💶 {clean_price}\n\n"
                        else:
                            menu_text += "\n"

            menu_text += "\n"

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

        return menu_text

    except Exception as e:
        logging.error(f"Enhanced menu fetch error: {e}")
        return "❌ Menu could not be loaded. Please try again later."


# ── menu commands ────────────────────────────────────────────────────────────

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show daily food menu in the requested format"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None or not is_user_allowed(user_id):
        await update.message.reply_text("Not authorized to use this bot.")
        return

    try:
        # Send message directly, don't try to edit
        status_msg = await update.message.reply_text(
            "🍽️ Loading today's menu...",
            reply_markup=get_main_keyboard()
        )

        # Page check
        if not page or page.is_closed():
            await update.message.reply_text("🔄 Logging in...")
            await login_studip()

        # Fetch menu
        menu_text = await get_todays_menu_enhanced(page)

        # Send menu
        await update.message.reply_text(
            menu_text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )

        # Delete status message (optional)
        try:
            await status_msg.delete()
        except:
            pass

    except Exception as e:
        error_msg = f"❌ Error loading menu:\n{str(e)}"
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_keyboard()
        )
        logging.error(f"Menu command error: {e}")


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for menu button (enhanced version)"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    try:
        await query.edit_message_text("🍽️ Loading menu...")

        if not page or page.is_closed():
            await query.edit_message_text("🔄 Logging in...")
            await login_studip()

        menu_text = await get_todays_menu_enhanced(page)
        await query.edit_message_text(menu_text, parse_mode="HTML")

    except Exception as e:
        error_msg = f"❌ Error loading menu:\n{str(e)}"
        await query.edit_message_text(error_msg)
        logging.error(f"Menu button error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
#  FILE LISTING + ZIP CREATION + WATCHER
# ──────────────────────────────────────────────────────────────────────────────
async def browser_auto_restart_loop(app):
    """Browser restart loop with better error handling"""
    global playwright, browser, browser_context, page

    while True:
        try:
            await asyncio.sleep(BROWSER_RESTART_INTERVAL)

            if not browser or browser.is_connected():
                continue

            logging.info("♻️ Browser restart triggered...")

            # Safely close existing browser
            try:
                if browser_context:
                    await browser_context.close()
                if browser:
                    await browser.close()
                if playwright:
                    await playwright.stop()
            except Exception as e:
                logging.warning(f"Browser cleanup warning: {e}")

            # Start new browser
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True)
            browser_context = await browser.new_context()
            page = await browser_context.new_page()

            # Re-login
            await login_studip()

        except Exception as e:
            logging.error(f"Browser auto-restart failed: {e}")
            await asyncio.sleep(300)  # Wait 5 minutes and retry


async def list_files(page, cid, folder_url=None):
    """Extract files/folders from Stud.IP file table preserving exact DOM order with proper URL handling."""
    async with page_lock:
        url = _normalize_url(
            folder_url) if folder_url else f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
            await page.wait_for_selector("table.documents tbody", timeout=15000)
        except Exception as e:
            logging.warning(f"Navigation error on list_files: {e}")
            raise TimeoutError("Timed out while loading file list")

        items = await page.evaluate(
            """() => {
                const res = [];
                const table = document.querySelector("table.documents");
                if (!table) return res;

                const readRow = (row, type) => {
                    const linkCell = row.querySelector("td:nth-child(3) a");
                    const name = (type === "file"
                                  ? row.querySelector("td:nth-child(3) a span")
                                  : linkCell)?.textContent?.trim() || null;

                    // GET FILE URL CORRECTLY - CRITICAL FIX
                    let href = null;
                    if (type === "file") {
                        // First find the download button
                        const downloadBtn = row.querySelector("a[title^='Download file']");
                        if (downloadBtn) {
                            href = downloadBtn.getAttribute("href");
                        }
                        // Fallback: first link
                        if (!href) {
                            href = linkCell?.getAttribute("href") || null;
                        }
                    } else {
                        // FOLDER URL
                        href = linkCell?.getAttribute("href") || null;
                    }

                    // Convert relative URLs to absolute
                    if (href && !href.startsWith('http') && !href.startsWith('//')) {
                        if (href.startsWith('/')) {
                            href = window.location.origin + href;
                        } else {
                            href = window.location.origin + '/' + href;
                        }
                    }

                    const timeEl = row.querySelector("time");
                    const modifiedText = timeEl?.textContent?.trim() || "-";
                    const modifiedISO  = timeEl?.getAttribute("datetime") || null;

                    const size = (type === "file"
                                  ? (row.querySelector("td:nth-child(4) span")?.textContent?.trim() || "-")
                                  : "-");

                    res.push({
                        type,
                        name,
                        url: href,
                        size,
                        modified: modifiedText,
                        modified_iso: modifiedISO,
                    });
                };

                table.querySelectorAll("tbody.subfolders tr").forEach(tr => readRow(tr, "folder"));
                table.querySelectorAll("tbody.files tr").forEach(tr => readRow(tr, "file"));

                return res;
            }"""
        )

    # Debug logging
    for item in items:
        if item["type"] == "file":
            logging.info(f"📄 File found: {item['name']} -> URL: {item['url']}")

    return items


async def get_fresh_file_url(page, cid, filename, current_url=None):
    """Return a fresh, valid download URL for a given file name with robust matching."""
    try:
        logging.info(f"🔄 Getting fresh URL for: {filename} in course {cid}")

        # List files from current URL or root
        if not current_url:
            current_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"

        files = await list_files(page, cid, current_url)

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
        root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
        if current_url != root_url:
            files = await list_files(page, cid, root_url)

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
async def create_recursive_zip(page, cid, base_url, browser_context, root_name: str, progress_callback=None):
    """
    Download all files (including subfolders) recursively into a ZIP file.
    Each file's URL is refreshed live before downloading.
    At the end, a ZIP summary card is returned.
    """
    from io import BytesIO
    import zipfile
    import posixpath

    async def _crawl(url, path_prefix, collected, empty_folders):
        """Recursively traverse folders and collect all files with fresh URLs."""
        try:
            items = await list_files(page, cid, url)
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
        logging.info(f"No files found in {root_name}.")
        return BytesIO(), 0, root_name, 0, 0

    cookies = await browser_context.cookies()
    cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    buf = BytesIO()
    timeout = aiohttp.ClientTimeout(total=300)
    total_size = 0
    success_count = 0

    async with aiohttp.ClientSession(timeout=timeout) as session:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zipf:
            # Add empty folders
            for folder in sorted(empty_folders):
                if folder and not folder.endswith("/"):
                    zipf.writestr(folder + "/", "")

            for i, (f, folder_path) in enumerate(all_files, start=1):
                filename = f["name"] or "unnamed"
                zip_path = f"{folder_path}/{filename}"

                # Always refresh the URL before downloading
                fresh_url = await get_fresh_file_url(page, cid, filename)
                if not fresh_url:
                    logging.warning(f"Skipping {filename}: no valid URL")
                    continue

                attempt = 0
                while attempt < 3:
                    try:
                        async with session.get(fresh_url, headers={"Cookie": cookie_header}) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                total_size += len(content)
                                zipf.writestr(zip_path, content)
                                success_count += 1
                                break
                            else:
                                logging.warning(f"HTTP {resp.status} for {filename}")
                                attempt += 1
                                await asyncio.sleep(2 ** attempt)
                    except Exception as e:
                        logging.warning(f"Retry {attempt + 1} for {filename}: {e}")
                        attempt += 1
                        await asyncio.sleep(2 ** attempt)

                if progress_callback:
                    try:
                        await progress_callback(total, i)
                    except Exception:
                        pass

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


async def check_new_announcements_parallel(page, bot, chat_id, silent: bool = False):
    """Parallel announcement checking for better performance"""
    global courses_map

    if not silent:
        await bot.send_message(chat_id=chat_id, text="📢 Checking announcements...",
                               disable_notification=True)

    try:
        CACHE_PATH = "announcement_cache.json"

        # ── Load cache safely ────────────────────────────────────────────────
        cache = {"seen": [], "history": []}
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                logging.warning("⚠️ Failed to load announcement cache, using empty.")

        seen = set(cache.get("seen", []))

        # ── Ensure courses are loaded ───────────────────────────────────────
        if not courses_map:
            logging.warning("⚠️ courses_map is empty — reloading...")
            # get_courses yerine list_courses kullan
            courses_list = await list_courses(page)
            courses_map = {cid: name for name, cid in courses_list}

            if not courses_map:
                logging.warning("🔐 Maybe session expired, trying re-login...")
                await login_studip()
                courses_list = await list_courses(page)
                courses_map = {cid: name for name, cid in courses_list}
                if not courses_map:
                    logging.error("❌ Still no courses after re-login, aborting check.")
                    return

        courses = [(name, cid) for cid, name in courses_map.items()]
        all_found = []

        # ── Parallel scraping setup ─────────────────────────────────────────
        semaphore = asyncio.Semaphore(5)

        async def check_single_course_announcements(course_name, cid):
            async with semaphore:
                try:
                    url = f"{BASE_URL}/dispatch.php/course/overview?cid={cid}"
                    async with page_lock:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        html = await page.content()

                    # Check if redirected to login page
                    if "loginform" in html:
                        logging.warning(f"🔐 Session expired while accessing {course_name}.")
                        await login_studip()
                        return course_name, cid, []

                    soup = BeautifulSoup(html, "html.parser")

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
                        title_tag = art.select_one("header h1 a") or art.select_one("header h1")
                        sender_tag = art.select_one(".news_user")
                        date_tag = art.select_one(".news_date")
                        body_tag = art.select_one(".formatted-content.ck-content")

                        title = title_tag.get_text(strip=True) if title_tag else "(No subject)"
                        sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
                        date_s = date_tag.get_text(strip=True) if date_tag else ""
                        body = clean_html_text(body_tag) if body_tag else "(No content)"

                        key = f"{cid}:{ann_id}" if ann_id else f"{cid}:{title}:{date_s}"
                        dt = _parse_ann_date(date_s)

                        announcement = {
                            "key": key,
                            "cid": cid,
                            "course": course_name,
                            "subject": title,
                            "sender": sender,
                            "date": date_s,
                            "dt": dt.isoformat() if dt else None,
                            "body": body,
                        }
                        course_announcements.append(announcement)

                    return course_name, cid, course_announcements

                except Exception as e:
                    logging.warning(f"Announcement parse error in {course_name}: {e}")
                    return course_name, cid, []

        # ── Parallel execution ─────────────────────────────────────────────
        tasks = [check_single_course_announcements(name, cid) for name, cid in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logging.error(f"Course announcement check failed: {result}")
                continue
            if isinstance(result, tuple) and len(result) == 3:
                course_name, cid, course_anns = result
                all_found.extend(course_anns)

        # ── Sort and detect new items ───────────────────────────────────────
        if all_found:
            all_found.sort(key=lambda x: (x["dt"] or "", x["date"]))
        new_items = [a for a in all_found if a["key"] not in seen]

        # ── Notifications ──────────────────────────────────────────────────
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
            await bot.send_message(chat_id=chat_id, text="☑️ No new announcements found.",
                                   disable_notification=True)

        # ── Update cache ───────────────────────────────────────────────────
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

        logging.info(f"📢 Announcement check completed: {len(new_items)} new, {len(all_found)} total.")

    except Exception as e:
        if not silent:
            await bot.send_message(chat_id=chat_id, text=f"❌ Announcement check error:\n{e}")
        logging.error(f"check_new_announcements_parallel fatal: {e}")


# Wrapper for parallel announcement checking
async def check_new_announcements(page, bot, chat_id, silent: bool = False):
    """Wrapper for parallel announcement checking"""
    return await check_new_announcements_parallel(page, bot, chat_id, silent)


async def fetch_message_body(session, message_url):
    """Fetch message body from detail page with improved parsing."""
    try:
        async with session.get(message_url) as resp:
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

                text = clean_html_text(content)
                if text and len(text.strip()) > 10:  # Meaningful content check
                    return text

        # Fallback: extract text from entire page
        main_content = soup.select_one("main") or soup.select_one("article") or soup
        text = clean_html_text(main_content)

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
        date = post.get('date', '-')
        body = post.get('body', '')

        # Limit body
        body_snippet = body if len(body) <= 200 else body[:200] + "..."

        text = (
            "━━━━━━━━━━━━━━━━━\n"
            "<b>💬 Forum Post</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"🏫 <b>Course:</b> {html.escape(course)}\n"
            f"📂 <b>Thread:</b> {html.escape(thread)}\n"
            f"👤 <b>By:</b> {html.escape(author)}\n"
            f"🕒 <b>Date:</b> {html.escape(date)}\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(body_snippet)}\n"
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


async def check_new_files(page, bot, chat_id, silent: bool = False):
    """Wrapper for parallel file checking"""
    return await check_new_files_parallel(page, bot, chat_id, silent)


async def check_new_files_parallel(page, bot, chat_id, silent: bool = False):
    """Parallel file checking for better performance"""
    global courses_map
    if not silent:
        await bot.send_message(
            chat_id=chat_id,
            text="📁 Checking files...",
            disable_notification=True
        )

    try:
        cache = load_files_cache()
        updated = False
        all_collected = []

        # Courses
        if not courses_map:
            async with page_lock:
                await page.goto(STUDIP_URL, wait_until="networkidle", timeout=30000)
            courses = await list_courses(page)
        else:
            courses = [(name, cid) for cid, name in courses_map.items()]

        if not courses:
            await bot.send_message(chat_id=chat_id, text="⚠️ No courses found.")
            return

        # Semaphore for parallel scanning (max 3 courses at once)
        semaphore = asyncio.Semaphore(3)

        async def check_single_course(course_name, cid):
            async with semaphore:
                try:
                    files = await list_files(page, cid)
                    if not files:
                        return course_name, cid, [], []

                    # Build current snapshot using stable timestamp
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


async def check_calendar_reminders(page, bot, chat_id, silent: bool = False):
    """Check for upcoming classes and send reminders."""
    try:
        if not silent:
            logging.info("⏰ Checking calendar reminders...")

        # Get cache
        reminders = load_reminders_cache()
        updated = False

        # Get today's events (force week view to ensure we have times)
        events = await get_calendar_events(page, week_view=False)
        if not events:
            return

        now = datetime.now(TZ_BERLIN)

        for event in events:
            # Skip invalid events
            if not event.get("start") or not event.get("title") or event["title"] == "Untitled":
                continue

            # Event details
            title = event["title"]
            start_dt = event["start"]
            location = event.get("location", "Unknown location")

            # Key for cache: title + start time
            # Using timestamp to manage recurring events properly
            event_key = f"{title}|{int(start_dt.timestamp())}"

            # If already sent, skip
            if event_key in reminders:
                continue

            # Calculate time difference
            diff = start_dt - now
            minutes_left = diff.total_seconds() / 60

            # Logic: Send reminder if within 10-15 minutes range
            # (10-15 ensures we catch it even if poll is every 2-3 mins)
            if 10 < minutes_left <= 15:
                # Send Reminder
                text = (
                    "🔔 <b>UPCOMING CLASS REMINDER</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>{title}</b>\n"
                    f"🕒 Starts in <b>{int(minutes_left)} mins</b>\n"
                    f"📍 {location}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )

                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    logging.info(f"🔔 Sent reminder for {title}")

                    # Store in cache
                    reminders[event_key] = int(now.timestamp())
                    updated = True
                except Exception as e:
                    logging.error(f"Failed to send reminder for {title}: {e}")

        # Cleanup old cache entries (keep last 24h)
        cutoff = now.timestamp() - 86400
        new_cache = {k: v for k, v in reminders.items() if v > cutoff}
        if len(new_cache) != len(reminders):
            updated = True

        if updated:
            save_reminders_cache(new_cache)

    except Exception as e:
        logging.error(f"❌ Error in check_calendar_reminders: {e}")


async def check_new_messages(page, bot, chat_id, silent: bool = False):
    """Fetch Stud.IP messages fully (content included) and send in order (newest last)."""

    if not silent:
        await bot.send_message(chat_id=chat_id, text="📨 Checking messages...", disable_notification=True)

    # Get cookies from session
    cookies = {cookie["name"]: cookie["value"] for cookie in await page.context.cookies()}
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
        # Fetch message list page
        url = f"{BASE_URL}/dispatch.php/messages/overview"
        async with session.get(url) as resp:
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table#messages tbody tr[id^='message_']")

        all_messages = []
        for tr in rows:
            link_tag = tr.select_one("td.title a[href*='dispatch.php/messages/read/']")
            if not link_tag:
                continue

            href = link_tag["href"].strip()
            title = link_tag.get_text(strip=True)
            sender_tag = tr.select_one("td:nth-of-type(3)")
            sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
            date_tag = tr.select_one("td:nth-of-type(4)")
            date = date_tag.get_text(strip=True) if date_tag else "Unknown"

            # URL'yi normalize et
            if not href.startswith('http'):
                if href.startswith('/'):
                    href = f"{BASE_URL}{href}"
                else:
                    href = f"{BASE_URL}/{href}"

            message_data = {
                "id": tr["id"].replace("message_", ""),
                "title": title,
                "sender": sender,
                "date": date,
                "url": href,
            }
            all_messages.append(message_data)

        # Load old cache
        old_messages = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    old_messages = json.load(f)
            except Exception as e:
                logging.warning(f"Could not load message cache: {e}")

        # Find new messages
        old_ids = {m["id"] for m in old_messages}
        new_messages = [m for m in all_messages if m["id"] not in old_ids]

        # Update content of ALL messages (not just new ones)
        logging.info(f"📨 Updating content for {len(all_messages)} messages...")

        # Fetch all message contents in parallel
        tasks = [fetch_message_body(session, m["url"]) for m in all_messages]
        bodies = await asyncio.gather(*tasks, return_exceptions=True)

        # Add contents to messages
        for msg, body in zip(all_messages, bodies):
            if isinstance(body, str):
                msg["body"] = body
            else:
                msg["body"] = f"[Error: {str(body)[:100]}]"

        # Update cache - save ALL messages
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(all_messages, f, indent=2, ensure_ascii=False)
            logging.info(f"✅ Message cache updated with {len(all_messages)} messages")
        except Exception as e:
            logging.error(f"Failed to save message cache: {e}")

        # Notify new messages
        if not new_messages:
            if not silent:
                await bot.send_message(chat_id=chat_id, text="☑️ No new messages found.", disable_notification=True)
            return False

        # Sort new messages by date (oldest to newest)
        new_messages_sorted = sorted(new_messages, key=lambda m: parse_date_safe(m.get("date", "")))

        for msg in new_messages_sorted:
            body_text = msg.get("body", "").strip()
            if not body_text or "[Error" in body_text:
                body_text = "📭 Message content could not be loaded"

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


# ── forum checking ─────────────────────────────────────────────────────────────

async def check_new_forum_posts_parallel(page, bot, chat_id, silent: bool = False):
    """Parallel forum checking for new posts"""
    global courses_map

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
            courses_list = await list_courses(page)
            courses_map = {cid: name for name, cid in courses_list}

        courses = [(name, cid) for cid, name in courses_map.items()]
        new_posts_found = []

        # ── Parallel scraping setup ─────────────────────────────────────────
        semaphore = asyncio.Semaphore(3)  # Don't overload the server

        async def check_single_course_forum(course_name, cid):
            async with semaphore:
                try:
                    # Navigate to Forum Overview
                    url = f"{BASE_URL}/dispatch.php/course/forum/index?cid={cid}"
                    async with page_lock:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        html_content = await page.content()

                    if "loginform" in html_content:
                        logging.warning(f"🔐 Session expired during forum check for {course_name}.")
                        # Let the next cycle handle re-login to avoid race conditions here
                        return []

                    soup = BeautifulSoup(html_content, "html.parser")

                    # Find all threads/categories
                    # Selector based on inspection: table.default.forum > tbody > tr
                    rows = soup.select("table.default.forum > tbody > tr")

                    # LOGGING: How many rows found?
                    logging.info(f"🔎 {course_name}: Found {len(rows)} forum rows")

                    course_new_posts = []

                    for row in rows:
                        # Check for "New" indicator
                        # Browser inspection: img.icon-role-attention
                        new_indicator = row.select_one(
                            "img.icon-role-attention, img[title*='new'], img[title*='neu'], span.new-indicator")

                        # LOGGING: Check specific row for debug (optional)
                        if new_indicator:
                            logging.info(f"✨ New indicator found in {course_name}")

                        # Also check the "new" attribute on the row itself if Stud.IP uses it
                        is_new = bool(new_indicator)

                        # Extract basic info
                        # Browser: Title is often in span.areaname inside the link
                        link_el = row.select_one("td:nth-child(2) a")
                        if not link_el:
                            # Try finding span.areaname and its parent
                            area_span = row.select_one("span.areaname")
                            if area_span:
                                link_el = area_span.find_parent("a")

                        if not link_el:
                            continue

                        thread_title = link_el.get_text(strip=True)
                        thread_href = link_el.get("href", "")

                        # DEBUG: Removed force True
                        # is_new = True

                        # If finding "new" is hard, we can also check the "Last post" date vs our cache
                        # but "new" indicator is most reliable for user-read status.

                        # Let's try to get the thread ID from href
                        # href usually looks like: .../index/index/[thread_id]?cid=...
                        thread_id = ""
                        m_id = re.search(r"/index/([a-f0-9]+)", thread_href)
                        if m_id:
                            thread_id = m_id.group(1)
                        else:
                            # Fallback: hash of title
                            thread_id = get_deterministic_hash(f"{cid}{thread_title}")

                        # Verify if we really need to check this thread
                        # If we have a cache entry for this thread, we can compare dates if available on the row
                        # But row date parsing can be tricky.

                        # Strategy: If "New" indicator is present OR simply check top threads if cache is empty
                        # For now, let's rely on "New" indicator to avoid opening every thread every time.
                        if is_new:
                            # ── Fetch Thread Details ────────────────────────
                            # We need to go into the thread to get the actual new post content
                            thread_url = f"{BASE_URL}/{thread_href}" if not thread_href.startswith(
                                "http") else thread_href

                            # Use a separate page or just navigate back?
                            # Navigating back and forth in a loop is slow.
                            # Better: Collect these URLs and fetch them after the loop or use a fresh context?
                            # Using the same page object means we must navigate.
                            # Since we are in a semaphore loop for *courses*, we are using the *single shared page*.
                            # Wait! `check_single_course_forum` runs in parallel but uses `page_lock`.
                            # So `page.goto` is serialized.
                            # We can just goto the thread, get content, then go back? No, `goto` history is linear.
                            # We can just proceed to the next URL.

                            async with page_lock:
                                await page.goto(thread_url, wait_until="domcontentloaded", timeout=20000)
                                thread_html = await page.content()

                            thread_soup = BeautifulSoup(thread_html, "html.parser")

                            # --- Logic to handle Categories vs Threads ---
                            # Check if this page is a Thread List (Category) or a Single Thread (Posts)
                            # Clues:
                            # - Thread List has a table with class "forum" or "default" containing rows with thread titles
                            # - Single Thread has "div.posting" or "article.forum_post"

                            is_thread_list = False
                            # Check for thread table
                            # Browser confirmed 'table.forum' exists on category page.
                            thread_table = thread_soup.select_one("table.forum, table.default")

                            # Check for real postings (actual user messages in a thread)
                            real_postings = thread_soup.select("div.real_posting")

                            # Debug inputs
                            logging.info(
                                f"ℹ️ {course_name} [{thread_title}]: Table found? {bool(thread_table)}, RealPostings: {len(real_postings)}")

                            # Decision logic
                            # If we have a table AND no real postings, it's likely a list
                            # Decision logic
                            # If we have a table, it IS a list (unless it's a very weird legacy thread with a table in a post)
                            # The presence of <div class="posting"> (singular) usually indicates the category description, not a user post
                            if thread_table:
                                is_thread_list = True
                                logging.info(f"📂 {course_name}: Table detected, forcing Category Mode.")
                            elif not real_postings:
                                # No table, no postings -> Empty or unknown
                                pass

                            if is_thread_list:
                                logging.info(f"📂 {course_name}: Processing as Category/Thread List.")

                                # Scrape threads from the table
                                # Selector: table.forum > tbody > tr
                                sub_rows = thread_table.select("tbody > tr") if thread_table else []

                                # Process top 5 threads even if not new (for debugging/manual check)
                                for i, sub_row in enumerate(sub_rows):
                                    # Limit to top 5 to avoid scraping archive
                                    if i > 5: break

                                    # Check for new (icon)
                                    sub_new = sub_row.select_one(
                                        "img.icon-role-attention, img[title*='new'], img[title*='neu']")

                                    # During manual check, maybe we want to check recent ones regardless?
                                    # Let's check if it's new OR if it's one of the top 3
                                    if not sub_new and i >= 3:
                                        continue

                                    # Extract link
                                    sub_link = sub_row.select_one("td:nth-child(2) a")
                                    if not sub_link: continue

                                    sub_href = sub_link.get("href", "")
                                    sub_title = sub_link.get_text(strip=True)

                                    logging.info(f"    ➡️ Checking thread: {sub_title}")

                                    # Visit the thread
                                    sub_url = f"{BASE_URL}/{sub_href}" if not sub_href.startswith("http") else sub_href

                                    try:
                                        async with page_lock:
                                            await page.goto(sub_url, wait_until="domcontentloaded", timeout=20000)
                                            sub_html = await page.content()

                                        sub_soup = BeautifulSoup(sub_html, "html.parser")

                                        # Now scrape posts from this SUB-thread
                                        # EXCLUDE .bg2 (preview box) and hidden elements
                                        sub_posts = sub_soup.select(
                                            "div.real_posting, div.posting:not(.bg2), article.forum_post, div[id^='forumposting_']")

                                        # Filter out hidden elements if possible (BS4 doesn't check style, so we do it manually)
                                        sub_posts = [p for p in sub_posts if "display: none" not in p.get("style", "")]

                                        if not sub_posts:
                                            # Fallback for old style
                                            sub_posting_divs = sub_soup.select("div.postbody")
                                            if sub_posting_divs:
                                                sub_posts = [d.find_parent() for d in sub_posting_divs if
                                                             "bg2" not in d.find_parent().get("class", [])]

                                        if sub_posts:
                                            # Get ALL posts for full conversation context
                                            context_posts = sub_posts
                                            last_sub_post = sub_posts[-1]  # The newest one

                                            # Extract info from the LAST post for cache checking
                                            s_content_el = last_sub_post.select_one(
                                                "div.postbody div.content, section.content, div.content")
                                            if s_content_el:
                                                for comment in s_content_el.find_all(
                                                        string=lambda text: isinstance(text,
                                                                                       str) and text.strip().startswith(
                                                                '<!--')):
                                                    comment.extract()
                                                s_body = clean_html_text(s_content_el)
                                            else:
                                                s_body = "(No content)"

                                            # Author/Date from last post
                                            s_title_div = last_sub_post.select_one("div.postbody div.title, div.title")
                                            s_author = "Unknown"
                                            s_date = "Unknown Date"

                                            if s_title_div:
                                                s_a_el = s_title_div.select_one("a[href*='profile']")
                                                if s_a_el:
                                                    s_author = s_a_el.get_text(strip=True)
                                                else:
                                                    full_text = s_title_div.get_text(" ", strip=True)
                                                    full_text = ' '.join(full_text.split())
                                                    if ',' in full_text:
                                                        s_author = full_text.split(',')[0].strip()
                                                    elif '-' in full_text:
                                                        s_author = full_text.split('-')[0].strip()
                                                    else:
                                                        s_author = full_text.strip()

                                                s_author = re.sub(r',?\s*\d{1,2}\.\d{1,2}\.\d{2,4}.*$', '',
                                                                  s_author).strip()

                                                s_text = s_title_div.get_text(" ", strip=True)
                                                s_text = ' '.join(s_text.split())
                                                s_m_date = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2})",
                                                                     s_text)
                                                if s_m_date: s_date = s_m_date.group(1)

                                            s_post_id = get_deterministic_hash(f"{sub_title}{s_date}{s_author}")

                                            # Check cache
                                            cache_key = f"{cid}_{sub_title}"
                                            if cache_key in cache:
                                                last_cached = cache[cache_key]
                                                if last_cached.get("date") == s_date and last_cached.get(
                                                        "author") == s_author:
                                                    continue

                                            # Extract all context posts
                                            conversation = []
                                            for idx, ctx_post in enumerate(context_posts):
                                                is_newest = (idx == len(context_posts) - 1)

                                                ctx_content_el = ctx_post.select_one(
                                                    "div.postbody div.content, section.content, div.content")
                                                if ctx_content_el:
                                                    for comment in ctx_content_el.find_all(
                                                            string=lambda text: isinstance(text,
                                                                                           str) and text.strip().startswith(
                                                                    '<!--')):
                                                        comment.extract()
                                                    ctx_body = clean_html_text(ctx_content_el)
                                                else:
                                                    ctx_body = "(No content)"

                                                ctx_title_div = ctx_post.select_one("div.postbody div.title, div.title")
                                                ctx_author = "Unknown"
                                                ctx_date = "Unknown Date"

                                                if ctx_title_div:
                                                    ctx_a_el = ctx_title_div.select_one("a[href*='profile']")
                                                    if ctx_a_el:
                                                        ctx_author = ctx_a_el.get_text(strip=True)
                                                    else:
                                                        ctx_full = ctx_title_div.get_text(" ", strip=True)
                                                        ctx_full = ' '.join(ctx_full.split())
                                                        if ',' in ctx_full:
                                                            ctx_author = ctx_full.split(',')[0].strip()
                                                        elif '-' in ctx_full:
                                                            ctx_author = ctx_full.split('-')[0].strip()
                                                        else:
                                                            ctx_author = ctx_full.strip()

                                                    ctx_author = re.sub(r',?\s*\d{1,2}\.\d{1,2}\.\d{2,4}.*$', '',
                                                                        ctx_author).strip()

                                                    ctx_text = ctx_title_div.get_text(" ", strip=True)
                                                    ctx_text = ' '.join(ctx_text.split())
                                                    ctx_m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2})",
                                                                      ctx_text)
                                                    if ctx_m: ctx_date = ctx_m.group(1)

                                                conversation.append({
                                                    "author": ctx_author,
                                                    "date": ctx_date,
                                                    "body": ctx_body,
                                                    "is_new": is_newest
                                                })

                                            logging.info(f"✨ Found post in thread '{sub_title}': {s_author} - {s_date}")

                                            if s_author == "Unknown":
                                                logging.warning(
                                                    f"⚠️ Unknown author in thread '{sub_title}'. HTML snippet: {str(last_sub_post)[:500]}")

                                            post_data = {
                                                "key": cache_key,
                                                "cid": cid,
                                                "course": course_name,
                                                "thread": thread_title + " > " + sub_title,
                                                "author": s_author,
                                                "date": s_date,
                                                "body": s_body,
                                                "url": sub_url,
                                                "conversation": conversation  # NEW: full context
                                            }
                                            course_new_posts.append(post_data)

                                            cache[cache_key] = {
                                                "cid": cid,
                                                "thread_id": get_deterministic_hash(sub_href),
                                                "thread_title": sub_title,
                                                "post_id": s_post_id,
                                                "author": s_author,
                                                "date": s_date,
                                                "body_snippet": s_body[:50]
                                            }

                                    except Exception as e:
                                        logging.error(f"Failed to scrape sub-thread {sub_title}: {e}")

                                # After processing list, skip the rest of the outer loop (which looks for single posts)
                                continue

                            # Standard single thread scraping (fallback or if not a list)
                            if not is_thread_list:
                                # We are (presumably) in a thread already, or single-thread view
                                # Relaxed selectors to catch everything, EXCLUDING preview (.bg2)
                                posts = thread_soup.select(
                                    "div.real_posting, div.posting:not(.bg2), article.forum_post, div.forum_post")

                                # Filter hidden
                                posts = [p for p in posts if "display: none" not in p.get("style", "")]

                                # Fallback: look for IDs
                                if not posts:
                                    posts = thread_soup.select("div[id^='forumposting_']")

                                # Fallback 2: look for postbody and get parent
                                if not posts:
                                    posting_divs = thread_soup.select("div.postbody")
                                    if posting_divs:
                                        posts = [div.find_parent() for div in posting_divs if
                                                 "bg2" not in div.find_parent().get("class", [])]

                            if not posts:
                                logging.warning(
                                    f"⚠️ {course_name}: No posts found in '{thread_title}' (Category view? {is_thread_list})")
                                # Log the URL we tried
                                logging.info(f"   -> URL: {thread_url}")
                                # Clean up formatting if empty
                                continue

                            # Iterate over found posts (usually just 1 if we came from main list, or multiple if from thread list)
                            for last_post in posts:

                                # Use override name if available (from nested thread)
                                current_course_name = getattr(last_post, 'course_name_override', course_name)
                                current_thread_title = getattr(last_post, 'thread_title_override',
                                                               thread_title)  # not set yet but consistent

                                # Extract ID
                                post_id = last_post.get("id")  # forumposting_...

                                # Content - strip HTML tags
                                # Browser: div.postbody -> div.content
                                content_el = last_post.select_one(
                                    "div.postbody div.content, div.content, div.formatted-content")
                                if content_el:
                                    # Remove HTML comments
                                    for comment in content_el.find_all(
                                            string=lambda text: isinstance(text, str) and text.strip().startswith(
                                                    '<!--')):
                                        comment.extract()
                                    body_text = clean_html_text(content_el)
                                else:
                                    body_text = "(No content)"

                                if body_text == "(No content)":
                                    logging.warning(
                                        f"⚠️ {current_course_name}: Post content empty. HTML: {str(last_post)[:200]}")

                                # Author & Date (often in div.title inside postbody)
                                # Structure: <div class="title"> <a href="...">Author</a> - Date </div>

                                author_text = "Unknown"
                                date_text = "Unknown Date"

                                # Try finding title div
                                title_div = last_post.select_one("div.postbody div.title, div.title, header")

                                if title_div:
                                    # Author - look for link first
                                    author_el = title_div.select_one(
                                        "a[href*='profile'], a.profile, a.username, div.small_screen a")
                                    if author_el:
                                        author_text = author_el.get_text(strip=True)
                                    else:
                                        # Fallback: get text before comma or dash
                                        full_text = title_div.get_text(" ", strip=True)
                                        # Clean whitespace
                                        full_text = ' '.join(full_text.split())
                                        if ',' in full_text:
                                            author_text = full_text.split(',')[0].strip()
                                        elif '-' in full_text:
                                            author_text = full_text.split('-')[0].strip()
                                        else:
                                            author_text = full_text.strip()

                                    # Clean author: remove any date pattern that might have leaked
                                    author_text = re.sub(r',?\s*\d{1,2}\.\d{1,2}\.\d{2,4}.*$', '', author_text).strip()

                                    # Date
                                    # Usually text matches: dd.mm.yyyy hh:mm
                                    # Regex search in the whole title text
                                    title_text = title_div.get_text(" ", strip=True)
                                    title_text = ' '.join(title_text.split())  # Clean whitespace
                                    m_date = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2})", title_text)
                                    if m_date:
                                        date_text = m_date.group(1)
                                    else:
                                        logging.info(f"ℹ️ Date extraction failed for '{title_text}'")
                                else:
                                    logging.warning(f"⚠️ {current_course_name}: Title/Header div not found in post.")

                                # If still unknown, try fallback
                                if author_text == "Unknown":
                                    # Fallback to old header extraction
                                    author_el = last_post.select_one("a.profile, a.username")
                                    if author_el:
                                        author_text = author_el.get_text(strip=True)

                                    header_el = last_post.select_one("header, div.header")
                                    if header_el:
                                        header_text = header_el.get_text(" ", strip=True)
                                        if author_text == "Unknown":
                                            author_text = header_text  # simplified
                                        m_date = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4},?\s+\d{1,2}:\d{2})",
                                                           header_text)
                                        if m_date:
                                            date_text = m_date.group(1)

                                # Final check and debug dump
                                if author_text == "Unknown":
                                    logging.warning(
                                        f"⚠️ {current_course_name}: Author extraction failed. HTML: {str(last_post)[:1000]}")

                                # Unique Key for Cache
                                # If no post_id, make one from ThreadID + Date
                                unique_key = f"{cid}:{post_id if post_id else date_text}"  # Simplified key

                                if unique_key in cached_posts:
                                    continue

                                # Add to new posts list
                                new_posts.append({
                                    "course": current_course_name,
                                    "thread": current_thread_title if 'current_thread_title' in locals() else thread_title,
                                    "author": author_text,
                                    "date": date_text,
                                    "content": body_text,
                                    "key": unique_key
                                })

                            # End of posts loop
                            # Continue to next row in main loop
                            pass

                            logging.info(f"✨ Detected Post: {unique_key} | {thread_title}")

                            if unique_key not in cache:
                                # Found a REAL new post
                                logging.info(f"🆕 NEW Post added to list: {thread_title}")
                                post_data = {
                                    "key": unique_key,
                                    "cid": cid,
                                    "course": course_name,
                                    "thread": thread_title,
                                    "author": author_text,
                                    "date": date_text,
                                    "body": body_text,
                                    "url": thread_url
                                }
                                course_new_posts.append(post_data)

                    return course_new_posts

                except Exception as e:
                    logging.warning(f"Forum check error in {course_name}: {e}")
                    return []

        # ── Run checks ──────────────────────────────────────────────────────
        tasks = [check_single_course_forum(name, cid) for name, cid in courses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, list):
                new_posts_found.extend(res)

        # ── Notifications ──────────────────────────────────────────────────
        if new_posts_found:
            # Sort by date maybe? Date parsing is fragile, let's just send.
            for post in new_posts_found:
                # Check if we have conversation context
                if 'conversation' in post and post['conversation']:
                    # Multi-message format with context
                    text_parts = [
                        "🔥 💬 <b>NEW FORUM POST</b> 🔥\n",
                        "━━━━━━━━━━━━━━━━━\n",
                        f"🏫 <b>Course:</b> {html.escape(post['course'])}\n",
                        f"📂 <b>Thread:</b> {html.escape(post['thread'])}\n",
                        "━━━━━━━━━━━━━━━━━\n\n"
                    ]

                    # Add each message in conversation
                    for msg in post['conversation']:
                        # Show full message without truncation
                        msg_body = msg['body']

                        new_marker = " 🔥 <b>LATEST MESSAGE</b>" if msg['is_new'] else ""
                        text_parts.append(
                            f"👤 <b>{html.escape(msg['author'])}</b> ({html.escape(msg['date'])}){new_marker}\n"
                            f"{html.escape(msg_body)}\n\n"
                        )

                    text_parts.append("━━━━━━━━━━━━━━━━━")
                    text = "".join(text_parts)
                else:
                    # Fallback: single message format (for non-category threads)
                    body_snippet = post['body']
                    if len(body_snippet) > 500:
                        body_snippet = body_snippet[:500] + "..."

                    text = (
                        "🔥 💬 <b>NEW FORUM POST</b> 🔥\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"🏫 <b>Course:</b> {html.escape(post['course'])}\n"
                        f"📂 <b>Thread:</b> {html.escape(post['thread'])}\n"
                        f"👤 <b>By:</b> {html.escape(post['author'])}\n"
                        f"🕒 <b>Date:</b> {html.escape(post['date'])}\n"
                        "━━━━━━━━━━━━━━━━━\n"
                        f"{html.escape(body_snippet)}\n"
                        "━━━━━━━━━━━━━━━━━"
                    )

                # Send
                try:
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Forward to WA 📲", callback_data="forward_wa")]])
                    await broadcast(bot, text, parse_mode="HTML", reply_markup=markup)
                    # Update cache immediately to avoid duplicate on crash
                    cache[post["key"]] = {k: v for k, v in post.items() if k != "key"}
                except Exception as e:
                    logging.error(f"Failed to send forum notification: {e}")

            # Save Cache
            try:
                # Limit cache size
                if len(cache) > 500:
                    # Remove old random items or implement proper LRU.
                    # For now just keep it simple, JSON is small.
                    pass

                with open(CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logging.error(f"Failed to save forum cache: {e}")

        elif not silent:
            await bot.send_message(chat_id=chat_id, text="☑️ No new forum posts found.",
                                   disable_notification=True)

    except Exception as e:
        if not silent:
            await bot.send_message(chat_id=chat_id, text=f"❌ Forum check error:\n{e}")
        logging.error(f"check_new_forum_posts_parallel fatal: {e}")


async def check_new_forum_posts(page, bot, chat_id, silent: bool = False):
    """Wrapper"""
    return await check_new_forum_posts_parallel(page, bot, chat_id, silent)


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


async def get_calendar_events(page, week_view=False):
    """
    Stud.IP Planer -> Return weekly or daily events.
    With improved error handling.
    """
    BASE_PLANER = f"{BASE_URL}/plugins.php/planerplugin/planer"

    async def _ensure_week_view():
        """Safely switch to 'Wochenkalender' tab and focus on 'Today'"""
        nav_paths = [
            {
                "url": f"{BASE_URL}/plugins.php/planerplugin/planer/calendar/index",
                "tabs": ["Wochenkalender", "Weekly Calendar", "Wochenansicht", "Week view"],
                "name": "Planner-Calendar"
            },
            {
                "url": f"{BASE_URL}/plugins.php/planerplugin/planer/schedule/index",
                "tabs": ["Wochenkalender", "Weekly Calendar", "Wochenansicht", "Week view"],
                "name": "Planner-Schedule"
            },
            {
                "url": f"{BASE_URL}/plugins.php/planerplugin/planer/index",
                "tabs": ["Wochenkalender", "Weekly Calendar", "Wochenansicht", "Week view"],
                "name": "Planner-Main"
            },
            {
                "url": f"{BASE_URL}/dispatch.php/calendar/views/week",
                "tabs": [],
                "name": "Standard-Kalender-Week"
            },
            {
                "url": f"{BASE_URL}/dispatch.php/calendar/planner/index",
                "tabs": [],
                "name": "Planner-Dispatch"
            }
        ]
        
        for path in nav_paths:
            try:
                # 0. Check Login
                if "login" in page.url.lower() or await page.locator(SELECTORS["start"]).count() > 0:
                    logging.info("🕒 Session expired or at login page. Re-logging...")
                    await login_studip()

                logging.info(f"📅 Navigating to {path['name']}: {path['url']}")
                await page.goto(path["url"], wait_until="networkidle", timeout=45000)
                
                # Check again after goto
                if "login" in page.url.lower():
                    logging.info("🕒 Redirected to login. Re-logging...")
                    await login_studip()
                    await page.goto(path["url"], wait_until="networkidle", timeout=45000)

                # 1. Switch Tabs if needed
                if path["tabs"]:
                    tab_found = False
                    for tab_name in path["tabs"]:
                        tab_loc = page.locator(f"a:has-text('{tab_name}')").first
                        if await tab_loc.count() > 0:
                            parent = tab_loc.locator("xpath=..")
                            cls = await parent.get_attribute("class") or ""
                            if "active" not in cls.lower():
                                logging.info(f"Switching tab to '{tab_name}'")
                                await tab_loc.click(force=True)
                                await asyncio.sleep(2)
                            tab_found = True
                            break
                    if not tab_found:
                        logging.debug(f"Direct tab '{path['name']}' not found, attempting to proceed anyway.")

                # 2. Click 'Today' Button
                today_selectors = [
                    "button.fc-today-button", 
                    "button.fc-today-button.fc-button.fc-button-primary",
                    "button:has-text('today')", 
                    "button:has-text('heute')", 
                    "button:has-text('Today')",
                    ".fc-button-today",
                    "button.fc-button-primary:has-text('today')"
                ]
                for sel in today_selectors:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            logging.info(f"Clicking 'Today' button via {sel}")
                            await loc.click(force=True, timeout=5000)
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue
                
                # 3. Wait for content
                try:
                    await page.wait_for_selector(".fc-view, .fc-content, td.fc-day, .calendar-view, #calendar, .fc-agenda-view", timeout=30000)
                    logging.info(f"✅ Loaded {path['name']}")
                    return True
                except Exception as e:
                    logging.warning(f"⚠️ Content timeout for {path['name']}: {e}")
                    # Capture debug screenshot
                    try:
                        p_name = str(path.get("name", "unknown")).replace(" ", "_").replace("-", "_")
                        debug_path = f"debug_calendar_{p_name}.png"
                        await page.screenshot(path=debug_path)
                        logging.info(f"📸 Saved diagnostic screenshot to {debug_path}")
                    except Exception as se:
                        logging.warning(f"Failed to save debug screenshot: {se}")
                        
                    title = await page.title()
                    logging.info(f"Current Page: '{title}' at {page.url}")
                    continue

            except Exception as e:
                logging.warning(f"⚠️ Path {path['name']} failed: {e}")
                continue

        return False

        pass

    try:
        # 1) Prepare view (with retry mechanism)
        success = await _ensure_week_view()
        if not success:
            return []

        # 2) Get HTML
        html_text = await page.content()
        soup = BeautifulSoup(html_text, "html.parser")

        # 3) Get week dates
        week_dates = []
        for th in soup.select(".fc-head .fc-day-header[data-date]"):
            week_dates.append(th["data-date"])

        # 4) Time-grid column order (excluding axis)
        dates_order = []
        for td in soup.select(".fc-time-grid .fc-bg tr > td"):
            if "fc-axis" in (td.get("class") or []):
                continue
            d = td.get("data-date")
            if d:
                dates_order.append(d)

        # 5) Event nodes (Modern FullCalendar selectors)
        ev_nodes = soup.select(".fc-view .fc-event")
        logging.info(f"🟢 Detected {len(ev_nodes)} event elements in DOM.")

        events: list[dict[str, Any]] = []
        seen = set()
        now = datetime.now(TZ_BERLIN)

        for ev in ev_nodes:
            # --- tooltip / aria (Fallback) ---
            tooltip_html = ev.get("data-tooltip") or ev.get("title") or ""
            aria_html = ev.get("aria-label") or ""

            fields = {}
            if tooltip_html:
                fields = _parse_tooltip_fields(tooltip_html)
            elif aria_html:
                fields = _parse_tooltip_fields(aria_html)

            # --- title & location (Direct DOM extraction) ---
            t_el = ev.select_one(".fc-title") or ev.select_one(".fc-event-title")
            title = fields.get("title")
            location = fields.get("location")

            if t_el:
                raw_text = t_el.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
                if not title and lines:
                    title = lines[0]
                if not location and len(lines) > 1:
                    location = lines[-1]

            title = title or "Untitled"

            # --- time ---
            start_dt = fields.get("start")
            end_dt = fields.get("end")

            if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
                time_el = ev.select_one(".fc-time, .fc-event-time")
                time_text = time_el.get("data-full") if time_el else None
                if not time_text and time_el:
                    time_text = time_el.get_text(" ", strip=True)
                
                m = re.search(r"(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2})", (time_text or ""))
                if m:
                    s_str, e_str = m.groups()
                    col_date = None
                    try:
                        parent_td = ev.find_parent("td")
                        idx = -1
                        if parent_td:
                            tr = parent_td.parent
                            tds = tr.find_all("td", recursive=False)
                            if parent_td in tds:
                                idx = tds.index(parent_td) - 1
                        
                        if 0 <= idx < len(week_dates):
                            col_date = week_dates[idx]
                    except Exception:
                        pass
                    
                    if not col_date and isinstance(start_dt, datetime):
                        col_date = start_dt.date().isoformat()
                    
                    if col_date:
                        try:
                            d0 = datetime.fromisoformat(col_date).date()
                            s_t = datetime.strptime(s_str, "%H:%M").time()
                            e_t = datetime.strptime(e_str, "%H:%M").time()
                            start_dt = datetime.combine(d0, s_t)
                            end_dt = datetime.combine(d0, e_t)
                        except Exception:
                            pass

            if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
                continue

            # Timezone awareness
            if isinstance(start_dt, datetime) and start_dt.tzinfo is None: 
                start_dt = start_dt.replace(tzinfo=TZ_BERLIN)
            if isinstance(end_dt, datetime) and end_dt.tzinfo is None: 
                end_dt = end_dt.replace(tzinfo=TZ_BERLIN)

            if not isinstance(start_dt, datetime) or not isinstance(end_dt, datetime):
                continue

            # status
            status = "upcoming"
            if end_dt < now:
                status = "past"
            elif start_dt <= now <= end_dt:
                status = "ongoing"
            else:
                status = "upcoming"

            # url
            href = ev.get("href")
            url = BASE_URL + href if href and href.startswith("/") else None

            start_iso = start_dt.isoformat() if start_dt else ""
            end_iso = end_dt.isoformat() if end_dt else ""
            # Ensure title and location are strings for the key
            t_key = str(title or "")
            l_key = str(location or "")
            key = (t_key, start_iso, end_iso, l_key)
            if key in seen:
                continue
            seen.add(key)

            events.append({
                "title": str(title or "Untitled"),
                "start": start_dt,
                "end": end_dt,
                "location": str(location or ""),
                "url": url,
                "status": status,
            })

        # For weekly view return all events, for daily only today's
        if not week_view:
            today_date = now.date()
            logging.info(f"🔍 Filtering for today's date: {today_date}")
            events = [e for e in events if isinstance(e.get("start"), datetime) and e["start"].date() == today_date]
            for ev in events:
                logging.info(f"✅ Keeping event: {ev['title']} ({ev['start'].isoformat() if isinstance(ev.get('start'), datetime) else 'N/A'})")

        logging.info(f"📅 Found {len(events)} events for {'week' if week_view else 'today'}")
        return events

    except Exception as e:
        logging.error(f"❌ Calendar fetch error: {e}")
        return []


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


async def send_weekly_calendar(message_or_query, events):
    from datetime import datetime

    async def _edit_or_reply(text, **kw):
        if hasattr(message_or_query, "edit_message_text"):
            await message_or_query.edit_message_text(text, **kw)
        elif hasattr(message_or_query, "reply_text"):
            await message_or_query.reply_text(text, **kw)

    if not events:
        text = (
            "🎉 *No Events This Week!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🗓️ *Weekly View*"
        )
        await _edit_or_reply(text, parse_mode="Markdown")
        return

    # Group events by day
    events_by_day = {}
    for event in events:
        day_key = event["start"].strftime("%Y-%m-%d")
        if day_key not in events_by_day:
            events_by_day[day_key] = []
        events_by_day[day_key].append(event)

    # Sort each day's events by time
    for day_events in events_by_day.values():
        day_events.sort(key=lambda x: x["start"])

    # Header
    start_of_week = min(events, key=lambda x: x["start"])["start"]
    end_of_week = max(events, key=lambda x: x["end"])["end"]

    header = (
        "✨ *Weekly Schedule* ✨\n"
        f"🗓️ *{start_of_week:%d %B} - {end_of_week:%d %B %Y}*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    lines = [header]

    # Process days in order
    for day_key in sorted(events_by_day.keys()):
        day_events = events_by_day[day_key]
        day_date = datetime.fromisoformat(day_key)
        day_name = day_date.strftime("%A")
        day_emoji = _get_day_emoji(day_name)

        lines.append(f"\n{day_emoji} *{day_name}, {day_date:%d.%m.%Y}*")
        lines.append("━━━━━━━━━━━━━━━━━━")

        if not day_events:
            lines.append("   🎉 *No events*")
            continue

        for event in day_events:
            status = _status_emoji(event.get("status"))
            course_icon = _get_course_icon(event.get("title", ""))
            loc_icon = _get_location_emoji(event.get("location", ""))

            duration = f"{event['start']:%H:%M}–{event['end']:%H:%M}"

            lines.append(
                f"{status} {course_icon} *{event['title']}*\n"
                f"   🕒 `{duration}`\n"
                f"   {loc_icon} {_safe_loc(event['location'])}"
            )

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    text = "\n".join(lines)

    await _edit_or_reply(text, parse_mode="Markdown")


async def send_today_calendar(message_or_query, events):
    from datetime import datetime

    async def _edit_or_reply(text, **kw):
        if hasattr(message_or_query, "edit_message_text"):
            await message_or_query.edit_message_text(text, **kw)
        elif hasattr(message_or_query, "reply_text"):
            await message_or_query.reply_text(text, **kw)

    today = datetime.now()

    if not events:
        text = (
            "🎉 *No Events Today!*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📅 *Your schedule is clear*\n"
            "🕒 Perfect time to catch up or relax!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🗓️ *{today:%A, %d %B %Y}*"
        )
        await _edit_or_reply(text, parse_mode="Markdown")
        return

    # header + title
    header = (
        "✨ *Today's Schedule* ✨\n"
        f"📅 *{today:%A, %d %B %Y}*\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    lines = [header]
    for ev in events:
        ev_title = str(ev.get("title") or "Untitled")
        ev_location = str(ev.get("location") or "")
        
        status = _status_emoji(ev.get("status"))
        course_icon = _get_course_icon(ev_title)
        loc_icon = "🏫" if "A" in ev_location else "📍"

        duration = f"{ev['start']:%H:%M}–{ev['end']:%H:%M}"

        lines.append(
            f"{status} {course_icon} *{ev['title']}*\n"
            f"   🕒 `{duration}`\n"
            f"   {loc_icon} {_safe_loc(ev['location'])}"
        )

    lines.append("━━━━━━━━━━━━━━━━━━")
    text = "\n\n".join(lines)

    await _edit_or_reply(text, parse_mode="Markdown")


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

    # Main Menu button
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="show_menu")])

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
    """Manual full check: messages → announcements → files + start all watchers"""
    chat_id = update.effective_chat.id
    global page, browser_context, global_watcher_paused, last_full_check_time, global_scan_count

    if chat_id in check_in_progress:
        await update.message.reply_text("⚠️ Another check is already running.")
        return

    try:
        check_in_progress.add(chat_id)
        global_watcher_paused = True  # ✅ Pause global watchers

        # Send a single "Checking..." message
        status_msg = await update.message.reply_text(
            "🔍 Starting full check...\n\n"
            "📨 Checking messages...\n"
            "📢 Checking announcements...\n"
            "📁 Checking files...",
            disable_notification=True
        )

        # ♻️ Reconnect if needed
        if not page or page.is_closed():
            emoji_map = {
                "starting login": "⚙️",
                "opening idp": "🌐",
                "submitting username": "👤",
                "submitting password": "🔒",
                "submitting one-time code": "🔢",
                "login successful": "✅",
            }

            async def notify(msg: str):
                try:
                    key = msg.lower()
                    icon = next((v for k, v in emoji_map.items() if k in key), "ℹ️")
                    await status_msg.edit_text(f"{icon} {msg}")
                except Exception:
                    pass

            page = await login_studip(notify=notify)
            browser_context = page.context

        # ✅ Load courses ONCE
        if not courses_map:
            try:
                async with page_lock:
                    await page.goto(STUDIP_URL, wait_until="networkidle", timeout=30000)
                await list_courses(page)
                logging.info(f"✅ Loaded {len(courses_map)} courses")
            except Exception as e:
                logging.error(f"Failed to load courses: {e}")

        # List to collect results
        results = []

        # ✉️ 1️⃣ Check messages - SILENT modda
        try:
            await status_msg.edit_text(
                "🔍 Starting full check...\n\n"
                "✅ Checking messages...\n"
                "📢 Checking announcements...\n"
                "📁 Checking files..."
            )
            has_new_messages = await check_new_messages(page, context.bot, chat_id, silent=True)
            results.append(("messages", has_new_messages))
        except Exception as e:
            results.append(("messages", f"error: {str(e)[:200]}"))
            logging.exception("check_new_messages failed")

        # 📢 2️⃣ Check announcements - SILENT modda
        try:
            await status_msg.edit_text(
                "🔍 Starting full check...\n\n"
                "✅ Checking messages... ✓\n"
                "✅ Checking announcements...\n"
                "📁 Checking files..."
            )
            has_new_announcements = await check_new_announcements(page, context.bot, chat_id, silent=True)
            results.append(("announcements", has_new_announcements))
        except Exception as e:
            results.append(("announcements", f"error: {str(e)[:200]}"))
            logging.exception("check_new_announcements failed")

        # 📂 3️⃣ Check files - SILENT modda
        try:
            await status_msg.edit_text(
                "🔍 Starting full check...\n\n"
                "✅ Checking messages... ✓\n"
                "✅ Checking announcements... ✓\n"
                "✅ Checking files...\n"
                "💬 Checking forum posts..."
            )
            has_new_files = await check_new_files(page, context.bot, chat_id, silent=True)
            results.append(("files", has_new_files))
        except Exception as e:
            results.append(("files", f"error: {str(e)[:200]}"))
            logging.exception("check_new_files failed")

        # 💬 4️⃣ Check forum - SILENT modda
        try:
            await status_msg.edit_text(
                "🔍 Starting full check...\n\n"
                "✅ Checking messages... ✓\n"
                "✅ Checking announcements... ✓\n"
                "✅ Checking files... ✓\n"
                "✅ Checking forum posts..."
            )
            has_new_forum_posts = await check_new_forum_posts_parallel(page, context.bot, chat_id, silent=True)
            results.append(("forum", has_new_forum_posts))
        except Exception as e:
            results.append(("forum", f"error: {str(e)[:200]}"))
            logging.exception("check_new_forum_posts failed")

        # Build result message
        result_text = "🔍 Full check completed:\n\n"

        for check_type, result in results:
            if check_type == "messages":
                if result is True:
                    result_text += "📨 Checking messages...\n✅ New messages found!\n\n"
                elif "error" in str(result):
                    result_text += "📨 Checking messages...\n❌ Error\n\n"
                else:
                    result_text += "📨 Checking messages...\n☑️ No new messages found\n\n"

            elif check_type == "announcements":
                if result is True:
                    result_text += "📢 Checking announcements...\n✅ New announcements found!\n\n"
                elif "error" in str(result):
                    result_text += "📢 Checking announcements...\n❌ Error\n\n"
                else:
                    result_text += "📢 Checking announcements...\n☑️ No new announcements found\n\n"

            elif check_type == "files":
                if result is True:
                    result_text += "📁 Checking files...\n✅ New files found!\n\n"
                elif "error" in str(result):
                    result_text += "📁 Checking files...\n❌ Error\n\n"
                else:
                    result_text += "📁 Checking files...\n☑️ No new or updated files found\n\n"

            elif check_type == "forum":
                if result:
                    result_text += "💬 Checking forum posts...\n✅ New posts found!\n\n"
                elif "error" in str(result):
                    result_text += "💬 Checking forum posts...\n❌ Error\n\n"
                else:
                    result_text += "💬 Checking forum posts...\n☑️ No new posts found\n\n"

        # Update status message with results
        await status_msg.edit_text(result_text)

        # 🚀 4️⃣ Schedule file watcher silently AFTER initial full check
        async def _delayed_watcher_start():
            await asyncio.sleep(30)  # start 30 seconds after full check completes
            if chat_id not in watch_tasks or watch_tasks[chat_id].done():
                task = asyncio.create_task(watch_loop(chat_id, context))
                watch_tasks[chat_id] = task
                logging.info(f"👀 File watcher started silently for chat_id={chat_id}")

        asyncio.create_task(_delayed_watcher_start())

        # 🕒 Full check timestamp
        global_scan_count += 1
        last_full_check_time = datetime.now()

        # ✅ Done
        await update.message.reply_text(
            "📌 Quick Access:",
            reply_markup=get_show_last_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Fatal error:\n{str(e)[:500]}")
        logging.exception("_run_check_now crashed")

    finally:
        check_in_progress.discard(chat_id)
        global_watcher_paused = False  # ✅ Resume global watchers


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
    global page, browser_context

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
        files = await list_files(page, cid, root_url)
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

        files = await list_files(page, cid, folder_url)
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
                cookies = await browser_context.cookies()
                cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                timeout = aiohttp.ClientTimeout(total=120)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(temp_url, headers={"Cookie": cookie_header}) as resp:
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

        url = await get_fresh_file_url(page, cid, fname, current_url)
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
            cookies = await browser_context.cookies()
            cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            timeout = aiohttp.ClientTimeout(total=120)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"Cookie": cookie_header}) as resp:
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
        cid = info.get("cid")
        action = info.get("action")

        if action == "back":
            if len(nav_stack.get(user_id, [])) > 1:
                nav_stack[user_id].pop()
                nav_names[user_id].pop()

            if nav_stack.get(user_id):
                back_url = nav_stack[user_id][-1]
                files = await list_files(page, cid, back_url)
                await send_folder(query, files, cid, user_id, current_url=back_url)
            else:
                # Fallback: course root'a git
                root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
                nav_stack[user_id] = [root_url]
                nav_names[user_id] = [courses_map.get(cid, f"Course {cid[:6]}")]
                files = await list_files(page, cid, root_url)
                await send_folder(query, files, cid, user_id, current_url=root_url)

        elif action == "home":
            root_url = f"https://elearning.uni-oldenburg.de/dispatch.php/course/files?cid={cid}"
            nav_stack[user_id] = [root_url]
            nav_names[user_id] = [courses_map.get(cid, f"Course {cid[:6]}")]
            files = await list_files(page, cid, root_url)
            await send_folder(query, files, cid, user_id, current_url=root_url)

        elif action == "courses":
            async with page_lock:
                await page.goto(STUDIP_URL, wait_until="networkidle")
            courses = await list_courses(page)
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
                page, cid, current_url, browser_context, root_name=root_name, progress_callback=progress
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

        try:
            logging.info(f"📨 Opening message ID {msg_id} via direct URL...")

            msg_url = f"https://elearning.uni-oldenburg.de/dispatch.php/messages/read/{msg_id}/rec"
            await page.goto(msg_url, wait_until="networkidle", timeout=20000)

            html = await page.content()

            # Extract information
            subject_match = re.search(r'<span id="ui-id-7".*?>(.*?)</span>', html, re.S)
            subject = re.sub(r"<.*?>", "", subject_match.group(1)).strip() if subject_match else "(No subject)"

            sender_match = re.search(r'<td><strong>From</strong></td>\s*<td>(.*?)</td>', html, re.S)
            sender = re.sub(r"<.*?>", "", sender_match.group(1)).strip() if sender_match else "Unknown"

            date_match = re.search(r'<td><strong>Date</strong></td>\s*<td>(.*?)</td>', html, re.S)
            date_str = re.sub(r"<.*?>", "", date_match.group(1)).strip() if date_match else "-"

            body_match = re.search(r'<div class="formatted-content ck-content">(.*?)</div>', html, re.S)
            content = re.sub(r"<.*?>", "", body_match.group(1)).strip() if body_match else "No content found."

            # Send to Telegram
            header = (
                "✉️ <b>FULL MESSAGE</b>\n"
                "                  \n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📬 <b>Subject:</b> {subject}\n"
                f"👤 <b>From:</b> {sender}\n"
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
            await query.message.reply_text(f"⚠️ Could not load message content:\n{e}")

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
            data = [text.replace("📅", "").strip()]
        chat_id = message.chat_id
        sender = message

    global page, browser_context
    user_id = update.effective_user.id
    if not is_user_allowed(user_id):
        await sender.reply_text("Not authorized to use this bot.")
        return

    try:
        # Temporary loading message
        status_msg = await sender.reply_text("⏳ Please wait...")

        # Handle menu command
        if data[0] == "menu":
            await menu_command(update, context)

        # Handle calendar commands - Show Today view and add buttons
        elif data[0] == "calendar":
            logging.info("📅 Opening Planer page and fetching today's events...")
            events = await get_calendar_events(page, week_view=False)

            # Create buttons
            keyboard = [
                [InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
                 InlineKeyboardButton("🗓️ Weekly Plan", callback_data="calendar_weekly")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await send_today_calendar(sender, events)
            await sender.reply_text("📌 Quick Access:", reply_markup=reply_markup)

        # Handle start command
        elif data[0] == "start":
            await start(update, context)

        # Handle check command
        elif data[0] == "check":
            await check_command(update, context)

        # Handle status command
        elif data[0] == "status":
            await status_command(update, context)

        else:
            await sender.reply_text("❓ Unknown command.")

        # Safely delete loading message
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception as e:
            logging.warning(f"⚠️ Could not delete status message: {e}")

    except Exception as e:
        logging.error(f"Button handler error: {e}")
        try:
            await sender.reply_text(f"⚠️ An error occurred:\n`{e}`", parse_mode="Markdown")
        except Exception:
            pass


async def handle_calendar_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_today callback with better error handling"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    try:
        await query.edit_message_text("📅 Fetching today's schedule...")

        # Page check
        if not page or page.is_closed():
            await query.edit_message_text("🔄 Browser session expired, reconnecting...")
            await login_studip()

        events = await get_calendar_events(page, week_view=False)

        # Create buttons
        keyboard = [
            [InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
             InlineKeyboardButton("🗓️ Weekly Plan", callback_data="calendar_weekly")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await send_today_calendar(query, events)
        await query.message.reply_text("📌 Quick Access:", reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Calendar today error: {e}")
        error_msg = f"❌ Error loading calendar:\n{str(e)[:200]}"

        # User-friendly error message
        if "ERR_ABORTED" in str(e) or "net::" in str(e):
            error_msg = "❌ Network error loading calendar. Please try again in a moment."

        await query.edit_message_text(error_msg)


async def handle_calendar_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar_weekly callback with better error handling"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        await query.message.reply_text("Not authorized to use this bot.")
        return

    try:
        await query.edit_message_text("🗓️ Fetching weekly schedule...")

        # Page check
        if not page or page.is_closed():
            await query.edit_message_text("🔄 Browser session expired, reconnecting...")
            await login_studip()

        events = await get_calendar_events(page, week_view=True)

        # Create buttons
        keyboard = [
            [InlineKeyboardButton("📅 Today", callback_data="calendar_today"),
             InlineKeyboardButton("🗓️ Weekly Plan", callback_data="calendar_weekly")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await send_weekly_calendar(query, events)
        await query.message.reply_text("📌 Quick Access:", reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Calendar weekly error: {e}")
        error_msg = f"❌ Error loading weekly calendar:\n{str(e)[:200]}"

        # User-friendly error message
        if "ERR_ABORTED" in str(e) or "net::" in str(e):
            error_msg = "❌ Network error loading calendar. Please try again in a moment."

        await query.edit_message_text(error_msg)


async def delete_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete any free text that isn't a valid keyboard command."""
    try:
        if not update.message or not update.message.text:
            return
        text = _normalize_button_text(update.message.text)
        if text.startswith("▶️") or text.lower().startswith("start") or text.startswith("🔁") or "check" in text.lower():
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
            ["🍽️ Menu", "📅 Calendar"]  # Calendar button should be here
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
        if page and not page.is_closed():
            async with page_lock:
                await page.goto(STUDIP_URL, wait_until="networkidle")
            courses = await list_courses(page)
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
    global page, browser_context, watcher_controller_running, global_watcher_paused, check_in_progress

    logged_in = "✅ Yes" if page and not page.is_closed() else "❌ No"

    # Watcher durumu
    if watcher_controller_running:
        watcher_status = "🟢 Running"
    else:
        watcher_status = "🔴 Stopped"

    global_paused = "✅ Yes" if global_watcher_paused else "❌ No"
    active_check = "✅ Yes" if check_in_progress else "❌ No"
    wa_group_name = os.getenv("WHATSAPP_GROUP_NAME", "StudIP Alerts")

    # Check WA Connection Status
    wa_status = "🔴 Offline / Not Connected"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:3838/status", timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    wa_status = "🟢 Connected" if data.get("isAuthenticated") else "🟡 Waiting for QR Scan"
    except Exception:
        pass

    text = (
        "━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🔑 Logged in: {logged_in}\n"
        f"👀 Unified Watcher: {watcher_status}\n"
        f"🔁 Global Scans: {global_scan_count}\n"
        f"📱 WA Group: {wa_group_name}\n"
        f"💬 WA Status: {wa_status}"
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
        sys_info += "\n\n⚠️ <b>High memory usage detected — bot restart recommended.</b>"

    # Last full check time
    if "last_full_check_time" in globals() and last_full_check_time:
        since = datetime.now() - last_full_check_time
        hours, remainder = divmod(int(since.total_seconds()), 3600)
        minutes = remainder // 60
        text += f"\n\n🕓 Last Full Check: {last_full_check_time.strftime('%d %b %Y %H:%M')} ({hours}h {minutes}m ago)"
    else:
        text += "\n\n🕓 Last Full Check: —"
        
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
        pg = await login_studip()
        await pg.goto(STUDIP_URL, wait_until="networkidle")
        courses = await list_courses(pg)

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
    import asyncio
    import nest_asyncio

    logging.info("Telegram bot running (full Stud.IP automation + watcher + ZIP)...")

    if not acquire_instance_lock():
        logging.error("❌ Another bot instance is already running. Exiting.")
        return

    try:
        # Apply nest_asyncio for Jupyter/async environment compatibility
        nest_asyncio.apply()
        loop = asyncio.get_event_loop()

        # Build Telegram application
        app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .build()
        )

        # ── Command handlers ──────────────────────────────────────────────────

        # ── Command handlers ──────────────────────────────────────────────────

        # 1. FIRST specific callback patterns
        app.add_handler(CallbackQueryHandler(show_last_messages, pattern="^show_last_messages$"))
        app.add_handler(CallbackQueryHandler(forward_to_whatsapp, pattern="^forward_wa$"))
        app.add_handler(CallbackQueryHandler(show_last_announcements, pattern="^show_last_announcements$"))
        app.add_handler(CallbackQueryHandler(show_last_files, pattern="^show_last_files$"))
        app.add_handler(CallbackQueryHandler(show_last_forum_posts, pattern="^show_last_forum_posts$"))
        app.add_handler(CallbackQueryHandler(menu_button_handler, pattern="^show_menu$"))
        app.add_handler(CallbackQueryHandler(handle_status_buttons, pattern="^(start_watchers|stop_watchers|request_wa_qr|change_wa_group|change_ical_link)$"))

        # 2. SPECIAL callback patterns (calendar buttons)
        app.add_handler(CallbackQueryHandler(handle_calendar_today, pattern="^calendar_today$"))
        app.add_handler(CallbackQueryHandler(handle_calendar_weekly, pattern="^calendar_weekly$"))

        # 3. GENEL callback handler - en SONA
        app.add_handler(CallbackQueryHandler(handle_selection))

        # 4. Command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("check", check_command))
        app.add_handler(CommandHandler("watch", watch))
        app.add_handler(CommandHandler("status", status_command))
        app.add_handler(CommandHandler("menu", menu_command))

        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, handle_settings_reply))

        # 5. Message handlers - FIRST handle_reply_buttons, THEN delete_text
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_buttons))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, delete_text))

        async def initialize_browser():
            """Initialize browser before starting background services"""
            global page, browser_context
            logging.info("🔄 Initializing browser...")

            try:
                if not page or page.is_closed():
                    page = await login_studip()
                    browser_context = page.context

                    # Load courses
                    await page.goto(STUDIP_URL, wait_until="networkidle")
                    await list_courses(page)
                    logging.info(f"✅ Browser initialized with {len(courses_map)} courses")
                else:
                    logging.info("✅ Browser already initialized")

            except Exception as e:
                logging.error(f"❌ Browser initialization failed: {e}")
                raise

        async def browser_auto_restart_loop():
            """Browser auto-restart loop (every 4 hours)"""
            global playwright, browser, browser_context, page

            logging.info("🟢 Browser auto-restart loop INITIALIZED")

            # Schedule first restart for 4 hours later
            await asyncio.sleep(BROWSER_RESTART_INTERVAL)

            while True:
                try:
                    # Correct condition: restart if browser exists AND is connected
                    if browser and browser.is_connected():
                        logging.info("♻️ Browser restart triggered to prevent RAM growth...")

                        # 1️⃣ Save cookies
                        cookies = await browser_context.cookies()
                        logging.info(f"💾 Saving {len(cookies)} cookies before restart...")

                        # 2 Safely close old browser
                        try:
                            if browser_context:
                                await browser_context.close()
                            if browser:
                                await browser.close()
                            if playwright:
                                await playwright.stop()
                        except Exception as e:
                            logging.warning(f"Browser cleanup warning: {e}")

                        # 3 Start new browser
                        playwright = await async_playwright().start()
                        headless = os.getenv("HEADLESS", "false").lower() != "false"
                        browser = await playwright.chromium.launch(headless=headless)
                        browser_context = await browser.new_context()
                        page = await browser_context.new_page()

                        # 4️⃣ Perform login
                        await login_studip()

                        # 5 Reload courses
                        await list_courses(page)

                        logging.info("✅ Browser successfully restarted and restored")
                    else:
                        logging.warning("🔴 Browser not connected or not available, skipping restart")

                    # Wait for next restart
                    await asyncio.sleep(BROWSER_RESTART_INTERVAL)

                except Exception as e:
                    logging.error(f"❌ Browser auto-restart failed: {e}")
                    # Wait 30 minutes on error and retry
                    await asyncio.sleep(1800)

        async def link_cache_cleanup_loop():
            """Link cache cleanup loop (saatte bir)"""
            logging.info("🟢 Link cache cleanup loop STARTED")
            while True:
                try:
                    await asyncio.sleep(3600)  # Wait 1 hour
                    cleanup_link_cache()
                    logging.info("🧹 Link cache cleaned up")
                except Exception as e:
                    logging.error(f"Link cache cleanup error: {e}")
                    await asyncio.sleep(3600)

        async def monitor_tasks():
            """Monitor and restart failed background tasks"""
            logging.info("🟢 Task monitor STARTED")

            task_map = {
                "message_watcher": global_message_watcher,
                "announcement_watcher": global_announcement_watcher,
                "browser_restart": browser_auto_restart_loop,
                "cache_cleanup": link_cache_cleanup_loop,
            }

            background_tasks = {}

            # Start initial tasks
            for name, coro_func in task_map.items():
                task = asyncio.create_task(coro_func(), name=name)
                background_tasks[name] = task
                logging.info(f"✅ Started background task: {name}")

            await asyncio.sleep(10)  # Short wait for initial startup

            while True:
                try:
                    for name, task in list(background_tasks.items()):
                        if task.done():
                            logging.error(f"❌ Background task {name} died, restarting...")

                            # Check exception
                            try:
                                exception = task.exception()
                                if exception:
                                    logging.error(f"Task {name} exception: {exception}")
                            except (asyncio.InvalidStateError, CancelledError):
                                pass

                            # Start new task
                            new_task = asyncio.create_task(task_map[name](), name=name)
                            background_tasks[name] = new_task
                            logging.info(f"✅ Restarted task: {name}")

                    await asyncio.sleep(30)  # Check every 30 seconds

                except Exception as e:
                    logging.error(f"Task monitor error: {e}")
                    await asyncio.sleep(60)

        async def run_all():
            """Main application runner"""
            try:
                logging.info("🚀 Starting bot initialization...")

                # ✅ Browser initialization with error handling
                try:
                    # Check if browser needs initialization
                    if not page or page.is_closed():
                        logging.info("🔄 Initializing browser...")
                        await login_studip()
                        logging.info("✅ Browser initialized successfully")
                    else:
                        logging.info("✅ Browser already initialized")
                except Exception as e:
                    logging.warning(f"⚠️ Browser initialization warning: {e}")
                    logging.info("🔄 Continuing without browser initialization...")

                # Start unified watcher
                logging.info("🚀 Starting unified watcher...")
                await start_unified_watcher(app)

                logging.info("✅ All background services started successfully")
                logging.info("🤖 Starting Telegram bot polling...")

                # Start Telegram bot
                app.run_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                    timeout=60,
                    close_loop=False
                )

            except Exception as e:
                logging.error(f"❌ Error in run_all: {e}")
                raise

        # First start browser, then run main loop
        logging.info("🚀 Starting bot initialization...")

        # Browser initialization and start main loop
        loop.run_until_complete(run_all())

    except KeyboardInterrupt:
        logging.info("🛑 Bot stopped by user (Ctrl+C)")
    except Exception as e:
        logging.error(f"❌ Fatal error in main: {e}")
        import traceback
        logging.error(traceback.format_exc())
    finally:
        # 🔒 Cleanup
        logging.info("🧹 Cleaning up resources...")
        try:
            # Collect all asyncio tasks and cancel them
            # Stop unified watcher
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

            for task in tasks:
                task.cancel()

            # Wait for tasks to be cancelled
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))

            # Close browser
            if browser_context:
                loop.run_until_complete(browser_context.close())
            if browser:
                loop.run_until_complete(browser.close())
            if playwright:
                loop.run_until_complete(playwright.stop())

            logging.info("✅ Resources cleaned up successfully")

        except Exception as e:
            logging.warning(f"Cleanup warning: {e}")

        finally:
            # Release instance lock
            release_instance_lock()
            logging.info("🔓 Instance lock released")
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
        # Start node directly instead of npm via shell to avoid zombie processes on restart
        wa_process = subprocess.Popen(["node", "server.js"], cwd=wa_service_dir)
        
        def cleanup_wa():
            logging.info("Stopping WhatsApp microservice...")
            wa_process.terminate()
            try:
                wa_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                wa_process.kill()
            
        atexit.register(cleanup_wa)

    asyncio.run(main())  # Correctly running async function
