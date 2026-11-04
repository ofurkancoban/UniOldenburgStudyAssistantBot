"""Course material index and search, workload estimation, weekly study plan and progress.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import html
import io
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import base64
import struct
from datetime import time as _dtime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from exam_reminder import get_registered_exam_schedule
from fast_enroll import get_course_type_map, get_open_courses, get_semester_options
from studip_utils import TZ_BERLIN

from unibot import db
from unibot.config import (
    GWDG_API_KEY,
    GWDG_API_URL,
    GWDG_EMBEDDING_MODEL,
)
from unibot import state
from unibot.storage import (
    get_scan_previous_semester_enabled,
    get_weekly_study_plan_enabled,
    load_study_plan_cache,
    save_study_plan_cache,
    set_weekly_study_plan_enabled,
    shared_http,
    single_flight,
)
from unibot.formatting import _course_abbrev, _hours, _progress_bar, clean_course_title
from unibot.telegram_ui import (
    _reply_or_edit,
    _show_typing,
    _split_message,
    _start_loading_animation,
    _stop_loading_animation,
)
from unibot.auth import is_user_allowed, login_studip
from unibot.studip_api import (
    _ensure_watched_courses_map,
    _parse_file_date_v2,
    _switch_semester_filter,
    list_courses,
    list_files,
)
from unibot.ai import (
    GWDG_BURST_WAIT_SECONDS,
    _gwdg_throttle,
    _record_gwdg_quota,
)
from unibot.documents import extract_text_from_file, extract_text_from_file_async, summarize_lecture_file
from unibot.schedule import STUDY_PLAN_EXAM_WINDOW_DAYS, get_calendar_events, schedule_study_sessions
from unibot.monitoring import render_weekly_study_plan_message
from unibot.exams import STUDY_PLAN_MAX_FILE_MB, STUDY_PLAN_SUPPORTED_EXTENSIONS, get_file_exam_events


async def handle_toggle_weekly_study_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stateless flip toggle, same template as handle_toggle_wardrobe_feature."""
    query = update.callback_query
    await query.answer()
    new_state = not get_weekly_study_plan_enabled()
    set_weekly_study_plan_enabled(new_state)
    await query.edit_message_text(
        f"📘 Weekly Study Plan is now {'✅ On' if new_state else '🔲 Off'}.\nRun /status to see the full menu again."
    )


STUDY_GAP_MIN_MINUTES = 90  # weekly study plan: shorter free windows aren't worth scheduling a session into


