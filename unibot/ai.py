"""LLM providers (GWDG, OpenRouter), speech-to-text, text-to-speech and JSON extraction.

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import asyncio
import contextvars
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from studip_utils import TZ_BERLIN, aread_bytes, awrite_bytes

from unibot.config import (
    GWDG_API_KEY,
    GWDG_API_URL,
    GWDG_IMAGE_EDIT_SERVICE,
    GWDG_TEXT_MODEL,
    GWDG_WHISPER_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_TTS_MODEL,
    OPENROUTER_TTS_VOICE,
)
from unibot import state
from unibot.auth import current_user_id, is_user_admin
from unibot.storage import save_wardrobe_item_descriptions, shared_http, student_courses


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"


# ── Voice-to-task (free-tier speech-to-text, no API key needed) ────────────────
# Google's free endpoint only accepts one language per call, so both are tried
# concurrently and langdetect picks whichever transcript actually matches the
# language it was recognized in.
VOICE_TASK_LANGUAGES = ["tr-TR", "en-US"]
VOICE_TASK_LANGDETECT_CODES = {"tr-TR": "tr", "en-US": "en"}


async def _recognize_in_language(recognizer, audio, language: str):
    """Return (text, request_error). text is None if unrecognized in this
    language; request_error is set only when the service itself failed."""
    import speech_recognition as sr
    try:
        text = await asyncio.to_thread(recognizer.recognize_google, audio, language=language)
        return text, None
    except sr.UnknownValueError:
        return None, None
    except sr.RequestError as e:
        return None, e




async def transcribe_with_whisper(ogg_path: str) -> Optional[str]:
    """Transcribe a voice note with GWDG's Whisper (takes the OGG as is,
    detects the language itself). In a side-by-side test it kept English
    course names inside Turkish sentences ("Advanced Microeconomics
    dersinin..."), which Google's one-language-per-call endpoint dropped,
    and heard "Mensa" where Google heard "Manisa". The course names go in
    as a vocabulary hint. None if GWDG is unset or the call failed."""
    if not GWDG_API_KEY or time.monotonic() < state._gwdg_blocked_until or not use_ai_budget():
        return None  # the free Google recognizer takes over
    # the speaking student's own course names help the transcription
    hint = ", ".join(["Stud.IP", "StuMS", "Mensa"] + sorted(set(student_courses(current_user_id()).values())))[:800]
    form = aiohttp.FormData()
    form.add_field("model", GWDG_WHISPER_MODEL)
    form.add_field("response_format", "json")
    form.add_field("prompt", hint)
    form.add_field("file", await aread_bytes(ogg_path), filename="voice.ogg", content_type="audio/ogg")
    try:
        await _gwdg_throttle()
        async with shared_http() as session:
            async with session.post(
                f"{GWDG_API_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {GWDG_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                _record_gwdg_quota(resp.headers)
                if resp.status != 200:
                    logging.warning("Whisper transcription failed: HTTP %s", resp.status)
                    return None
                data = await resp.json(content_type=None)
        return (data.get("text") or "").strip() or None
    except Exception as e:
        logging.warning("Whisper transcription failed: %s: %s", type(e).__name__, e)
        return None


async def transcribe_voice_message(ogg_path: str) -> str:
    """Convert a Telegram voice note (OGG/Opus) to text: GWDG's Whisper
    first (see transcribe_with_whisper), else SpeechRecognition's free
    Google Web Speech endpoint, trying Turkish and English concurrently.
    Requires ffmpeg on PATH for the OGG->WAV conversion (via pydub). Raises
    RuntimeError with a user-facing message on any failure (missing ffmpeg,
    unclear audio in both languages, network issue, ...).
    """
    text = await transcribe_with_whisper(ogg_path)
    if text:
        return text

    import speech_recognition as sr
    from pydub import AudioSegment

    wav_path = ogg_path + ".wav"
    def _convert() -> None:
        AudioSegment.from_file(ogg_path).export(wav_path, format="wav")

    def _record(recognizer):
        with sr.AudioFile(wav_path) as source:
            return recognizer.record(source)

    try:
        await asyncio.to_thread(_convert)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg is not installed on the server, so voice messages can't be converted. Install it with `apt install ffmpeg` (Linux) or `brew install ffmpeg` (Mac).") from e
    except Exception as e:
        raise RuntimeError(f"Could not process the audio file: {e}") from e

    try:
        recognizer = sr.Recognizer()
        audio = await asyncio.to_thread(_record, recognizer)

        results = await asyncio.gather(
            *(_recognize_in_language(recognizer, audio, lang) for lang in VOICE_TASK_LANGUAGES)
        )
        candidates = {}
        request_error = None
        for lang, (text, err) in zip(VOICE_TASK_LANGUAGES, results):
            if text:
                candidates[lang] = text
            if err:
                request_error = err

        if not candidates:
            if request_error:
                raise RuntimeError(f"Speech recognition service unavailable: {request_error}")
            raise RuntimeError("Couldn't understand the audio - please try speaking more clearly.")

        if len(candidates) == 1:
            return next(iter(candidates.values()))

        # Both languages produced something - trust whichever transcript's
        # actual detected language matches the model that produced it.
        from langdetect import detect
        for lang, text in candidates.items():
            try:
                if detect(text) == VOICE_TASK_LANGDETECT_CODES.get(lang):
                    return text
            except Exception:
                logging.debug("Language detection failed", exc_info=True)
                continue
        return candidates[VOICE_TASK_LANGUAGES[0]]
    finally:
        for path in (ogg_path, wav_path):
            try:
                os.remove(path)
            except OSError:
                pass


# (timeout_seconds, attempts) for AI calls made where nobody is waiting on
# the result, i.e. while the morning summary is prepared ahead of time
# (see prepare_morning_summary). Unset everywhere else, so interactive
# features keep their short single-attempt timeouts.
_ai_patience: contextvars.ContextVar = contextvars.ContextVar("_ai_patience", default=None)
AI_RETRY_PAUSE_SECONDS = 5


# GWDG answers 429 when a quota window is used up. A burst (2 requests /
# 10 s) is waited out once; a used-up hour/day/month makes _call_gwdg_chat
# skip GWDG until it resets, so callers go straight to their fallback.
GWDG_BURST_WAIT_SECONDS = 6
GWDG_SHORT_TIMEOUT_SECONDS = 8
# Remaining/limit per quota window as of the last GWDG response, e.g.
# {"day": (312, 400), "month": (2650, 3000)}; shown by /healthcheck.
_gwdg_quota: dict = {}


# GWDG allows 2 requests per 10 s per key; spacing the bot's own calls
# keeps a burst (a voice note: transcription, intent, search, answer) from
# being refused.
GWDG_BURST_REQUESTS = 2
GWDG_BURST_WINDOW_SECONDS = 10.5
_gwdg_recent_calls: list = []
_gwdg_throttle_lock = asyncio.Lock()


async def _gwdg_throttle() -> None:
    """Wait until another GWDG request fits into the burst window."""
    async with _gwdg_throttle_lock:
        now = time.monotonic()
        del _gwdg_recent_calls[:-GWDG_BURST_REQUESTS]
        if len(_gwdg_recent_calls) == GWDG_BURST_REQUESTS:
            wait = _gwdg_recent_calls[0] + GWDG_BURST_WINDOW_SECONDS - now
            if wait > 0:
                await asyncio.sleep(wait)
        _gwdg_recent_calls.append(time.monotonic())


def _record_gwdg_quota(headers) -> None:
    for window in ("day", "month"):
        remaining = headers.get(f"x-ratelimit-remaining-requests-{window}")
        limit = headers.get(f"x-ratelimit-limit-requests-{window}")
        if remaining is not None and limit is not None:
            _gwdg_quota[window] = (int(remaining), int(limit))


async def _post_gwdg_chat(session, body: dict, timeout: float) -> tuple:
    """(status, json body or None, headers) of one GWDG chat request."""
    await _gwdg_throttle()
    async with session.post(
        f"{GWDG_API_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GWDG_API_KEY}"},
        json=body,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        _record_gwdg_quota(resp.headers)
        data = await resp.json(content_type=None) if resp.status == 200 else None
        return resp.status, data, resp.headers


async def _call_gwdg_chat(messages: list, model: str, temperature: float = 0, timeout: float = 15) -> Optional[str]:
    """One chat completion on GWDG's OpenAI-compatible API. Returns the
    assistant text, or None if unconfigured, rate limited or failed, so the
    caller can fall back to another provider."""
    if not GWDG_API_KEY or time.monotonic() < state._gwdg_blocked_until:
        return None
    body = {"model": model, "temperature": temperature, "messages": messages}
    try:
        async with shared_http() as session:
            status, data, headers = await _post_gwdg_chat(session, body, timeout)
            if status == 429:
                exhausted = next((w for w in ("month", "day", "hour") if headers.get(f"x-ratelimit-remaining-requests-{w}") == "0"), None)
                if exhausted:
                    wait = int(headers.get(f"x-ratelimit-reset-requests-{exhausted}", 3600))
                    state._gwdg_blocked_until = time.monotonic() + wait
                    logging.warning("GWDG %s quota used up, using fallbacks for %ss", exhausted, wait)
                    return None
                await asyncio.sleep(GWDG_BURST_WAIT_SECONDS)
                status, data, headers = await _post_gwdg_chat(session, body, timeout)
            if status != 200:
                logging.warning("GWDG call failed: HTTP %s", status)
                return None
            text = data["choices"][0]["message"]["content"] or ""
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or None
    except Exception as e:
        logging.warning("GWDG call failed: %s: %s", type(e).__name__, e)
        return None


# ── per-student AI budget ─────────────────────────────────────────────────────
# The GWDG/OpenRouter quotas are shared by everyone: one student can't use
# them up for all. Background work (no acting user) and admins don't count.
AI_CALLS_PER_HOUR = 60
AI_CALLS_PER_DAY = 300
AI_BUDGET_MESSAGE = "⏳ You've used a lot of AI answers recently - please try again in a while."
_ai_calls: dict[int, list[float]] = {}


def _budget_user(user_id=None) -> Optional[int]:
    uid = user_id if user_id is not None else current_user_id()
    if uid is None or is_user_admin(uid):
        return None
    return int(uid)


def _recent_ai_calls(uid: int, now: float) -> list[float]:
    calls = [t for t in _ai_calls.get(uid, []) if now - t < 86400]
    if calls:
        _ai_calls[uid] = calls
    else:
        _ai_calls.pop(uid, None)
    return calls


def ai_budget_left(user_id=None) -> bool:
    """Whether the student may start another AI call (without using one)."""
    uid = _budget_user(user_id)
    if uid is None:
        return True
    now = time.time()
    calls = _recent_ai_calls(uid, now)
    return len(calls) < AI_CALLS_PER_DAY and sum(1 for t in calls if now - t < 3600) < AI_CALLS_PER_HOUR


def use_ai_budget(user_id=None) -> bool:
    """Count one AI call for the student; False (and nothing counted) when
    their hourly or daily budget is used up."""
    uid = _budget_user(user_id)
    if uid is None:
        return True
    if not ai_budget_left(uid):
        logging.info("AI budget used up for user %s", uid)
        return False
    _ai_calls.setdefault(uid, []).append(time.time())
    return True


async def _call_openrouter(system_prompt: str, user_text: str, temperature: float = 0, timeout: Optional[float] = None, reasoning: bool = True, model: Optional[str] = None, gwdg_model: Optional[str] = None) -> Optional[str]:
    """One text completion: GWDG (GWDG_TEXT_MODEL) first, OpenRouter if that
    fails. Retried and with a longer timeout while _ai_patience is set. An
    explicit timeout always wins. `gwdg_model` overrides GWDG_TEXT_MODEL;
    `reasoning` and `model` only apply to the OpenRouter call."""
    if not use_ai_budget():
        return None
    patience = _ai_patience.get()
    timeout = timeout or (patience[0] if patience else 15)
    attempts = patience[1] if patience else 1
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_text}]
    # GWDG usually answers in 1-3 s but sometimes stalls for 15 s+; on the
    # short interactive calls, give up on it early and let OpenRouter answer.
    gwdg_timeout = timeout if timeout > 15 else min(timeout, GWDG_SHORT_TIMEOUT_SECONDS)
    for attempt in range(attempts):
        result = await _call_gwdg_chat(messages, gwdg_model or GWDG_TEXT_MODEL, temperature, gwdg_timeout)
        if result is None:
            result = await _call_openrouter_once(system_prompt, user_text, temperature, timeout, reasoning, model)
        if result is not None:
            return result
        if attempt + 1 < attempts:
            logging.info("OpenRouter call: retrying (%s/%s)", attempt + 2, attempts)
            await asyncio.sleep(AI_RETRY_PAUSE_SECONDS)
    return None


async def _call_openrouter_once(system_prompt: str, user_text: str, temperature: float = 0, timeout: float = 15, reasoning: bool = True, model: Optional[str] = None) -> Optional[str]:
    """POST one chat completion to OpenRouter's free tier and return the raw
    assistant text, or None on any failure (missing key, network error,
    timeout, non-2xx response). Callers must treat None as "couldn't
    understand" and degrade gracefully rather than raising.

    `temperature` defaults to 0 (deterministic) for the structured-output
    callers (intent classification, extraction) that need consistent JSON.
    Pass a higher value (e.g. 1.0+) for free-form creative text like the
    daily-plan narration, where 0 would make the model converge on the same
    wording every time - deterministic sampling with the same prompt tends
    to produce the same greedy completion.

    `timeout` (seconds) can be raised for slower structured-extraction
    calls. `reasoning=False` turns off the
    model's thinking phase for prompts where it adds minutes but little
    quality (see pick_outfit_from_wardrobe). `model` overrides
    OPENROUTER_MODEL for one call."""
    if not OPENROUTER_API_KEY:
        return None
    try:
        async with shared_http() as session:
            async with session.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model or OPENROUTER_MODEL,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text},
                    ],
                    **({} if reasoning else {"reasoning": {"enabled": False}}),
                },
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    logging.warning("OpenRouter call failed: HTTP %s", resp.status)
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        logging.warning("OpenRouter call failed: %s: %s", type(e).__name__, e)
        return None


async def synthesize_speech(text: str) -> Optional[tuple]:
    """_synthesize_speech_once, retried and with a longer timeout while
    _ai_patience is set (see _call_openrouter)."""
    if not use_ai_budget():
        return None
    patience = _ai_patience.get()
    timeout = max(30, patience[0]) if patience else 30
    attempts = patience[1] if patience else 1
    for attempt in range(attempts):
        result = await _synthesize_speech_once(text, timeout)
        if result is not None:
            return result
        if attempt + 1 < attempts:
            logging.info("TTS call: retrying (%s/%s)", attempt + 2, attempts)
            await asyncio.sleep(AI_RETRY_PAUSE_SECONDS)
    return None


async def _synthesize_speech_once(text: str, timeout: float = 30) -> Optional[tuple]:
    """Convert text to speech via OpenRouter's free-tier TTS endpoint
    (/api/v1/audio/speech - a separate API surface from chat/completions,
    so it isn't listed in /api/v1/models and doesn't go through
    _call_openrouter). Returns (pcm_bytes, sample_rate) - raw 16-bit mono
    PCM, whose exact sample rate is read back from the response's
    Content-Type (e.g. "audio/pcm;rate=24000;channels=1") rather than
    assumed, since it can differ by model. Returns None on any failure
    (missing key, network error, non-2xx) - callers must degrade
    gracefully (e.g. fall back to sending the plain text)."""
    if not OPENROUTER_API_KEY:
        return None
    payload = {"model": OPENROUTER_TTS_MODEL, "input": text}
    if OPENROUTER_TTS_VOICE:
        # Only some TTS models take a "voice" selector - sending one a model
        # doesn't support (e.g. the previous model's voice name against a
        # different model) gets a 400 back instead of just being ignored.
        payload["voice"] = OPENROUTER_TTS_VOICE
    try:
        async with shared_http() as session:
            async with session.post(
                OPENROUTER_TTS_URL,
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.warning("TTS call failed: HTTP %s: %s", resp.status, body[:200])
                    return None
                content_type = resp.headers.get("Content-Type", "")
                sample_rate = 24000
                m = re.search(r"rate=(\d+)", content_type)
                if m:
                    sample_rate = int(m.group(1))
                pcm_bytes = await resp.read()
                return pcm_bytes, sample_rate
    except Exception as e:
        logging.warning("TTS call failed: %s: %s", type(e).__name__, e)
        return None


async def _pcm_to_ogg_voice(pcm_bytes: bytes, sample_rate: int) -> Optional[str]:
    """Convert raw 16-bit mono PCM audio to an OGG/Opus file via ffmpeg -
    the format Telegram's send_voice needs for a proper voice-message
    bubble (not just a generic audio attachment). Returns the temp file
    path (caller must remove it when done), or None if ffmpeg is missing
    or the conversion fails."""
    pcm_path = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.pcm")
    ogg_path = pcm_path + ".ogg"
    await awrite_bytes(pcm_path, pcm_bytes)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", pcm_path,
            "-c:a", "libopus", "-b:a", "48k", "-y", ogg_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(ogg_path):
            logging.warning("_pcm_to_ogg_voice: ffmpeg exited %s", proc.returncode)
            return None
        return ogg_path
    except FileNotFoundError:
        logging.warning("_pcm_to_ogg_voice: ffmpeg not installed")
        return None
    finally:
        try:
            os.remove(pcm_path)
        except OSError:
            pass


async def generate_outfit_tryon_gwdg(reference_bytes: bytes, garments: list) -> Optional[bytes]:
    """Free fallback for generate_outfit_tryon_gemini on GWDG. Its image
    edit model (Qwen-Image-Edit-2511) swaps in a different person when it
    gets the garment photos too, so it only sees the reference photo plus
    each garment's text description (see describe_wardrobe_item): the
    person stays, the garments come out close but less exact than Nano
    Banana. Items without a stored description are described now and the
    description saved. Returns the PNG bytes, or None if unconfigured or a
    call failed."""
    from unibot.wardrobe import describe_wardrobe_item
    if not GWDG_API_KEY or not garments:
        return None
    lines = []
    for item, data in garments:
        text = item.get("description")
        if not text:
            text = await describe_wardrobe_item(item, data)
            if not text:
                return None
            item["description"] = text
            save_wardrobe_item_descriptions({item["id"]: text})
        lines.append(f"- {item.get('label', 'clothing item')} ({item.get('type', 'item')}): {text}")
    description = "\n".join(lines)
    prompt = (
        "Dress the man in Picture 1 in this outfit, each item reproduced exactly as described:\n"
        f"{description}\n"
        "Outerwear worn open over the top, shoes on the feet, any cap or beanie on the head. Keep him exactly "
        "the same person: identical face, beard, hair, skin tone, body shape, pose, framing, background and "
        "lighting. Trousers are full length, reaching the shoes with a natural break, never cropped or "
        "tapered. Plain white crew socks. Photorealistic full-body photo from head to feet, no text, no collage."
    )
    form = aiohttp.FormData()
    form.add_field("prompt", prompt)
    form.add_field("image", reference_bytes, filename="picture1.jpg", content_type="image/jpeg")
    headers = {"Authorization": f"Bearer {GWDG_API_KEY}", "inference-service": GWDG_IMAGE_EDIT_SERVICE}
    try:
        await _gwdg_throttle()
        async with shared_http() as session:
            async with session.post(f"{GWDG_API_URL}/images/edits", headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                _record_gwdg_quota(resp.headers)
                body = await resp.read()
                if resp.status == 200 and resp.content_type.startswith("image/"):
                    return body
                logging.warning("GWDG try-on failed: HTTP %s %r", resp.status, body[:200])
                return None
    except Exception as e:
        logging.warning("GWDG try-on failed: %s", e)
        return None


def _extract_json_object(raw: Optional[str]) -> Optional[dict]:
    """Parse the JSON object out of a model response, tolerating any prose
    the model wraps it in despite being asked for strict JSON. A fenced
    ```json block wins; otherwise the first object that parses on its own,
    so an example in braces before the answer doesn't spoil it."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for fence in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL):
        found = _first_json_object(decoder, fence.group(1))
        if found is not None:
            return found
    return _first_json_object(decoder, raw)


def _first_json_object(decoder: json.JSONDecoder, text: str) -> Optional[dict]:
    """First top-level JSON object in free text (nested braces are fine)."""
    start = text.find("{")
    while start != -1:
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return None


async def check_openrouter_health(live: bool = False) -> tuple[bool, str]:
    """(ok, detail). `live=False` (used by the automatic periodic check,
    to avoid spending API quota every cycle) only confirms the API key is
    configured; `live=True` (used by the on-demand /healthcheck command)
    makes one real, minimal completion call."""
    api_key = (OPENROUTER_API_KEY or "").strip()
    if not api_key:
        return False, "OPENROUTER_API_KEY not set"
    if not live:
        return True, "Key configured"
    try:
        result = await _call_openrouter("Reply with exactly one word: OK", "ping", temperature=0)
        if result and "OK" in result.upper():
            return True, "OK"
        return False, f"Unexpected response: {result!r}"
    except Exception as e:
        return False, str(e)[:150]


async def check_gwdg_health(live: bool = False) -> tuple[bool, str]:
    """(ok, detail). Optional service, so an unset key is fine. Reports
    what's left of the shared daily/monthly quota as of the last GWDG
    response; `live=True` makes one minimal call first, which also
    refreshes those numbers. A used-up quota counts as a failure (the
    fallbacks are in use until it resets)."""
    if not GWDG_API_KEY:
        return True, "Not configured (optional)"
    if live:
        result = await _call_gwdg_chat([{"role": "user", "content": "Reply with exactly one word: OK"}], GWDG_TEXT_MODEL, timeout=20)
        if not result:
            return False, "No answer, OpenRouter/Cloudflare fallbacks in use"
    quota = " · ".join(f"{remaining}/{limit} left {'today' if window == 'day' else 'this month'}" for window, (remaining, limit) in _gwdg_quota.items())
    if time.monotonic() < state._gwdg_blocked_until:
        resume = datetime.now(TZ_BERLIN) + timedelta(seconds=state._gwdg_blocked_until - time.monotonic())
        return False, f"Quota used up, fallbacks in use until {resume:%d.%m %H:%M}" + (f" ({quota})" if quota else "")
    return True, ("OK · " + quota) if quota else ("OK" if live else "Key configured")
