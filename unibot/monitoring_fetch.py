"""Fetch layer of the course watchers: Stud.IP API and HTML reads for
announcements, messages and forum threads, and the parsing of what comes back.
Nothing here keeps state or sends a message (see unibot.monitoring)."""

import html
import logging
import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from studip_utils import TZ_BERLIN, berlin_now, redact_url, tag_attr
from unibot.formatting import (
    _truncate_with_ellipsis,
    clean_html_text,
)
from unibot.studip_api import (
    BASE_URL,
    _normalize_url,
    _api_berlin,
    _api_html_to_text,
    _api_message,
    _api_names,
    _api_rel_id,
    STUDIP_API_PAGE_LIMIT,
    studip_api_get,
    studip_api_get_all,
    studip_api_user_id,
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
        except (ValueError, TypeError):
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
    except (ValueError, IndexError):
        pass
    return datetime.min


async def _course_announcements_api(cid, course_name, session=None) -> list:
    """The course's currently published announcements from the JSON:API, in
    the same shape (and with the same "cid:news_id" key) as the ones scraped
    from the course overview page."""
    news, included = await studip_api_get_all(f"/courses/{cid}/news", {"include": "author"}, session=session)
    names = _api_names(included)
    now = datetime.now(TZ_BERLIN)
    result = []
    for n in news:
        a = n["attributes"]
        start, end = _api_berlin(a.get("publication-start")), _api_berlin(a.get("publication-end"))
        if (start and start > now) or (end and end < now):
            continue
        dt = _api_berlin(a.get("publication-start") or a.get("mkdate"))
        date_s = dt.strftime("%d/%m/%y") if dt else ""
        result.append({
            "key": f"{cid}:{n['id']}", "cid": cid, "course": course_name,
            "subject": a.get("title") or "(No subject)",
            "sender": names.get(_api_rel_id(n, "author"), "Unknown"),
            "date": date_s, "dt": _parse_ann_date(date_s).isoformat(),
            "body": _api_html_to_text(a.get("content")) or "(No content)",
        })
    return result


async def fetch_message_body(session, message_url):
    """Fetch message body from detail page with improved parsing."""
    safe_url = _normalize_url(message_url)
    if not safe_url:
        logging.warning("Rejected untrusted message detail URL: %s", redact_url(message_url))
        return "[Invalid message URL]"
    try:
        async with await session.get(safe_url) as resp:
            if resp.status != 200:
                return f"[HTTP Error: {resp.status}]"
            html = await resp.text()

        # lxml, not html.parser - see the comment in check_new_announcements_parallel
        # on the same inline-SVG-icon parse-tree corruption bug.
        soup = BeautifulSoup(html, "lxml")

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
        logging.error("fetch_message_body failed for %s: %s", redact_url(message_url), e)
        return f"[Error fetching content: {str(e)[:100]}]"


# The first inbox check establishes a baseline from one page. Later checks
# walk older pages until they reach a message already in the user's cache.
MESSAGE_CHECK_LIMIT = STUDIP_API_PAGE_LIMIT


async def _fetch_messages_api(limit: int = MESSAGE_CHECK_LIMIT, session=None,
                              known_ids: Optional[set[str]] = None) -> list:
    """Fetch messages newest first, paging back to the cached inbox boundary.

    With no cache, one page is enough to establish a baseline. With a cache,
    keep paging until a known message is reached so a busy inbox cannot hide
    messages beyond an arbitrary newest-N window.
    """
    uid = await studip_api_user_id(session=session)
    path = f"/users/{uid}/inbox"
    page_limit = max(1, min(int(limit), STUDIP_API_PAGE_LIMIT))
    offset = 0
    known = {str(message_id) for message_id in known_ids} if known_ids is not None else None
    messages = []
    fingerprints = set()
    while True:
        page = await studip_api_get(path, {
            "page[limit]": page_limit, "page[offset]": offset, "include": "sender",
        }, session=session)
        data = page.get("data", [])
        if not isinstance(data, list):
            raise ValueError("inbox API page data is not a list")
        if not data:
            break
        fingerprint = tuple(str(item.get("id")) for item in data if isinstance(item, dict))
        if fingerprint in fingerprints:
            raise ValueError(f"inbox API repeated a page at offset {offset}")
        fingerprints.add(fingerprint)
        names = _api_names(page.get("included", []))
        found_known = False
        for message in data:
            if not isinstance(message, dict) or message.get("id") is None:
                raise ValueError("inbox API returned a malformed message")
            if known is not None and str(message["id"]) in known:
                found_known = True
                break
            messages.append(_api_message(message, names))
        if known is None or found_known or len(data) < page_limit:
            break
        offset += page_limit
    return messages


async def _fetch_messages_html(session) -> list:
    """Overview-page rows (no bodies), the fallback when the API fails."""
    url = f"{BASE_URL}/dispatch.php/messages/overview"
    async with await session.get(url) as resp:
        html_text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"inbox overview answered HTTP {resp.status}")
    if "loginform" in html_text.lower():
        raise RuntimeError("inbox overview returned the login form")

    # lxml, not html.parser: see the comment in check_new_announcements_parallel
    # on the same inline-SVG-icon parse-tree corruption bug.
    soup = BeautifulSoup(html_text, "lxml")
    rows = soup.select("table#messages tbody tr[id^='message_']")
    all_messages = []
    for tr in rows:
        link_tag = tr.select_one("td.title a[href*='dispatch.php/messages/read/']")
        if not link_tag: continue
        href = tag_attr(link_tag, "href").strip()
        title = link_tag.get_text(strip=True)
        sender_tag = tr.select_one("td:nth-of-type(3)")
        sender = sender_tag.get_text(strip=True) if sender_tag else "Unknown"
        date_tag = tr.select_one("td:nth-of-type(4)")
        date = date_tag.get_text(strip=True) if date_tag else "Unknown"

        if not href.startswith('http'):
            href = f"{BASE_URL}/{href.lstrip('/')}"

        all_messages.append({
            "id": tag_attr(tr, "id").replace("message_", ""), "title": title, "sender": sender, "date": date, "url": href,
        })
    return all_messages


