"""Text extraction from course files (PDF/PPTX/HTML) and lecture summaries.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import os
import time
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ContextTypes

from unibot.config import (
    GWDG_API_KEY,
    GWDG_API_URL,
)
from unibot import state
from unibot.storage import link_cache, shared_http
from unibot.formatting import clean_course_title, truncate_html
from unibot.telegram_ui import _start_loading_animation, _stop_loading_animation
from unibot.auth import is_user_allowed, login_studip
from unibot.studip_api import get_fresh_file_url
from unibot.ai import (
    _call_openrouter,
    _gwdg_throttle,
    _record_gwdg_quota,
)


# ── Feature 9: 1-Minute Audio "Podcast" Summary for Course PDFs/Slides ────────

FILE_AUDIO_SUMMARY_SYSTEM_PROMPT = (
    "You are an expert academic tutor providing a concise, high-yield summary of a university lecture document (slides or notes). "
    "Your goal is to give the student a clear, intellectually solid understanding of what this document actually teaches.\n\n"
    "Guidelines:\n"
    "1. No podcast fluff or fake enthusiasm: Do NOT use podcast intros, hype, greetings, or sign-offs (e.g. avoid 'Hey everyone', 'Welcome back', 'Buckle up', 'Tune in next time'). Get straight to the subject.\n"
    "2. Filter out administrative noise: Completely ignore professor contact info, syllabus disclaimers, course logistics, copyright notices, and slide numbers.\n"
    "3. Focus on core substance: Identify the central topic and explain the 2 to 4 key concepts, theoretical models, mechanisms, or quantitative methods presented.\n"
    "4. Tone and structure: Professional, clear, engaging, and academic. Explain the intuition behind the concepts and why they matter in the context of the course.\n"
    "5. Format: Write in natural, smooth spoken English (130 to 170 words, about 60 seconds reading pace). Do NOT use markdown bold/italics, bullet points, asterisks, or numbered lists, as this will be read aloud via text-to-speech."
)


async def handle_file_audio_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle `file_audio_sum|<sid>` callback: extract text from the PDF/slide deck,
    generate a 1-minute spoken summary via LLM, and send it as a Telegram voice note."""
    from unibot.morning import _try_send_voice
    query = update.callback_query
    await query.answer("Generating 1-min audio summary...")

    user_id = query.from_user.id
    if not is_user_allowed(user_id):
        return

    parts = (query.data or "").split("|", 1)
    sid = parts[1].strip() if len(parts) > 1 else ""
    info = link_cache.get(sid, {})
    cid = info.get("cid")
    fname = info.get("name")
    course_name = info.get("course") or (state.courses_map.get(cid) if cid else "") or "Course"

    if not cid or not fname:
        await query.message.reply_text("⚠️ File reference expired. Please open the file list again.")
        return

    status_msg = await query.message.reply_text(
        f"🎧 <b>Preparing 1-Min Audio Summary...</b>\n📄 <i>{html.escape(fname)}</i>",
        parse_mode="HTML",
    )
    stop_evt, anim_task = _start_loading_animation(status_msg, f"Preparing 1-Min Audio Summary for {fname}...")

    try:
        session = await login_studip()
        file_url = info.get("url")
        content = None

        if file_url:
            try:
                async with await session.get(file_url) as r:
                    if r.status == 200:
                        content = await r.read()
            except Exception:
                logging.debug("File download failed", exc_info=True)
                content = None

        if not content:
            fresh_url = await get_fresh_file_url(cid, fname, info.get("current_url"), session=session, user_id=user_id)
            if fresh_url:
                info["url"] = fresh_url
                link_cache[sid] = {**info, "ts": time.time()}
                async with await session.get(fresh_url) as r:
                    if r.status == 200:
                        content = await r.read()

        if not content:
            await _stop_loading_animation(stop_evt, anim_task)
            await status_msg.edit_text(f"⚠️ Could not download <b>{html.escape(fname)}</b> from Stud.IP.", parse_mode="HTML")
            return

        extracted = await extract_text_from_file_async(fname, content)
        if not extracted or len(extracted.strip()) < 40:
            await _stop_loading_animation(stop_evt, anim_task)
            await status_msg.edit_text(
                f"⚠️ Could not extract readable text from <b>{html.escape(fname)}</b>.",
                parse_mode="HTML",
            )
            return

        user_prompt = (
            f"Course: {clean_course_title(course_name)}\n"
            f"File: {fname}\n\n"
            f"Extracted document content:\n{extracted[:12000]}"
        )
        script = await _call_openrouter(
            FILE_AUDIO_SUMMARY_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.3,
            timeout=35,
            reasoning=False,
        )
        if not script:
            await _stop_loading_animation(stop_evt, anim_task)
            await status_msg.edit_text("⚠️ AI summary service is temporarily unavailable. Please try again shortly.")
            return

        clean_script = script.strip().strip('"')
    except Exception as e:
        # an expired session or a Stud.IP error must not leave the student
        # with a "Preparing..." message that never changes
        logging.warning("Audio summary for %s failed: %s", fname, e)
        await _stop_loading_animation(stop_evt, anim_task)
        await status_msg.edit_text(f"⚠️ Could not prepare the summary: {html.escape(str(e)[:200])}", parse_mode="HTML")
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    summary_card = (
        f"🎧 <b>1-Minute Study Summary</b>\n"
        f"📘 <b>{html.escape(clean_course_title(course_name))}</b>\n"
        f"📄 <i>{html.escape(fname)}</i>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"{html.escape(clean_script)}"
    )
    await status_msg.edit_text(truncate_html(summary_card), parse_mode="HTML")
    await _try_send_voice(query.message, clean_script)


