"""Small dependency-free helpers shared by the bot modules.

Pure functions only (no bot state, no network), so they can be imported
anywhere and unit-tested without starting the bot.
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

TZ_BERLIN = ZoneInfo("Europe/Berlin")


# ── time ──────────────────────────────────────────────────────────────────────

def berlin_now() -> datetime:
    """Current Berlin wall-clock time as a naive datetime.

    Stud.IP/StuMS dates are parsed as naive Berlin-local values throughout,
    so comparisons need a naive "now" too, but one that doesn't depend on the
    server's timezone (datetime.now() would silently shift by 1-2 h, or a
    whole day near midnight, on a UTC host). Never call .timestamp() on it;
    use time.time() or an aware datetime for that."""
    return datetime.now(TZ_BERLIN).replace(tzinfo=None)


# ── files ─────────────────────────────────────────────────────────────────────

def atomic_write_json(path: str, data: Any, indent: Optional[int] = 2) -> None:
    """Write JSON so that readers only ever see the old or the new file.

    The data goes to a temp file in the same directory, is fsynced and then
    renamed over `path`. A crash or full disk mid-write leaves the previous
    version intact instead of a truncated file. Raises on failure."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def safe_filename(name: str) -> str:
    """Display/upload name for a file whose name comes from Stud.IP (anyone
    who can upload to a course picks it): no directories, no control
    characters, never empty."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    base = base.lstrip(".") or "file"
    return base[:200]


def safe_zip_path(path: str) -> str:
    """Folder path inside a ZIP built from Stud.IP folder names (no "..",
    no absolute paths), so extracting it can't escape the target folder."""
    parts = [safe_filename(p) for p in str(path or "").replace("\\", "/").split("/") if p.strip(" .")]
    return "/".join(parts) or "files"


# ── text ──────────────────────────────────────────────────────────────────────

def md_escape(text) -> str:
    """Escape free text for Telegram's legacy Markdown mode, where a stray
    `_`, `*`, backtick or `[` makes the whole message fail to send."""
    # Same set as telegram.helpers.escape_markdown(version=1); legacy
    # Markdown has no escape for the backslash itself.
    out = str(text or "")
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def html_escape(text) -> str:
    """Escape free text for Telegram's HTML parse mode (`&` and `<` break it)."""
    return html.escape(str(text or ""), quote=False)


def format_hours(minutes: int) -> str:
    hours = minutes / 60
    return f"{int(hours)}h" if hours == int(hours) else f"{hours:.1f}h"


def redact_url(url) -> str:
    """URL without query string or fragment, for logs: Stud.IP download and
    SSO links can carry tokens there."""
    try:
        parts = urlsplit(str(url))
        if not parts.scheme:
            return str(url).split("?", 1)[0]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return "<url>"