async def _render_open_courses_for_semester(sender, semester_id: str):
    """List a given semester's currently open-for-enrolment courses within the
    student's own degree programme. Shared by handle_course_semester_pick
    (explicit picker) and handle_browse_courses (when a default semester is
    set, skipping the picker).

    If "include previous semester" is toggled on (get_scan_previous_semester_enabled,
    see handle_toggle_prev_sem_scan), the semester right before this one is
    scanned too and merged in — a module can still have an open retake slot
    on a prior term's course instance. Off by default, since most of the
    time it's just doubling the fetch time for nothing."""
    # Explicitly clear the keyboard here: editing text alone leaves whatever
    # buttons were on the previous message (e.g. the prior semester's "Enroll"
    # rows) live and tappable during this network wait, which let an
    # impatient tap land on stale "Enroll" buttons and silently enroll in the
    # wrong course from the old list.
    scan_prev = get_scan_previous_semester_enabled()
    loading_text = "📚 Checking which courses are open for enrolment" + (" (this and the previous semester)" if scan_prev else "") + "... this may take a moment."
    status_msg = await _reply_or_edit(sender, loading_text, reply_markup=InlineKeyboardMarkup([]))
    await _show_typing(sender)
    stop_evt, anim_task = _start_loading_animation(status_msg, loading_text)

    try:
        session = await login_studip()
        semester_options = await get_semester_options(session)
        current_label = next((label for sid, label in semester_options if sid == semester_id), None)

        open_courses = await get_open_courses(session, semester_id=semester_id)
        # get_open_courses() only reads each component's own "unrestricted
        # access" badge on the module listing page — it doesn't know which of
        # those the student is already enrolled in, so that's cross-checked
        # here against the student's own course list before displaying.
        enrolled_cids = {cid for _, cid in await list_courses(semester_id=semester_id)}
        open_courses = [c for c in open_courses if c["sem_id"] not in enrolled_cids]

        prev_label = None
        if scan_prev:
            # get_semester_options() is newest-first, so the semester right
            # before this one (chronologically) is the NEXT entry in the list.
            idx = next((i for i, (sid, _) in enumerate(semester_options) if sid == semester_id), None)
            if idx is not None and idx + 1 < len(semester_options):
                prev_id, prev_label = semester_options[idx + 1]
                try:
                    prev_open = await get_open_courses(session, semester_id=prev_id)
                    prev_enrolled = {cid for _, cid in await list_courses(semester_id=prev_id)}
                    prev_open = [c for c in prev_open if c["sem_id"] not in prev_enrolled]
                    # Tag each item with its source semester's label, and only
                    # once there are two terms in play, so the grouping below
                    # can tell same-named modules from different semesters
                    # apart without cluttering the common single-semester case.
                    for c in open_courses:
                        c["_sem_label"] = current_label
                    for c in prev_open:
                        c["_sem_label"] = prev_label
                    open_courses = open_courses + prev_open
                except Exception as e:
                    # Best-effort - the current semester's results above are
                    # still shown even if the previous-semester scan fails.
                    logging.warning("_render_open_courses_for_semester: previous-semester scan failed: %s", e)
                    prev_label = None
                finally:
                    # The fetches above leave Stud.IP's shared session filter
                    # pinned to the previous semester - restore it to the one
                    # actually being browsed so it doesn't silently corrupt
                    # some unrelated later no-arg list_courses() call.
                    await _switch_semester_filter(session, semester_id)
    except Exception as e:
        # Stop the animation before this edits the same status_msg with the
        # error — otherwise a late animation tick can race with (or land
        # after) this edit and clobber it back to a loading frame.
        await _stop_loading_animation(stop_evt, anim_task)
        logging.error("_render_open_courses_for_semester: failed to fetch open courses: %s", e)
        await _reply_or_edit(sender, f"❌ Error: {str(e)[:200]}", reply_markup=InlineKeyboardMarkup([]))
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    change_semester_row = InlineKeyboardButton("📆 Change Semester", callback_data="change_sem|enroll")
    toggle_label = f"{'✅' if scan_prev else '🔲'} Include Previous Semester"
    toggle_row = InlineKeyboardButton(toggle_label, callback_data=f"toggle_prev_sem_scan|{semester_id}")

    if not open_courses:
        await _reply_or_edit(
            sender,
            "ℹ️ No courses in your degree programme are currently open for enrolment in that semester" + (" (or the previous one)" if scan_prev and prev_label else "") + ".",
            reply_markup=InlineKeyboardMarkup([[toggle_row], [change_semester_row]]),
        )
        return

    # Group by (semester label, module) rather than just module — when the
    # previous semester is merged in, a module can share a name across both
    # terms, and these need to stay visually/functionally separate (distinct
    # headers) even though the course details underneath differ. _sem_label
    # is only set on items at all when two semesters were actually merged
    # (see above), so this is a no-op grouping-by-module-alone otherwise.
    grouped = {}
    for c in open_courses:
        grouped.setdefault((c.get("_sem_label"), c["module"]), []).append(c)

    # Circles first, then squares once a semester has more modules than
    # circle colors — same color on a module's header and every one of its
    # components ties them together at a glance.
    color_palette = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚫️", "⚪️", "🟤",
                      "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "⬛", "⬜", "🟫"]

    keyboard = []
    for i, ((sem_label, module_label), courses) in enumerate(grouped.items()):
        color = color_palette[i % len(color_palette)]
        # "Module:" distinguishes this header row (a non-tappable group label,
        # callback_data="course_noop") from the actual Lecture/Seminar/Exercise
        # buttons below it, which otherwise looked identical apart from lacking
        # a type prefix. The semester tag only shows when more than one term
        # is in play, to avoid clutter in the common single-semester case.
        header = f"{color} Module: {module_label[:48]}"
        if sem_label:
            header = f"{color} [{sem_label}] {module_label[:40]}"
        keyboard.append([InlineKeyboardButton(header, callback_data="course_noop")])
        for c in courses:
            keyboard.append([InlineKeyboardButton(
                f"{color} {c['type']}: {c['course'][:45]}",
                callback_data=f"course_enroll_ask|{c['sem_id']}",
            )])

    keyboard.append([toggle_row])
    keyboard.append([change_semester_row])

    count_text = f"📚 {len(open_courses)} course component(s) open for enrolment, grouped by module"
    count_text += f" (current + {prev_label} semester)." if prev_label else "."
    await _reply_or_edit(
        sender,
        f"{count_text}\n"
        "A module can require both a Lecture and a separate Seminar/Exercise — each is enrolled in individually. Tap one to enroll:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _render_enrolled_courses_for_semester(sender, semester_id: str):
    """List a given semester's currently enrolled courses so the student can
    pick one to sign out of. Shared by handle_deenroll_semester_pick (explicit
    picker) and handle_browse_my_courses_for_deenroll (when a default
    semester is set, skipping the picker)."""
    # Explicitly clear the keyboard here: editing text alone leaves whatever
    # buttons were on the previous message (e.g. the prior semester's "Sign
    # out" rows) live and tappable during this network wait, which let an
    # impatient tap land on a stale "Sign out" button for the wrong course.
    status_msg = await _reply_or_edit(sender, "📚 Loading your enrolled courses for that semester...", reply_markup=InlineKeyboardMarkup([]))
    await _show_typing(sender)
    stop_evt, anim_task = _start_loading_animation(status_msg, "Loading your enrolled courses for that semester...")

    try:
        courses = await list_courses(semester_id=semester_id)
    except Exception as e:
        # Stop before this edits the same status_msg with the error — see
        # the identical note in _render_open_courses_for_semester.
        await _stop_loading_animation(stop_evt, anim_task)
        logging.error("_render_enrolled_courses_for_semester: failed to fetch courses: %s", e)
        await _reply_or_edit(sender, f"❌ Error: {str(e)[:200]}", reply_markup=InlineKeyboardMarkup([]))
        return
    finally:
        await _stop_loading_animation(stop_evt, anim_task)

    change_semester_row = InlineKeyboardButton("📆 Change Semester", callback_data="change_sem|deenroll")

    if not courses:
        await _reply_or_edit(
            sender,
            "ℹ️ No enrolled courses found for that semester.",
            reply_markup=InlineKeyboardMarkup([[change_semester_row]]),
        )
        return

    # Best-effort: label each course by Lecture/Exercise/Seminar/etc. using the
    # same module scrape browse-courses uses. Only covers courses that belong
    # to one of the student's own degree-programme modules for that semester —
    # older/other courses (e.g. a language course) just show without a type
    # prefix rather than failing the whole list.
    type_map = {}
    try:
        session = await login_studip()
        type_map = await get_course_type_map(session, semester_id=semester_id)
    except Exception as e:
        logging.warning("_render_enrolled_courses_for_semester: type lookup failed, showing list without types: %s", e)

    keyboard = []
    for name, cid in courses:
        course_type = type_map.get(cid)
        label = f"🚪 [{course_type}] {name[:45]}" if course_type else f"🚪 {name[:55]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"course_deenroll_ask|{cid}")])
    keyboard.append([change_semester_row])
    await _reply_or_edit(
        sender,
        "📚 Your enrolled courses for that semester. Tap one to sign out of it:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _list_course_files(cid, skip_folder_re=None) -> list:
    """[(folder_name or None, file_item)] for a course's top level plus one
    folder level down, skipping folders whose name matches skip_folder_re
    (default _SKIPPED_FOLDER_RE: side material like books, literature,
    code, problem sets)."""
    skip_folder_re = skip_folder_re or _SKIPPED_FOLDER_RE
    out = []
    for item in await list_files(cid):
        if item.get("type") == "file":
            out.append((None, item))
        elif item.get("type") == "folder" and item.get("url") and not skip_folder_re.match(item.get("name", "")):
            try:
                sub = await list_files(cid, item["url"])
            except Exception as e:
                logging.warning("_list_course_files: list_files failed for folder %s: %s", item.get('name'), e)
                continue
            out.extend((item["name"], f) for f in sub if f.get("type") == "file")
    return out


# ── Course material search (questions answered from the course files) ──────
# Every enrolled course's PDF/PPTX/HTML files are split into passages and
# embedded with GWDG's e5-mistral-7b-instruct, once a day and only for new
# or changed files; a spoken question then gets the closest passages added
# to its context (see search_course_material, _route_voice_intent).
COURSE_INDEX_PATH = "course_index.json"
COURSE_INDEX_CHUNK_CHARS = 1200
COURSE_INDEX_OVERLAP_CHARS = 150
COURSE_INDEX_MAX_CHUNKS_PER_FILE = 80
COURSE_INDEX_BATCH = 16
# Embedding requests per daily run, so a first full index can't eat the
# day's shared GWDG quota; whatever is left gets indexed the next day.
COURSE_INDEX_MAX_CALLS = 60
COURSE_SEARCH_TOP_K = 5
# Passages that answered a test question scored 0.70-0.84, unrelated ones
# (course passages for a weather question) about 0.55.
COURSE_SEARCH_MIN_SCORE = 0.65
# e5 expects an instruction on the query side only.
COURSE_SEARCH_INSTRUCTION = "Instruct: Given a student's question, retrieve passages from the course material that answer it\nQuery: "


def load_course_index() -> dict:
    """{"files": {"<cid>|<rel_path>": {"course", "file", "size",
    "modified_ts", "chunks": [{"text", "vec"}]}}}, vec being the
    normalized embedding as base64 float16."""
    if db.exists(COURSE_INDEX_PATH):
        try:
            return db.load(COURSE_INDEX_PATH)
        except Exception as e:
            logging.warning("Could not load course index: %s", e)
    return {"files": {}}


def save_course_index(data: dict) -> None:
    try:
        db.save(COURSE_INDEX_PATH, data)
    except Exception as e:
        logging.error("Failed to save course index: %s", e)


def _pack_vector(vec: list) -> str:
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return base64.b64encode(struct.pack(f"<{len(vec)}e", *(x / norm for x in vec))).decode("ascii")


def _unpack_vector(packed: str) -> tuple:
    raw = base64.b64decode(packed)
    return struct.unpack(f"<{len(raw) // 2}e", raw)


def _chunk_text(text: str) -> list:
    """~COURSE_INDEX_CHUNK_CHARS passages, cut at line breaks where
    possible, overlapping a little so a sentence split across two is
    still found whole in one of them."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text) and len(chunks) < COURSE_INDEX_MAX_CHUNKS_PER_FILE:
        end = min(len(text), start + COURSE_INDEX_CHUNK_CHARS)
        if end < len(text):
            cut = text.rfind("\n", start + COURSE_INDEX_CHUNK_CHARS // 2, end)
            end = cut if cut > 0 else end
        chunk = text[start:end].strip()
        if len(chunk) > 40:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - COURSE_INDEX_OVERLAP_CHARS, start + 1)
    return chunks


async def embed_texts(texts: list) -> Optional[list]:
    """Embedding vectors for texts via GWDG (one request), or None."""
    if not GWDG_API_KEY or not texts or time.monotonic() < state._gwdg_blocked_until:
        return None
    try:
        async with shared_http() as session:
            for attempt in range(2):
                await _gwdg_throttle()
                async with session.post(
                    f"{GWDG_API_URL}/embeddings",
                    headers={"Authorization": f"Bearer {GWDG_API_KEY}"},
                    json={"model": GWDG_EMBEDDING_MODEL, "input": texts},
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    _record_gwdg_quota(resp.headers)
                    status = resp.status
                    data = await resp.json(content_type=None) if status == 200 else None
                if status != 429 or attempt:
                    break
                await asyncio.sleep(GWDG_BURST_WAIT_SECONDS)
        if status != 200:
            logging.warning("GWDG embeddings failed: HTTP %s", status)
            return None
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
    except Exception as e:
        logging.warning("GWDG embeddings failed: %s: %s", type(e).__name__, e)
        return None


@single_flight
async def index_course_files(session) -> dict:
    """Bring course_index.json up to date with every enrolled course's
    files: new or changed ones are downloaded, read (Docling for scans),
    chunked and embedded; files gone from Stud.IP are dropped. Stops after
    COURSE_INDEX_MAX_CALLS embedding requests and continues next run.
    Returns {"indexed": n_files, "chunks": n_chunks, "removed": n}."""
    stats = {"indexed": 0, "chunks": 0, "removed": 0}
    if not GWDG_API_KEY:
        return stats
    await _ensure_watched_courses_map(session)
    index = load_course_index()
    files_index = index.setdefault("files", {})
    seen, calls = set(), 0
    for cid, course_name in list(state.courses_map.items()):
        try:
            files = await _list_course_files(cid)
        except Exception as e:
            logging.warning("index_course_files: list_files failed for %s: %s", cid, e)
            continue
        for folder, f in files:
            name, url = f.get("name"), f.get("url")
            if not name or not url or os.path.splitext(name)[1].lower() not in STUDY_PLAN_SUPPORTED_EXTENSIONS:
                continue
            if _file_size_mb(f.get("size")) > STUDY_PLAN_MAX_FILE_MB:
                continue
            rel = f"{folder}/{name}" if folder else name
            key = f"{cid}|{rel}"
            seen.add(key)
            dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
            modified_ts = None if dt == datetime.min else int(dt.timestamp())
            size = str(f.get("size", "-")).strip()
            old = files_index.get(key)
            if old and old.get("size") == size and old.get("modified_ts") == modified_ts:
                continue
            if calls >= COURSE_INDEX_MAX_CALLS:
                continue
            try:
                async with await state.global_session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.read()
            except Exception as e:
                logging.warning("index_course_files: download failed for %s: %s", rel, e)
                continue
            text = await extract_text_from_file_async(name, content)
            chunks = _chunk_text(text or "")
            vectors = []
            for i in range(0, len(chunks), COURSE_INDEX_BATCH):
                if calls >= COURSE_INDEX_MAX_CALLS:
                    break
                batch = await embed_texts([f"{course_name} | {rel}\n{c}" for c in chunks[i:i + COURSE_INDEX_BATCH]])
                calls += 1
                if batch is None:
                    break
                vectors += batch
            if len(vectors) != len(chunks):
                continue  # incomplete: retried next run
            files_index[key] = {
                "course": course_name, "file": rel, "size": size, "modified_ts": modified_ts,
                "chunks": [{"text": c, "vec": _pack_vector(v)} for c, v in zip(chunks, vectors)],
            }
            stats["indexed"] += 1
            stats["chunks"] += len(chunks)
            save_course_index(index)
    enrolled = set(state.courses_map)
    for key in [k for k in files_index if k not in seen and k.split("|", 1)[0] in enrolled]:
        del files_index[key]
        stats["removed"] += 1
    for key in [k for k in files_index if k.split("|", 1)[0] not in enrolled]:
        del files_index[key]
        stats["removed"] += 1
    save_course_index(index)
    logging.info("index_course_files: %s, %s embedding calls", stats, calls)
    return stats


async def search_course_material(question: str, top_k: int = COURSE_SEARCH_TOP_K) -> list:
    """The top_k indexed passages closest to the question that clear
    COURSE_SEARCH_MIN_SCORE, as [{"course", "file", "text", "score"}],
    best first; [] if there is no index or the query couldn't be
    embedded."""
    files_index = load_course_index().get("files", {})
    if not files_index:
        return []
    vectors = await embed_texts([COURSE_SEARCH_INSTRUCTION + question])
    if not vectors:
        return []
    norm = sum(x * x for x in vectors[0]) ** 0.5 or 1.0
    query = [x / norm for x in vectors[0]]
    scored = []
    for entry in files_index.values():
        for chunk in entry.get("chunks", []):
            score = sum(a * b for a, b in zip(query, _unpack_vector(chunk["vec"])))
            scored.append((score, entry, chunk))
    scored.sort(key=lambda x: -x[0])
    return [{"course": e["course"], "file": e["file"], "text": c["text"], "score": round(sc, 3)} for sc, e, c in scored[:top_k] if sc >= COURSE_SEARCH_MIN_SCORE]


@single_flight
async def collect_course_topics(session) -> dict:
    """Scan every ENROLLED course's files (top level plus one folder level
    down, since many courses keep each week's slides in a dated folder
    like "20261013"), summarize any new/changed lecture file into a
    one-line topic label, cache the result, and return
    {cid: {rel_path: entry}} read back from the now-current cache - so
    courses with no new files this run still contribute topics learned in
    prior runs. rel_path is "folder/name" for files inside a folder and
    just "name" at the top level. The new/changed diff (size first, then a
    >24h timestamp-change tolerance) mirrors check_new_files_parallel's
    own logic against files_cache.json. Files that classify as admin or
    exercise material (README, syllabus, quizzes) are never downloaded,
    since the planner doesn't schedule them. When the student picked
    which courses to track, only those are scanned."""
    await _ensure_watched_courses_map(session)
    tracked = load_study_progress().get("tracked")
    courses = [(name, cid) for cid, name in state.courses_map.items() if tracked is None or cid in tracked]
    if not courses:
        return {}

    cache = load_study_plan_cache()
    semaphore = asyncio.Semaphore(3)

    async def process_course(course_name, cid):
        async with semaphore:
            try:
                files = await _list_course_files(cid)
            except Exception as e:
                logging.warning("collect_course_topics: list_files failed for %s: %s", cid, e)
                return

            course_cache = cache.setdefault(cid, {})
            for folder, f in files:
                name = f.get("name")
                url = f.get("url")
                if not name or not url:
                    continue
                if os.path.splitext(name)[1].lower() not in STUDY_PLAN_SUPPORTED_EXTENSIONS:
                    continue
                if classify_lecture_file(name, folder)[0] in ("admin", "exercise"):
                    continue
                if _file_size_mb(f.get("size")) > STUDY_PLAN_MAX_FILE_MB:
                    continue
                key = f"{folder}/{name}" if folder else name

                dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
                modified_ts = None if dt == datetime.min else int(dt.timestamp())
                cur_size = str(f.get("size", "-")).strip()

                old = course_cache.get(key)
                needs_processing = True
                if old is not None:
                    old_size = str(old.get("size", "-")).strip()
                    old_ts = old.get("modified_ts")
                    size_changed = old_size != cur_size
                    ts_changed = old_ts is not None and modified_ts is not None and abs(modified_ts - old_ts) > 86400
                    needs_processing = size_changed or ts_changed
                if not needs_processing:
                    continue

                try:
                    async with await state.global_session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        content = await resp.read()
                except Exception as e:
                    logging.warning("collect_course_topics: download failed for %s: %s", key, e)
                    continue

                text = await extract_text_from_file_async(name, content)
                if not text:
                    continue
                summary = await summarize_lecture_file(name, text)
                if not summary:
                    continue
                course_cache[key] = {"summary": summary, "size": cur_size, "modified_ts": modified_ts, "folder": folder}

    await asyncio.gather(*(process_course(name, cid) for name, cid in courses))
    save_study_plan_cache(cache)

    enrolled_cids = {cid for _, cid in courses}
    return {cid: files for cid, files in cache.items() if cid in enrolled_cids}


# A lecture's position in the course is read from its folder/filename
# ("20261013/Top_OLS.pdf", "AM_L3_Nash.pdf", "Lecture_04.pptx",
# "03_Intro.pdf") rather than from its upload timestamp - instructors
# routinely re-upload last year's whole deck at once, so timestamps say
# nothing about the order the material is taught in.
_LECTURE_NUMBER_RE = re.compile(
    r"(?:^|[^a-z])(?:l|lec|lecture|vl|vorlesung|week|woche|kapitel|chapter|ch|session|sitzung|unit|topic|part|teil)[\s_.-]*0*(\d{1,2})(?!\d)",
    re.IGNORECASE,
)
_LEADING_NUMBER_RE = re.compile(r"^0*(\d{1,2})(?!\d)[\s_.-]")
_DATED_FOLDER_RE = re.compile(r"^(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?:\D|$)")
_EXAM_FILE_RE = re.compile(r"klausur|exam(?!ple)|prüfung|pruefung|mock|past.?paper|sample.?question|answers|(?:^|[^a-z])q-?a(?:[^a-z]|$)|(?:^|[^a-z])test(?:[^a-z]|$)", re.IGNORECASE)
_EXERCISE_FILE_RE = re.compile(r"übung|uebung|exercise|tutorial|tutorium|(?:^|[^a-z])(?:sheet|blatt|solutions?|lösung|loesung)(?:[^a-z]|$)|assignment|problem.?set|quiz", re.IGNORECASE)
_ADMIN_FILE_RE = re.compile(r"syllabus|readme|(?:^|[^a-z])info(?:[^a-z]|$)|organi[sz]ation|orga\b|literatur|reading.?list|handbook|modul", re.IGNORECASE)
_SKIPPED_FOLDER_RE = re.compile(r"^(books?|literat\w*|readings?|r ?markdown|syllabus|src|code|data|labs?|ps|ep|solutions?|videos?|pyblp)$", re.IGNORECASE)
STUDY_SESSION_BLOCK_MINUTES = 90  # one lecture unit per block; longer windows are split into several blocks
STUDY_SESSION_MIN_BLOCK_MINUTES = 60  # a window's leftover shorter than this isn't worth a separate unit
STUDY_PLAN_MAX_BLOCKS_PER_DAY = 3
STUDY_DAY_START_HOUR = 10
STUDY_DAY_END_HOUR = 21


def _file_size_mb(size_str) -> float:
    m = re.match(r"([\d.,]+)\s*([KMG]?B)", str(size_str or ""), re.IGNORECASE)
    if not m:
        return 0.0
    value = float(m.group(1).replace(",", "."))
    return value * {"B": 1 / 1024 / 1024, "KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2).upper()]


def _folder_date(folder: Optional[str]):
    m = _DATED_FOLDER_RE.match(folder or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None


def classify_lecture_file(filename: str, folder: Optional[str] = None) -> tuple:
    """Return (kind, lecture_number) for a course file, where kind is one of
    "exam" (past papers, used for exam prep), "exercise", "lecture",
    "admin" (syllabus/README sheets, never scheduled) or "other"."""
    stem = os.path.splitext(filename)[0]
    if _EXAM_FILE_RE.search(stem) or _EXAM_FILE_RE.search(folder or ""):
        return "exam", None
    if _EXERCISE_FILE_RE.search(stem):
        return "exercise", None
    m = _LECTURE_NUMBER_RE.search(stem) or _LEADING_NUMBER_RE.search(stem)
    if m:
        return "lecture", int(m.group(1))
    if _ADMIN_FILE_RE.search(stem):
        return "admin", None
    return "other", None


# ── Workload estimation ──────────────────────────────────────────────────────
# Every lecture unit becomes a list of tasks with a time estimate, so the
# weekly plan can schedule by minutes instead of "one lecture per block":
# slides by page count, videos by the README's stated recording time (or
# their file size, calibrated on weeks where the README states it), and
# quizzes, problem sets and empirical projects by their number of
# questions/pages. Measurements are cached per file like the topic cache.

WORKLOAD_CACHE_PATH = "workload_cache.json"
SLIDE_MINUTES_PER_PAGE = 2
MIN_SLIDE_TASK_MINUTES = 20
VIDEO_STUDY_FACTOR = 1.25  # pausing, rewinding, taking notes
DEFAULT_VIDEO_MB_PER_MINUTE = 5.0
QUIZ_MINUTES_PER_QUESTION = 5
MIN_QUIZ_MINUTES = 15
PROBLEM_SET_MINUTES_PER_QUESTION = 15
MIN_PROBLEM_SET_MINUTES = 30
PROJECT_MINUTES_PER_PAGE = 60
PROJECT_MINUTES_RANGE = (120, 360)
PAST_PAPER_MINUTES = 90
UNKNOWN_UNIT_MINUTES = 60  # a lecture with no measurable material (e.g. no slides uploaded)

_VIDEO_EXTENSIONS = (".webm", ".mp4", ".m4v", ".mov", ".mkv")
# Unlike the topic scan, problem-set and project folders matter here.
_WORKLOAD_SKIPPED_FOLDER_RE = re.compile(r"^(books?|literat\w*|readings?|r ?markdown|syllabus|src|code|data|labs?|solutions?|videos?|pyblp|sample_questions|exam_preparation)$", re.IGNORECASE)
_PROBLEM_SET_FOLDER_RE = re.compile(r"^(ps|problem.?sets?|exercises?|übung\w*|uebung\w*|homework|assignments?)$", re.IGNORECASE)
_PROJECT_FOLDER_RE = re.compile(r"^(ep|projects?|projekt\w*)$", re.IGNORECASE)
_PROBLEM_SET_NAME_RE = re.compile(r"^ps[_\s-]|problem.?set|(?:^|[^a-z])(?:sheet|blatt|homework|assignment)(?:[^a-z]|$)", re.IGNORECASE)
_PROJECT_NAME_RE = re.compile(r"^ep[_\s-]|project|projekt", re.IGNORECASE)
_QUESTION_LINE_RE = re.compile(r"(?im)^\s*(?:(?:question|exercise|problem|task|aufgabe)\s*\d+|\d{1,2}[.)]\s+\S)")
_RECORDING_MINUTES_RE = re.compile(r"(?:recording|video|opencast|aufzeichnung)[^.]{0,120}?(\d{1,3})\s*(?:min\b|minutes|minuten)", re.IGNORECASE)
_MENTIONED_FILE_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_\-.]*\.(?:pdf|pptx|html))", re.IGNORECASE)  # READMEs list files as "--Top_X.pdf"


def load_workload_cache() -> dict:
    """{cid: {rel_path: {"kind", "size", "modified_ts", "pages",
    "questions", "video_minutes", "mentions"}}}."""
    if db.exists(WORKLOAD_CACHE_PATH):
        try:
            return db.load(WORKLOAD_CACHE_PATH)
        except Exception as e:
            logging.warning("Could not load workload cache: %s", e)
    return {}


def save_workload_cache(data: dict) -> None:
    try:
        db.save(WORKLOAD_CACHE_PATH, data)
    except Exception as e:
        logging.error("Failed to save workload cache: %s", e)


def classify_workload_file(name: str, folder_path: Optional[str]) -> Optional[str]:
    """"slides", "video", "readme", "quiz", "problem_set", "project",
    "past_paper", or None for anything that isn't work to schedule
    (data files, solutions, papers that sit next to a project brief)."""
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    parts = folder_path.split("/") if folder_path else []
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if stem.lower().startswith("readme"):
        return "readme" if ext in (".pdf", ".html", ".txt", ".md") else None
    if ext not in STUDY_PLAN_SUPPORTED_EXTENSIONS:
        return None
    if "quiz" in stem.lower():
        return "quiz"
    if any(_PROBLEM_SET_FOLDER_RE.match(p) for p in parts) or _PROBLEM_SET_NAME_RE.search(stem):
        return "problem_set"
    if any(_PROJECT_FOLDER_RE.match(p) for p in parts):
        return "project" if _PROJECT_NAME_RE.search(stem) else None
    if _PROJECT_NAME_RE.search(stem) and parts and _folder_date(parts[0]):
        return "project"
    kind, _ = classify_lecture_file(name, parts[-1] if parts else None)
    if kind == "exam":
        return "past_paper"
    if kind in ("lecture", "other"):
        return "slides"
    return None


def _measure_workload_file(kind: str, name: str, content: bytes) -> dict:
    text = extract_text_from_file(name, content) or ""
    if kind == "readme":
        m = _RECORDING_MINUTES_RE.search(text)
        return {
            "video_minutes": int(m.group(1)) if m else None,
            "mentions": sorted({x.lower() for x in _MENTIONED_FILE_RE.findall(text)}),
        }
    pages = None
    ext = os.path.splitext(name)[1].lower()
    try:
        if ext == ".pdf":
            import pypdf
            pages = len(pypdf.PdfReader(io.BytesIO(content)).pages)
        elif ext == ".pptx":
            import pptx
            pages = len(pptx.Presentation(io.BytesIO(content)).slides)
        elif ext == ".html":
            # reveal.js/Quarto decks: one <section> per slide
            pages = content.count(b"<section") or (len(text) // 1500 + 1)
    except Exception as e:
        logging.warning("_measure_workload_file: page count failed for %s: %s", name, e)
    return {"pages": pages, "questions": len(_QUESTION_LINE_RE.findall(text))}


async def _list_course_files_deep(cid) -> list:
    """Like _list_course_files, plus the problem-set/project subfolders
    inside weekly dated folders ("20261013/PS/PS_BaseR.pdf")."""
    out = []
    for item in await list_files(cid):
        if item.get("type") == "file":
            out.append((None, item))
            continue
        if item.get("type") != "folder" or not item.get("url") or _WORKLOAD_SKIPPED_FOLDER_RE.match(item.get("name", "")):
            continue
        try:
            sub = await list_files(cid, item["url"])
        except Exception as e:
            logging.warning("_list_course_files_deep: listing failed for %s: %s", item.get('name'), e)
            continue
        for s in sub:
            if s.get("type") == "file":
                out.append((item["name"], s))
            elif (
                s.get("type") == "folder" and s.get("url") and _folder_date(item["name"])
                and (_PROBLEM_SET_FOLDER_RE.match(s.get("name", "")) or _PROJECT_FOLDER_RE.match(s.get("name", "")))
            ):
                try:
                    out.extend((f"{item['name']}/{s['name']}", f) for f in await list_files(cid, s["url"]) if f.get("type") == "file")
                except Exception as e:
                    logging.warning("_list_course_files_deep: listing failed for %s/%s: %s", item['name'], s.get('name'), e)
    return out


@single_flight
async def collect_course_workload(session, cids: list) -> dict:
    """Measure every schedulable file of the given courses (downloads only
    new/changed files; videos are never downloaded, only their size is
    used) and return {cid: {rel_path: entry}} for them."""
    cache = load_workload_cache()
    semaphore = asyncio.Semaphore(3)

    async def process_course(cid):
        async with semaphore:
            try:
                files = await _list_course_files_deep(cid)
            except Exception as e:
                logging.warning("collect_course_workload: listing failed for %s: %s", cid, e)
                return
            old_entries = cache.get(cid, {})
            entries = {}
            for folder, f in files:
                name, url = f.get("name"), f.get("url")
                kind = classify_workload_file(name or "", folder)
                if not kind or not url:
                    continue
                key = f"{folder}/{name}" if folder else name
                cur_size = str(f.get("size", "-")).strip()
                if kind == "video":
                    entries[key] = {"kind": "video", "size": cur_size}
                    continue
                if _file_size_mb(cur_size) > STUDY_PLAN_MAX_FILE_MB:
                    continue
                dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
                modified_ts = None if dt == datetime.min else int(dt.timestamp())
                old = old_entries.get(key)
                if old is not None and old.get("kind") == kind and str(old.get("size", "-")).strip() == cur_size and not (
                    old.get("modified_ts") and modified_ts and abs(modified_ts - old["modified_ts"]) > 86400
                ):
                    entries[key] = old
                    continue
                try:
                    async with await state.global_session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        content = await resp.read()
                except Exception as e:
                    logging.warning("collect_course_workload: download failed for %s: %s", key, e)
                    continue
                entries[key] = {"kind": kind, "size": cur_size, "modified_ts": modified_ts, **_measure_workload_file(kind, name, content)}
            cache[cid] = entries

    await asyncio.gather(*(process_course(cid) for cid in cids))
    save_workload_cache(cache)
    return {cid: cache.get(cid, {}) for cid in cids}


def _video_mb_per_minute(workload: dict) -> float:
    """Calibrate the video size→duration estimate on weeks whose README
    states the total recording time (Opencast .webm files carry no
    duration header, so this is the only cheap source)."""
    total_mb = total_min = 0.0
    for entries in workload.values():
        folders: dict = {}
        for rel, e in entries.items():
            folders.setdefault(rel.split("/")[0], []).append(e)
        for group in folders.values():
            minutes = next((e.get("video_minutes") for e in group if e.get("kind") == "readme" and e.get("video_minutes")), None)
            mb = sum(_file_size_mb(e.get("size")) for e in group if e.get("kind") == "video")
            if minutes and mb:
                total_mb += mb
                total_min += minutes
    return total_mb / total_min if total_min else DEFAULT_VIDEO_MB_PER_MINUTE


def _make_task(unit_key: str, kind: str, ref: str, title: str, minutes: float) -> dict:
    return {"key": f"{unit_key}::{kind}::{ref}", "kind": kind, "title": title, "minutes": max(5, int(round(minutes)))}


def _file_task(unit_key: str, rel: str, e: dict) -> Optional[dict]:
    """The task for one measured file (slides/quiz/problem set/project/past paper)."""
    stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
    kind, pages, questions = e.get("kind"), e.get("pages"), e.get("questions") or 0
    if kind == "slides":
        minutes = max(MIN_SLIDE_TASK_MINUTES, pages * SLIDE_MINUTES_PER_PAGE) if pages else UNKNOWN_UNIT_MINUTES
        return _make_task(unit_key, kind, rel, f"Slides {stem}" + (f" ({pages} p.)" if pages else ""), minutes)
    if kind == "quiz":
        return _make_task(unit_key, kind, rel, f"Quiz {stem}", max(MIN_QUIZ_MINUTES, questions * QUIZ_MINUTES_PER_QUESTION))
    if kind == "problem_set":
        return _make_task(unit_key, kind, rel, f"Problem set {stem}" + (f" ({questions} q.)" if questions else ""), max(MIN_PROBLEM_SET_MINUTES, questions * PROBLEM_SET_MINUTES_PER_QUESTION))
    if kind == "project":
        lo, hi = PROJECT_MINUTES_RANGE
        return _make_task(unit_key, kind, rel, f"Empirical project {stem}", min(hi, max(lo, (pages or 0) * PROJECT_MINUTES_PER_PAGE)))
    if kind == "past_paper":
        return _make_task(unit_key, kind, rel, f"Past exam {stem}", PAST_PAPER_MINUTES)
    return None


def build_course_units(files: dict, workload: Optional[dict] = None, video_mb_per_minute: float = DEFAULT_VIDEO_MB_PER_MINUTE) -> dict:
    """Turn one course's topic cache ({rel_path: {"summary", "folder", ...}})
    and workload cache ({rel_path: measured entry}) into
    {"lectures": [...], "exam_practice": [...], "dated": bool}, lectures in
    teaching order, each with "tasks" (see _file_task) that say what to do
    and how long it takes. A unit is one lecture:
    - dated folders ("20261013"): one unit per folder = that day's class,
      with its slides, videos, quiz, problem set and project. When the
      folder's README names the slide decks, only those count (the rest
      are optional readings). "dated" is then True and the unit's date is
      the class it belongs to.
    - otherwise one unit per lecture file, ordered by its number.
    Unnumbered non-admin material only counts as lectures when the course
    has no numbered lecture files at all (otherwise it's usually a side
    reading that would shift every lecture's position by one)."""
    workload = workload or {}
    files_list = []
    for key, entry in files.items():
        name = key.rsplit("/", 1)[-1]
        folder = entry.get("folder")
        kind, number = classify_lecture_file(name, folder)
        files_list.append({
            "file": key,
            "topic": entry.get("summary") or name,
            "kind": kind,
            "number": number,
            "folder": folder,
            "date": _folder_date(folder),
            "modified_ts": entry.get("modified_ts") or 0,
        })
    exam_practice = sorted((u for u in files_list if u["kind"] == "exam"), key=lambda u: u["file"])
    for u in exam_practice:
        u["tasks"] = [_make_task(u["file"], "past_paper", u["file"], f"Past exam {os.path.splitext(u['file'].rsplit('/', 1)[-1])[0]}", PAST_PAPER_MINUTES)]
    content = [u for u in files_list if u["kind"] in ("lecture", "other")]

    dated_folders = {u["folder"] for u in content if u["date"] is not None}
    dated_folders |= {rel.split("/")[0] for rel in workload if "/" in rel and _folder_date(rel.split("/")[0])}
    if dated_folders:
        lectures = []
        for folder in dated_folders:
            topics = [u["topic"] for u in sorted(content, key=lambda u: (u["number"] if u["number"] is not None else 999, u["file"])) if u["folder"] == folder]
            entries = {rel: e for rel, e in workload.items() if rel.split("/")[0] == folder}
            readme = next((e for e in entries.values() if e.get("kind") == "readme"), None)
            mentions = {m.lstrip("-_.").lower() for m in (readme.get("mentions") or [])} if readme else set()
            tasks = []
            for rel, e in sorted(entries.items()):
                if e.get("kind") == "slides" and rel.count("/") == 1 and mentions and rel.rsplit("/", 1)[-1].lower() not in mentions:
                    continue  # not one of the README's slide decks: optional reading
                task = _file_task(folder, rel, e)
                if task:
                    tasks.append(task)
            videos = [e for e in entries.values() if e.get("kind") == "video"]
            if videos:
                stated = readme.get("video_minutes") if readme else None
                raw = stated or sum(_file_size_mb(e.get("size")) for e in videos) / video_mb_per_minute
                approx = "" if stated else "est. "
                tasks.append(_make_task(folder, "video", "all", f"Videos ({len(videos)} files, {approx}{int(raw)} min)", raw * VIDEO_STUDY_FACTOR))
            # Watch/read first, then quiz, then exercises and projects.
            order = {"slides": 0, "video": 1, "quiz": 2, "problem_set": 3, "project": 4}
            tasks.sort(key=lambda t: order.get(t["kind"], 9))
            if not tasks and topics:
                tasks = [_make_task(folder, "study", "all", "Study the material", UNKNOWN_UNIT_MINUTES)]
            if not tasks:
                continue
            topic = "; ".join(topics) or ", ".join(t["title"] for t in tasks)
            lectures.append({"file": folder, "topic": topic, "kind": "lecture", "number": None, "date": _folder_date(folder), "tasks": tasks})
        lectures.sort(key=lambda u: (u["date"], u["file"]))
        return {"lectures": lectures, "exam_practice": exam_practice, "dated": True}

    lectures = [u for u in content if u["kind"] == "lecture"] or content
    lectures.sort(key=lambda u: (u["number"] if u["number"] is not None else 999, u["modified_ts"], u["file"]))
    for u in lectures:
        task = _file_task(u["file"], u["file"], workload[u["file"]]) if u["file"] in workload else None
        u["tasks"] = [task or _make_task(u["file"], "study", "all", "Study the slides", UNKNOWN_UNIT_MINUTES)]
    return {"lectures": lectures, "exam_practice": exam_practice, "dated": False}


def _unit_label(unit: dict) -> str:
    if unit.get("date") is not None:
        prefix = f"{unit['date']:%d.%m}: "
    elif unit.get("number") is not None:
        prefix = f"L{unit['number']}: "
    else:
        prefix = ""
    return f"{prefix}{unit['topic']}"


def _apply_unit_progress(unit: dict, studied: set, task_minutes: dict) -> None:
    """Annotate a unit's tasks with "remaining" minutes, and the unit with
    "remaining" (sum) and "done". A unit marked studied by hand counts as
    fully done regardless of task minutes."""
    manual = unit["file"] in studied
    for t in unit["tasks"]:
        t["remaining"] = 0 if manual else max(0, t["minutes"] - task_minutes.get(t["key"], 0))
    unit["remaining"] = sum(t["remaining"] for t in unit["tasks"])
    unit["done"] = unit["remaining"] == 0


STUDY_PROGRESS_PATH = "study_progress.json"


def load_study_progress() -> dict:
    """{"studied": {cid: [unit_key, ...]}, "last_plan": [session, ...],
    "held": {cid: n}, "held_date": "YYYY-MM-DD", "tracked": [cid, ...]}.
    A unit_key is the lecture file's name, or "session-N" for a held
    lecture with no slides. "tracked" is absent while course selection is
    automatic (see compute_course_progress)."""
    if db.exists(STUDY_PROGRESS_PATH):
        try:
            return db.load(STUDY_PROGRESS_PATH)
        except Exception as e:
            logging.warning("Could not load study progress: %s", e)
    return {}


def save_study_progress(data: dict) -> None:
    try:
        db.save(STUDY_PROGRESS_PATH, data)
    except Exception as e:
        logging.error("Failed to save study progress: %s", e)


def _semester_lecture_start(today):
    """First day of the current semester's lecture period (approximate:
    WS from 1 Oct, SS from 1 Apr) - only used as the lower bound for
    counting lectures that already took place."""
    if today.month >= 10:
        return datetime(today.year, 10, 1).date()
    if today.month >= 4:
        return datetime(today.year, 4, 1).date()
    return datetime(today.year - 1, 10, 1).date()


async def count_lectures_held(session, course_candidates: list) -> dict:
    """{cid: number of lecture sessions already held this semester}, from
    the Stud.IP calendar. Only "Vorlesung"/"Lecture"/"Seminar" entries count
    (exercises repeat the lecture's material), and cancelled ones
    ("fällt aus") are skipped. This is what "covered in class" means for
    the behind-schedule check: the first N lecture units of a course."""
    from unibot.voice import _best_title_match
    today = datetime.now(TZ_BERLIN).date()
    start = _semester_lecture_start(today)
    events = await get_calendar_events(session, start, days=(today - start).days + 1)
    now = datetime.now(TZ_BERLIN)
    held: dict = {}
    for ev in events:
        title = ev.get("title", "")
        lower = title.lower()
        if ev["end_dt"] > now:
            continue
        if not lower.startswith(("vorlesung", "lecture", "seminar")):
            continue
        if "fällt aus" in lower or "cancelled" in lower or "canceled" in lower:
            continue
        cid = _best_title_match(clean_course_title(title), course_candidates, min_score=0.6)
        if cid:
            held[cid] = held.get(cid, 0) + 1
    return held


async def _get_lectures_held(session, progress: dict, refresh: bool = False) -> dict:
    """Held-lecture counts, recomputed at most once a day unless refresh
    is set (the progress menu's buttons would otherwise refetch the whole
    semester's calendar on every tap)."""
    today_iso = datetime.now(TZ_BERLIN).date().isoformat()
    if not refresh and progress.get("held_date") == today_iso and "held" in progress:
        return progress["held"]
    course_candidates = [(name, cid) for cid, name in state.courses_map.items()]
    held = await count_lectures_held(session, course_candidates)
    progress["held"] = held
    progress["held_date"] = today_iso
    save_study_progress(progress)
    return held


def compute_course_progress(course_files: dict, held: dict, progress: dict, names: dict, workload: Optional[dict] = None) -> list:
    """One row per tracked course: which lecture units count as covered in
    class, which of those still have work left (the backlog, with its
    minutes), and what comes next. Sorted with the biggest backlog first.
    The course selection is progress["tracked"]; when absent (automatic),
    every course that has had lectures this semester or already has
    logged progress is included. Studied state comes from
    progress["studied"] (whole units marked by hand) and
    progress["task_minutes"] (minutes credited from the weekly plan)."""
    today = datetime.now(TZ_BERLIN).date()
    workload = workload or {}
    video_rate = _video_mb_per_minute(workload)
    tracked = progress.get("tracked")
    rows = []
    for cid, name in names.items():
        studied = set(progress.get("studied", {}).get(cid, []))
        task_minutes = progress.get("task_minutes", {}).get(cid, {})
        n_held = held.get(cid, 0)
        if tracked is not None:
            if cid not in tracked:
                continue
        elif n_held == 0 and not studied and not task_minutes:
            continue

        units = build_course_units(course_files.get(cid, {}), workload.get(cid, {}), video_rate)
        lectures = units["lectures"]
        if units["dated"]:
            # Dated folders say exactly which material belongs to which
            # class, so no need to estimate from the held-lecture count.
            covered = [u for u in lectures if u["date"] <= today]
        else:
            covered = list(lectures[:n_held])
            for k in range(len(lectures) + 1, n_held + 1):
                key = f"session-{k}"
                covered.append({
                    "file": key, "topic": f"Lecture {k} notes (no slides on Stud.IP yet)", "kind": "lecture", "number": k,
                    "tasks": [_make_task(key, "study", "all", "Review your lecture notes", UNKNOWN_UNIT_MINUTES)],
                })
        for u in lectures + covered + units["exam_practice"]:
            _apply_unit_progress(u, studied, task_minutes)
        covered_ids = {id(u) for u in covered}
        backlog = [u for u in covered if not u["done"]]
        rows.append({
            "cid": cid,
            "name": name,
            "held": n_held,
            "dated": units["dated"],
            "total_lectures": len(lectures),
            "covered": covered,
            "studied_covered": len(covered) - len(backlog),
            "backlog": backlog,
            "backlog_minutes": sum(u["remaining"] for u in backlog),
            "upcoming": [u for u in lectures if id(u) not in covered_ids and not u["done"]],
            "exam_practice": [u for u in units["exam_practice"] if not u["done"]],
            "lectures": lectures,
        })
    rows.sort(key=lambda r: (-r["backlog_minutes"], r["name"]))
    return rows


def _split_gaps_into_blocks(gaps: list) -> list:
    blocks = []
    per_day: dict = {}
    for g in gaps:
        t = g["start_dt"]
        while (g["end_dt"] - t).total_seconds() / 60 >= STUDY_SESSION_MIN_BLOCK_MINUTES:
            if per_day.get(g["date_key"], 0) >= STUDY_PLAN_MAX_BLOCKS_PER_DAY:
                break
            end = min(t + timedelta(minutes=STUDY_SESSION_BLOCK_MINUTES), g["end_dt"])
            blocks.append((t, end))
            per_day[g["date_key"]] = per_day.get(g["date_key"], 0) + 1
            t = end
    return blocks


def find_free_study_windows(events: list, week_start) -> list:
    """Free windows of at least STUDY_GAP_MIN_MINUTES within each weekday's
    STUDY_DAY_START_HOUR-STUDY_DAY_END_HOUR study day, in the same dict
    shape as find_weekly_study_gaps. Unlike that function (which is shared
    with the voice narration and only looks between two classes), this
    also counts the morning before the first class, the afternoon after
    the last one and fully free days. Cancelled classes ("fällt aus") are
    treated as free time. Weekends are deliberately excluded."""
    windows = []
    for i in range(5):
        day = week_start + timedelta(days=i)
        day_start = datetime.combine(day, _dtime(STUDY_DAY_START_HOUR, 0), tzinfo=TZ_BERLIN)
        day_end = datetime.combine(day, _dtime(STUDY_DAY_END_HOUR, 0), tzinfo=TZ_BERLIN)
        busy = sorted(
            (ev for ev in events if ev["date_key"] == day and "fällt aus" not in ev.get("title", "").lower()),
            key=lambda ev: ev["start_dt"],
        )
        cursor, after = day_start, ("no classes that day" if not busy else "start of day")
        for ev in busy + [None]:
            if ev is None:
                end, before = day_end, ("no classes that day" if not busy else "end of day")
            else:
                end, before = min(ev["start_dt"], day_end), clean_course_title(ev.get("title", "")) or "your next class"
            minutes = (end - cursor).total_seconds() / 60
            if minutes >= STUDY_GAP_MIN_MINUTES:
                windows.append({
                    "date_key": day,
                    "start_dt": cursor,
                    "end_dt": end,
                    "duration_minutes": int(minutes),
                    "after": after,
                    "before": before,
                })
            if ev is not None and ev["end_dt"] > cursor:
                cursor = max(cursor, ev["end_dt"])
                after = clean_course_title(ev.get("title", "")) or "your previous class"
    return windows


async def gather_weekly_study_inputs(session) -> dict:
    """Collect this run's inputs: next week's free study windows, the
    student's own upcoming registered exams (closest first, next 3 weeks;
    the same get_registered_exam_schedule source _upcoming_exams uses, not
    the full-curriculum list), and per-course progress rows (ordered
    lecture units vs. lectures already held vs. units already studied)."""
    from unibot.voice import _best_title_match
    today = datetime.now(TZ_BERLIN).date()
    next_monday = today + timedelta(days=7 - today.weekday())
    events = await get_calendar_events(session, next_monday)
    gaps = find_free_study_windows(events, next_monday)

    try:
        raw_exams = await get_registered_exam_schedule(session)
    except Exception as e:
        logging.warning("gather_weekly_study_inputs: get_registered_exam_schedule failed: %s", e)
        raw_exams = []

    course_files = await collect_course_topics(session)
    course_candidates = [(name, cid) for cid, name in state.courses_map.items()]
    exams = []
    for exam in raw_exams:
        date_str = exam.get("date")
        if not date_str:
            continue
        try:
            exam_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            continue
        days_until = (exam_date - today).days
        if not (0 <= days_until <= STUDY_PLAN_EXAM_WINDOW_DAYS):
            continue
        matched_cid = _best_title_match(exam["title"], course_candidates, min_score=0.6)
        exams.append({**exam, "days_until": days_until, "matched_cid": matched_cid})
    # Exams announced only in course files count too (already tied to their
    # course, no fuzzy matching needed). Retakes are left out: before the
    # main exam they aren't what the student is preparing for.
    known = {(e["matched_cid"], e.get("date")) for e in exams}
    for fe in get_file_exam_events():
        if fe["kind"] not in ("exam", "midterm") or not fe["date"]:
            continue
        days_until = (datetime.fromisoformat(fe["date"]).date() - today).days
        if 0 <= days_until <= STUDY_PLAN_EXAM_WINDOW_DAYS and (fe["cid"], fe["date"]) not in known:
            exams.append({"title": f"{fe['course']} ({fe['kind']})", "date": fe["date"], "days_until": days_until, "matched_cid": fe["cid"]})
    exams.sort(key=lambda e: e["days_until"])

    progress = load_study_progress()
    held = await _get_lectures_held(session, progress, refresh=True)
    tracked = progress.get("tracked")
    if tracked is None:
        tracked = _auto_tracked_cids(progress)
    workload = await collect_course_workload(session, [cid for cid in tracked if cid in state.courses_map])
    rows = compute_course_progress(course_files, held, progress, dict(state.courses_map), workload)
    return {"gaps": gaps, "exams": exams, "rows": rows, "week_start": next_monday}


def render_behind_lines(rows: list) -> list:
    behind = [r for r in rows if r["backlog"]]
    if not behind:
        return ["✅ <b>You're caught up</b> with every lecture held so far."]
    lines = ["⚠️ <b>Behind on:</b>"]
    for r in behind:
        n = len(r["backlog"])
        lines.append(f"• {html.escape(r['name'], quote=False)}: {n} lecture{'s' if n != 1 else ''}, ~{_hours(r['backlog_minutes'])} of work")
    return lines


def render_workload_lines(stats: dict) -> list:
    """Needed vs. free time for the week, per course, plus what doesn't fit."""
    needed_total = sum(stats["needed"].values())
    lines = [f"📊 <b>Workload:</b> {_hours(needed_total)} needed · {_hours(stats['free_minutes'])} free study time"]
    for cid, minutes in sorted(stats["needed"].items(), key=lambda kv: -kv[1]):
        scheduled = stats["scheduled"].get(cid, 0)
        short = minutes - scheduled
        tail = f" (⚠️ {_hours(short)} doesn't fit)" if short >= 15 else ""
        name = stats["names"].get(cid, cid)
        lines.append(f"• {html.escape(name, quote=False)} ({_course_abbrev(name)}): {_hours(minutes)}{tail}")
    for course, due in stats["missed_prep"]:
        lines.append(f"⚠️ Not enough free time to fully prepare {html.escape(course, quote=False)} before class on {datetime.fromisoformat(due):%a %d.%m}")
    return lines


def build_study_plan_keyboard(plan: list, done_sessions: list) -> InlineKeyboardMarkup:
    keyboard = []
    done = set(done_sessions)
    for i, s in enumerate(plan):
        mark = "✅" if i in done else "⬜"
        courses = list(dict.fromkeys(p["course"] for p in s["pieces"]))
        course_label = courses[0] + (f" +{len(courses) - 1}" if len(courses) > 1 else "")
        label = f"{mark} {i + 1}. {s['day'][:3]} {s['start']} {course_label}"
        keyboard.append([InlineKeyboardButton(label[:60], callback_data=f"sp_plan|{i}")])
    keyboard.append([InlineKeyboardButton("📊 Progress", callback_data="sp_progress")])
    return InlineKeyboardMarkup(keyboard)


async def run_weekly_study_plan_job(bot, user_ids, session, status_msg=None) -> str:
    """End-to-end weekly study plan: gather inputs, schedule, render, send
    to every user_id. Returns the rendered message text. status_msg, if
    given, is edited through the stages (used by the manual /studyplan
    command's progress display)."""
    # status_msg (from study_plan_command) already reads "Scanning course
    # files..."; editing it to the same text makes Telegram raise "Message
    # is not modified", so the first stage needs no edit.
    inputs = await gather_weekly_study_inputs(session)

    if not inputs["gaps"]:
        text = "\n".join(
            ["📘 No free study gaps found in next week's schedule, fully booked! No study plan to suggest this week.", ""]
            + render_behind_lines(inputs["rows"])
        )
        for uid in user_ids:
            try:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            except Exception as e:
                logging.warning("run_weekly_study_plan_job: failed to send to %s: %s", uid, e)
        return text

    if status_msg:
        await status_msg.edit_text("📘 Building your weekly study plan...\n\n🧠 Scheduling sessions...")
    plan = schedule_study_sessions(inputs["gaps"], inputs["rows"], inputs["exams"], inputs["week_start"])
    progress = load_study_progress()
    progress["last_plan"] = plan["sessions"]
    progress["last_plan_done"] = []
    save_study_progress(progress)

    text = render_weekly_study_plan_message(plan, inputs["gaps"], inputs["exams"], inputs["rows"])
    reply_markup = build_study_plan_keyboard(plan["sessions"], []) if plan["sessions"] else None
    chunks = _split_message(text)
    for uid in user_ids:
        try:
            for i, chunk in enumerate(chunks):
                await bot.send_message(chat_id=uid, text=chunk, parse_mode="HTML", reply_markup=reply_markup if i == len(chunks) - 1 else None)
        except Exception as e:
            logging.warning("run_weekly_study_plan_job: failed to send to %s: %s", uid, e)
    return text


def _toggle_studied(progress: dict, unit: dict, cid: str) -> bool:
    """Flip one unit's studied state by hand; returns the new state.
    Unmarking also clears minutes the weekly plan credited to its tasks,
    so the unit really starts over."""
    studied = progress.setdefault("studied", {}).setdefault(cid, [])
    if unit["done"]:
        if unit["file"] in studied:
            studied.remove(unit["file"])
        task_minutes = progress.setdefault("task_minutes", {}).setdefault(cid, {})
        for t in unit.get("tasks", []):
            task_minutes.pop(t["key"], None)
        return False
    studied.append(unit["file"])
    return True


async def handle_study_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A session button on the weekly plan message: credit (or take back)
    that session's minutes to its tasks and refresh the checkmarks."""
    query = update.callback_query
    progress = load_study_progress()
    plan = progress.get("last_plan", [])
    try:
        idx = int(query.data.split("|", 1)[1])
        s = plan[idx]
        pieces = s["pieces"]
    except (ValueError, IndexError, KeyError):
        await query.answer("This plan is outdated, run /studyplan again.", show_alert=True)
        return
    done = progress.setdefault("last_plan_done", [])
    now_done = idx not in done
    sign = 1 if now_done else -1
    for p in pieces:
        task_minutes = progress.setdefault("task_minutes", {}).setdefault(p["cid"], {})
        task_minutes[p["task_key"]] = max(0, task_minutes.get(p["task_key"], 0) + sign * p["minutes"])
    if now_done:
        done.append(idx)
    else:
        done.remove(idx)
    save_study_progress(progress)
    await query.answer("Session done ✅" if now_done else "Session unmarked")
    try:
        await query.edit_message_reply_markup(reply_markup=build_study_plan_keyboard(plan, done))
    except Exception as e:
        logging.debug("handle_study_plan_toggle: keyboard refresh skipped: %s", e)


async def _load_progress_rows(refresh: bool = False) -> tuple:
    """(rows, progress). With refresh, the tracked courses' files are
    rescanned first (only new/changed files are summarized/measured, so
    this is mostly listing requests) and held-lecture counts are
    recomputed; without it, cached data is used so per-lecture toggle
    buttons stay instant."""
    if not state.global_session or not await state.global_session.is_logged_in():
        await login_studip()
    await _ensure_watched_courses_map(state.global_session)
    if refresh:
        await collect_course_topics(state.global_session)
    progress = load_study_progress()
    held = await _get_lectures_held(state.global_session, progress, refresh=refresh)
    enrolled = set(state.courses_map)
    tracked = progress.get("tracked")
    if tracked is None:
        tracked = _auto_tracked_cids(progress)
    if refresh:
        await collect_course_workload(state.global_session, [cid for cid in tracked if cid in enrolled])
    course_files = {cid: files for cid, files in load_study_plan_cache().items() if cid in enrolled}
    workload = {cid: entries for cid, entries in load_workload_cache().items() if cid in enrolled}
    rows = compute_course_progress(course_files, held, progress, dict(state.courses_map), workload)
    return rows, progress


def render_progress_overview(rows: list) -> tuple:
    lines = ["📊 <b>Study Progress</b>", "<i>studied / lectures held so far</i>", "━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("No tracked courses with lectures yet. Use 🎯 Tracked Courses to pick the courses to follow.")
    for r in rows:
        covered = len(r["covered"])
        if r["backlog"]:
            status = f"⚠️ {len(r['backlog'])} behind, ~{_hours(r['backlog_minutes'])} left"
        elif covered:
            status = "✅ on track"
        else:
            status = "no lectures held yet"
        lines.append(f"<b>{html.escape(r['name'])}</b>")
        lines.append(f"{_progress_bar(r['studied_covered'], covered)} {r['studied_covered']}/{covered} · {status}")
        if r["backlog"]:
            lines.append(f"   next up: {html.escape(_unit_label(r['backlog'][0]), quote=False)}")
        elif r["upcoming"]:
            lines.append(f"   next lecture: {html.escape(_unit_label(r['upcoming'][0]), quote=False)}")
        lines.append("")
    keyboard = [[InlineKeyboardButton(f"{'⚠️' if r['backlog'] else '📚'} {r['name']}"[:60], callback_data=f"sp_course|{r['cid']}")] for r in rows]
    keyboard.append([InlineKeyboardButton("🎯 Tracked Courses", callback_data="sp_track_menu")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="sp_progress_refresh")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard)


def _auto_tracked_cids(progress: dict) -> list:
    """What automatic mode tracks: courses with lectures held this
    semester or with progress already logged."""
    held = progress.get("held", {})
    studied = progress.get("studied", {})
    return [cid for cid in state.courses_map if held.get(cid, 0) > 0 or studied.get(cid)]


def render_track_menu(progress: dict) -> tuple:
    tracked = progress.get("tracked")
    selected = set(tracked if tracked is not None else _auto_tracked_cids(progress))
    held = progress.get("held", {})
    mode = "manual selection" if tracked is not None else "automatic (courses with lectures held this semester)"
    lines = [
        "🎯 <b>Tracked Courses</b>",
        "Only the selected courses appear in /progress and the weekly study plan.",
        f"Mode: <i>{mode}</i>",
        "━━━━━━━━━━━━━━━━━━",
        "Tap a course to add or remove it.",
    ]
    keyboard = []
    for cid, name in sorted(state.courses_map.items(), key=lambda kv: (kv[0] not in selected, kv[1].lower())):
        mark = "✅" if cid in selected else "⬜"
        n = held.get(cid, 0)
        suffix = f" · {n} held" if n else ""
        keyboard.append([InlineKeyboardButton(f"{mark} {name}"[:55] + suffix, callback_data=f"sp_track|{cid}")])
    if tracked is not None:
        keyboard.append([InlineKeyboardButton("🔄 Back to automatic selection", callback_data="sp_track_auto")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to progress", callback_data="sp_progress")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def _load_track_menu_progress() -> dict:
    if not state.global_session or not await state.global_session.is_logged_in():
        await login_studip()
    await _ensure_watched_courses_map(state.global_session)
    progress = load_study_progress()
    await _get_lectures_held(state.global_session, progress)
    return progress


async def track_courses_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return
    status_msg = await update.message.reply_text("🎯 Loading your courses...", disable_notification=True)
    try:
        text, markup = render_track_menu(await _load_track_menu_progress())
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logging.exception("track_courses_command failed")
        await status_msg.edit_text(f"❌ Could not load courses: {e}")


async def handle_study_track_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sp_track_menu / sp_track_auto / sp_track|cid. The first toggle in
    automatic mode starts the manual selection from what automatic mode
    currently shows, so tapping one course doesn't wipe all the others."""
    query = update.callback_query
    await query.answer()
    progress = await _load_track_menu_progress()
    data = query.data
    if data == "sp_track_auto":
        progress.pop("tracked", None)
        save_study_progress(progress)
    elif data.startswith("sp_track|"):
        cid = data.split("|", 1)[1]
        tracked = progress.get("tracked")
        if tracked is None:
            tracked = _auto_tracked_cids(progress)
        if cid in tracked:
            tracked.remove(cid)
        else:
            tracked.append(cid)
        progress["tracked"] = tracked
        save_study_progress(progress)
    text, markup = render_track_menu(progress)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


def _course_view_units(row: dict) -> list:
    """Units shown (and toggleable by index) in one course's progress
    view: everything held so far, then the not-yet-held lectures."""
    covered_ids = {id(u) for u in row["covered"]}
    return row["covered"] + [u for u in row["lectures"] if id(u) not in covered_ids]


def render_course_progress(row: dict) -> tuple:
    units = _course_view_units(row)
    lines = [
        f"📚 <b>{html.escape(row['name'])}</b>",
        f"{row['held']} lecture(s) held · {row['studied_covered']}/{len(row['covered'])} of those studied"
        + (f" · ~{_hours(row['backlog_minutes'])} of catch-up left" if row["backlog_minutes"] else ""),
        "━━━━━━━━━━━━━━━━━━",
    ]
    keyboard = []
    for i, u in enumerate(units):
        held = i < len(row["covered"])
        if u["done"]:
            mark = "✅"
        elif held:
            mark = "⚠️"
        else:
            mark = "⬜"
        total = sum(t["minutes"] for t in u["tasks"])
        left = f" <i>({u['remaining']} of {total} min left)</i>" if 0 < u["remaining"] < total else (f" <i>(~{total} min)</i>" if not u["done"] else "")
        lines.append(f"{mark} {html.escape(_unit_label(u), quote=False)}{left}")
        if i < 40:
            keyboard.append([InlineKeyboardButton(f"{mark} {_unit_label(u)}"[:60], callback_data=f"sp_unit|{row['cid']}|{i}")])
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("✅ done · ⚠️ held in class, work left · ⬜ not held yet")
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="sp_progress")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return
    status_msg = await update.message.reply_text("📊 Loading study progress...\n\n📚 Scanning course files...", disable_notification=True)
    try:
        rows, _ = await _load_progress_rows(refresh=True)
        text, markup = render_progress_overview(rows)
        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logging.exception("progress_command failed")
        await status_msg.edit_text(f"❌ Could not load progress: {e}")


async def handle_study_progress_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sp_progress / sp_progress_refresh / sp_course|cid / sp_unit|cid|i."""
    query = update.callback_query
    await query.answer()
    data = query.data
    rows, progress = await _load_progress_rows(refresh=(data == "sp_progress_refresh"))

    if data in ("sp_progress", "sp_progress_refresh"):
        text, markup = render_progress_overview(rows)
        if query.message.reply_markup and any(b.callback_data and b.callback_data.startswith("sp_plan|") for row in query.message.reply_markup.inline_keyboard for b in row):
            # Opened from the weekly plan message: keep the plan intact and
            # show the overview as a new message instead of overwriting it.
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except Exception as e:
                # 🔄 Refresh with nothing changed: Telegram rejects an identical edit.
                if "not modified" not in str(e).lower():
                    raise
        return

    parts = data.split("|")
    row = next((r for r in rows if r["cid"] == parts[1]), None)
    if row is None:
        await query.edit_message_text("Course not found anymore, run /progress again.")
        return

    if parts[0] == "sp_unit":
        units = _course_view_units(row)
        try:
            unit = units[int(parts[2])]
        except (ValueError, IndexError):
            return
        _toggle_studied(progress, unit, row["cid"])
        save_study_progress(progress)
        rows = compute_course_progress(
            {row["cid"]: load_study_plan_cache().get(row["cid"], {})},
            progress.get("held", {}),
            progress,
            {row["cid"]: row["name"]},
            {row["cid"]: load_workload_cache().get(row["cid"], {})},
        )
        row = rows[0] if rows else row

    text, markup = render_course_progress(row)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


study_plan_in_progress: set = set()


async def study_plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual on-demand "/studyplan" trigger — same progress-message
    pattern as _run_check_now: one status message edited through stages,
    then the plan itself is delivered as its own message by
    run_weekly_study_plan_job (shared with the automatic weekly trigger)."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not is_user_allowed(user_id):
        return
    chat_id = update.effective_chat.id
    if chat_id in study_plan_in_progress:
        await update.message.reply_text("⚠️ A study plan is already being generated.")
        return

    study_plan_in_progress.add(chat_id)
    try:
        status_msg = await update.message.reply_text(
            "📘 Building your weekly study plan...\n\n📚 Scanning course files...",
            disable_notification=True,
        )
        if not state.global_session or not await state.global_session.is_logged_in():
            await status_msg.edit_text("🔄 Session expired, re-logging...")
            await login_studip()
        await run_weekly_study_plan_job(context.bot, [user_id], state.global_session, status_msg=status_msg)
        await status_msg.edit_text("✅ Study plan ready, see below 👇")
    except Exception as e:
        await update.message.reply_text(f"❌ Fatal error: {e}")
        logging.exception("study_plan_command failed")
    finally:
        study_plan_in_progress.discard(chat_id)