# A scanned or handwritten PDF gives pypdf next to nothing (55 characters
# from 13 pages of handwritten exercise notes, where Docling's OCR read
# ~7,000); typed slides give it 400+ per page.
DOCLING_MIN_CHARS_PER_PAGE = 100


async def convert_with_docling(filename: str, content: bytes) -> Optional[str]:
    """Markdown text of a document via GWDG's Docling service (layout
    analysis + OCR), or None if GWDG is unset, rate limited or failed."""
    if not GWDG_API_KEY or time.monotonic() < state._gwdg_blocked_until:
        return None
    form = aiohttp.FormData()
    form.add_field("files", content, filename=filename)
    try:
        await _gwdg_throttle()
        async with shared_http() as session:
            async with session.post(
                f"{GWDG_API_URL}/documents/convert/file",
                headers={"Authorization": f"Bearer {GWDG_API_KEY}", "accept": "application/json"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                _record_gwdg_quota(resp.headers)
                if resp.status != 200:
                    logging.warning("Docling conversion failed for %s: HTTP %s", filename, resp.status)
                    return None
                data = await resp.json(content_type=None)
        return ((data.get("document") or {}).get("md_content") or "").strip() or None
    except Exception as e:
        logging.warning("Docling conversion failed for %s: %s: %s", filename, type(e).__name__, e)
        return None


async def extract_text_from_file_async(filename: str, content: bytes) -> Optional[str]:
    """extract_text_from_file, with GWDG Docling as the fallback for PDFs
    and PPTX files the local parsers get (almost) no text from, typically
    scans and handwritten notes. Typed files never use the quota."""
    # pypdf and python-pptx are pure Python and slow on big decks: keep them off the event loop
    text = await asyncio.to_thread(extract_text_from_file, filename, content)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".pdf", ".pptx"):
        return text
    pages = 1
    if ext == ".pdf":
        pages = await asyncio.to_thread(_pdf_page_count, content)
    if text and len(text.strip()) >= DOCLING_MIN_CHARS_PER_PAGE * pages:
        return text
    converted = await convert_with_docling(filename, content)
    if converted and len(converted) > len((text or "").strip()):
        logging.info("Docling read %s: %s -> %s characters", filename, len((text or '').strip()), len(converted))
        return converted
    return text


def _pdf_page_count(content: bytes) -> int:
    try:
        import pypdf
        return max(1, len(pypdf.PdfReader(io.BytesIO(content)).pages))
    except Exception:
        logging.debug("PDF page count failed", exc_info=True)
        return 1


def extract_text_from_file(filename: str, content: bytes) -> Optional[str]:
    """Extract plain text from a downloaded lecture file, for topic
    summarization (HTML covers reveal.js/Quarto slide decks). Any extension
    other than PDF/PPTX/HTML returns None
    immediately with no log noise - that's the routine case for a typical
    course's file mix (videos, images, zips, docx), not an error. Each
    branch is independently try/except-wrapped since corrupt, scanned, or
    oddly-encoded files are common enough in real lecture archives that
    this must fail open rather than blow up the whole run."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages = []
            for page in reader.pages:
                try:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
                except Exception:
                    logging.debug("PDF page text extraction failed", exc_info=True)
                    continue
            return "\n".join(pages) if pages else None
        except Exception as e:
            logging.warning("extract_text_from_file: PDF parse failed for %s: %s", filename, e)
            return None
    elif ext == ".pptx":
        try:
            import pptx
            prs = pptx.Presentation(io.BytesIO(content))
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and shape.text_frame.text:
                        parts.append(shape.text_frame.text)
            return "\n".join(parts) if parts else None
        except Exception as e:
            logging.warning("extract_text_from_file: PPTX parse failed for %s: %s", filename, e)
            return None
    elif ext == ".html":
        try:
            soup = BeautifulSoup(content.decode("utf-8", errors="ignore"), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
            return text or None
        except Exception as e:
            logging.warning("extract_text_from_file: HTML parse failed for %s: %s", filename, e)
            return None
    return None


LECTURE_FILE_TOPIC_SYSTEM_PROMPT = (
    "You are given raw extracted text from a university lecture file (slides or notes). "
    "Respond with a single concise line (max 10-12 words) naming the specific academic topic or model covered "
    "(e.g., 'Linear Regression and OLS Estimation' or 'Nash Equilibrium and Dynamic Games'). "
    "Ignore administrative text, syllabus notices, and course titles. "
    "Never return generic phrases like 'lecture slides', 'course overview', or 'chapter 1'. "
    "No markdown, no quotes, plain text only."
)


async def summarize_lecture_file(filename: str, extracted_text: str) -> Optional[str]:
    """Turn a lecture file's extracted text into a short topic label for
    the weekly study plan's "known topics per course" prompt input.
    Truncates to the first 6000 characters before calling the LLM -
    lecture slides front-load the agenda/outline, so this cheaply captures
    the topic without needing the whole deck; this is also the feature's
    only defense against unbounded prompt size, since no token-budget
    handling exists anywhere else in this codebase."""
    text = (extracted_text or "")[:6000].strip()
    if not text:
        return None
    raw = await _call_openrouter(
        LECTURE_FILE_TOPIC_SYSTEM_PROMPT,
        f"Filename: {filename}\n\nExtracted text:\n{text}",
        temperature=0,
    )
    return raw.strip() if raw else None
