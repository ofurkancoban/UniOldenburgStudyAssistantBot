"""Text, HTML, date and emoji helpers for building messages.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import hashlib
import html
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup


TELEGRAM_HTML_LIMIT = 4000  # under Telegram's 4096 characters per message
_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z-]+)[^>]*>")


def _telegram_length(text: str) -> int:
    """Count UTF-16 code units, the length Telegram uses for message limits."""
    return len(text.encode("utf-16-le")) // 2


def _prefix_by_telegram_length(text: str, limit: int) -> str:
    size = 0
    end = 0
    for end, char in enumerate(text, start=1):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            return text[:end - 1]
        size += units
    return text[:end] if text else ""


def truncate_html(text: str, limit: int = TELEGRAM_HTML_LIMIT, marker: str = "…") -> str:
    """Telegram HTML cut to at most `limit` characters without breaking it:
    never inside a tag or an entity (&amp;), and every tag still open at the
    cut is closed again (a blind text[:limit] can make Telegram reject the
    whole message)."""
    if _telegram_length(text) <= limit:
        return text
    marker_length = _telegram_length(marker)
    cut = _prefix_by_telegram_length(text, max(0, limit - marker_length - 40))
    cut = re.sub(r"<[^>]*$", "", cut)              # a tag cut in half
    cut = re.sub(r"&#?[a-zA-Z0-9]*$", "", cut)     # an entity cut in half
    open_tags: list[str] = []
    for closing, name in _HTML_TAG_RE.findall(cut):
        name = name.lower()
        if not closing:
            open_tags.append(name)
        elif name in open_tags:
            del open_tags[len(open_tags) - 1 - open_tags[::-1].index(name)]
    return cut + marker + "".join(f"</{name}>" for name in reversed(open_tags))


def escaped_chunks(text: str, limit: int) -> list[str]:
    """`text` HTML-escaped and cut into parts that are each at most `limit`
    characters once escaped (escaping makes '&' five characters and '<' four,
    so cutting the plain text first can overshoot Telegram's limit)."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for ch in text:
        piece = html.escape(ch, quote=False)
        piece_size = _telegram_length(piece)
        if size + piece_size > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += piece_size
    if current:
        parts.append("".join(current))
    return parts


def split_html(text: str, limit: int = 3800) -> list[str]:
    """Telegram HTML cut into messages of at most `limit` characters. A cut
    falls between tags, preferably at a line break; a tag still open there is
    closed and opened again at the start of the next part, so every part is
    valid on its own. Text longer than the limit is cut inside the text,
    never inside a tag or an entity. Lengths use UTF-16 units as Telegram does."""
    tokens: list[str] = []
    text_limit = max(1, limit - 100)  # leave space for tags reopened across a split
    for part in re.split(r"(<[^>]*>)", text):
        if not part:
            continue
        if part.startswith("<") and part.endswith(">"):
            tokens.append(part)
            continue
        for line in part.splitlines(keepends=True):
            while _telegram_length(line) > text_limit:
                head = _prefix_by_telegram_length(line, text_limit)
                safe_head = re.sub(r"&#?[a-zA-Z0-9]*$", "", head)
                if safe_head:
                    head = safe_head
                tokens.append(head)
                line = line[len(head):]
            tokens.append(line)

    parts: list[str] = []
    open_tags: list[tuple[str, str]] = []  # (name, opening tag)
    current = ""

    def closing() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def flush() -> None:
        nonlocal current
        if current.strip():
            parts.append(current.rstrip("\n") + closing())
        current = "".join(tag for _, tag in open_tags)

    for token in tokens:
        if (_telegram_length(current) + _telegram_length(token)
                + _telegram_length(closing()) + 20 > limit):
            flush()
        current += token
        m = re.fullmatch(r"<(/?)([a-zA-Z-]+)[^>]*>", token)
        if m:
            name = m.group(2).lower()
            if m.group(1):
                for i in range(len(open_tags) - 1, -1, -1):
                    if open_tags[i][0] == name:
                        del open_tags[i]
                        break
            else:
                open_tags.append((name, token))
        elif token.endswith("\n") and _telegram_length(current) > limit * 0.8:
            flush()  # prefer a cut at a line break
    if current.strip():
        parts.append(current.rstrip("\n"))
    return parts


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
    
    # We use the element directly, assuming we don't need it pristine afterwards
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
    
    # Clean up excessive newlines (more than 2 to just 2)
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
    
    res: dict[str, Any] = {}
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
            logging.debug("HTML tooltip parsing failed: %s", e)
    
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
        logging.debug("Text tooltip parsing failed: %s", e)

    return res


def _status_emoji(status: str) -> str:
    """Return an emoji based on the event status."""
    return {"ongoing": "⏳", "upcoming": "⏰", "past": "✅", "cancelled": "🚫"}.get(status, "⚪")


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


# ── forum checking ─────────────────────────────────────────────────────────────

def _truncate_with_ellipsis(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters, appending "…" only when it
    actually got cut - plain slicing with no marker looks indistinguishable
    from a parsing bug (text just stopping mid-sentence)."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


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
    """Remove course type (e.g. Vorlesung:) and course code (e.g. 2.02.861 or 2.02.681a)."""
    if not raw_title:
        return ""
    rest = raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title.strip()
    match = re.match(r"^\d+(?:\.\d+)+[a-zA-Z]?\s+(.*)$", rest)
    if match:
        return match.group(1).strip()
    return rest


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


def _format_relative(dt: datetime, now: datetime, show_time: bool = True) -> str:
    """Render a datetime as a short relative label: 'today [HH:MM]', 'tomorrow
    [HH:MM]', 'in Nd [HH:MM]', or 'overdue'. The clock time is only appended
    when it's meaningfully set (not midnight, i.e. a real reminder/exam time
    is known) and `show_time` is True - registration deadlines all close at
    23:59, so that time is never worth showing there."""
    days = (dt.date() - now.date()).days
    has_time = show_time and dt.time() != datetime.min.time()
    time_suffix = f" {dt.strftime('%H:%M')}" if has_time else ""
    if days < 0:
        return "overdue"
    if days == 0:
        return f"today{time_suffix}"
    if days == 1:
        return f"tomorrow{time_suffix}"
    return f"in {days}d{time_suffix}"


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "▓" * filled + "░" * (width - filled)


def _hours(minutes: float) -> str:
    return f"{minutes / 60:.1f} h"


def _course_abbrev(name: str) -> str:
    """"Econometrics of Policy Evaluation" -> "EPE", to keep plan lines short."""
    words = [w for w in re.findall(r"[A-Za-zÄÖÜäöü]+", name) if w.lower() not in ("of", "and", "the", "for", "in", "und", "der", "die", "das")]
    return "".join(w[0].upper() for w in words)[:5] or name[:5]