JSONAPI_HEADERS = {"Accept": "application/vnd.api+json"}


def _forum_author_name(posts_data: dict, author_id) -> str:
    """Author name from the postings response's included forum members."""
    if author_id and "included" in posts_data:
        for inc in posts_data["included"]:
            if inc.get("type") == "forum-members" and inc.get("id") == author_id:
                return inc.get("attributes", {}).get("name", "Unknown")
    return "Unknown"


def _friendly_forum_date(iso_str) -> str:
    if not iso_str:
        return "Unknown date"
    parsed = _api_berlin(iso_str)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else str(iso_str)


def _post_author_id(post: dict):
    return post.get("relationships", {}).get("author", {}).get("data", {}).get("id")


FORUM_OMITTED = "<i>... (older messages omitted) ...</i>"


def _html_to_text(content_html: str) -> str:
    return BeautifulSoup(content_html, "html.parser").get_text(separator=" ", strip=True)


def _forum_thread_entry(course_name: str, cid: str, disc: dict, posts_data: dict) -> Optional[dict]:
    """Cache/notification entry for one discussion and its postings, or None
    when the discussion has no postings."""
    posts = posts_data.get("data", [])
    if not posts:
        return None
    title = disc.get("attributes", {}).get("title", "Unknown")
    disc_id = disc.get("id")

    # The whole conversation for context, at most the last 15 messages so
    # the message stays under Telegram's 4096 character limit.
    history = []
    for post in posts:
        attrs = post.get("attributes", {})
        p_html = attrs.get("content-html", "")
        p_body = _truncate_with_ellipsis(_html_to_text(p_html), 400) if p_html else "No content"
        p_author = _forum_author_name(posts_data, _post_author_id(post))
        history.append(f"👤 <b>{html.escape(p_author)}</b> <i>({_friendly_forum_date(attrs.get('mkdate', ''))})</i>:\n{html.escape(p_body)}")
    if len(history) > 15:
        history = [FORUM_OMITTED] + history[-15:]

    last_post = posts[-1]
    last_attrs = last_post.get("attributes", {})
    raw_timestamp = last_attrs.get("mkdate") or berlin_now().isoformat()
    parsed_timestamp = _api_berlin(raw_timestamp)
    time_str = parsed_timestamp.strftime("%Y-%m-%d %H:%M") if parsed_timestamp else _friendly_forum_date(raw_timestamp)
    last_content_html = last_attrs.get("content-html", "")
    last_message_plain = _html_to_text(last_content_html) if last_content_html else ""
    if len(history) > 1:
        final_body = "💬 <b>Conversation History:</b>\n\n" + "\n\n".join(history)
    else:
        body = _truncate_with_ellipsis(last_message_plain, 3000) if last_message_plain else "No content"
        final_body = f"📩 <b>Last message:</b>\n{html.escape(body)}"

    return {
        "cid": cid,
        "course": course_name,
        "thread": title,
        "author": _forum_author_name(posts_data, _post_author_id(last_post)),
        "date": time_str,
        "body": final_body,
        "last_message_plain": last_message_plain,
        # Stable per-post identifier lets the watcher distinguish a new reply
        # in an existing discussion from a discussion it already cached.
        "last_post_id": last_post.get("id"),
        "key": f"{cid}:{title}:{disc_id}",
        # A thread counts as new when Stud.IP reports unread postings
        "is_new": disc.get("meta", {}).get("unread-postings-count", 0) > 0,
        # Keep full precision for ordering and legacy-cache diffing; `date`
        # remains the shorter display form shown to students.
        "timestamp": parsed_timestamp.isoformat() if parsed_timestamp else time_str,
    }


async def _fetch_course_forum_strict(session, course_name: str, cid: str) -> list:
    """Entries for the 10 most recently updated discussions of one course.
    Raises when the session cannot read the course (expired login, no
    access), so the course registry can hand the scan to another member."""
    url = f"{BASE_URL}/jsonapi.php/v1/courses/{cid}/forum-discussions"
    async with await session.get(url, headers=JSONAPI_HEADERS) as r:
        if r.status != 200:
            raise RuntimeError(f"forum of {cid} answered HTTP {r.status}")
        data = await r.json()
    entries = []
    for disc in data.get("data", [])[:10]:
        posts_url = f"{BASE_URL}/jsonapi.php/v1/forum-discussions/{disc.get('id')}/postings?include=author"
        async with await session.get(posts_url, headers=JSONAPI_HEADERS) as r2:
            if r2.status != 200:
                raise RuntimeError(f"postings of discussion {disc.get('id')} answered HTTP {r2.status}")
            posts_data = await r2.json()
        entry = _forum_thread_entry(course_name, cid, disc, posts_data)
        if entry:
            entries.append(entry)
    return entries


async def _fetch_course_forum(session, course_name: str, cid: str) -> list:
    """_fetch_course_forum_strict, with every failure read as no entries."""
    try:
        return await _fetch_course_forum_strict(session, course_name, cid)
    except Exception as e:
        logging.warning("Error while checking forum API for %s: %s", course_name, e)
        return []
