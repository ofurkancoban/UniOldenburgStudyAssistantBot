"""OAuth2 (PKCE) token layer for Stud.IP.

Replaces the stored password for API access with an access token plus a
rotating refresh token.

The flow was reverse-engineered from a live session (Nov 2026):

  1. GET  /dispatch.php/api/oauth2/authorize
            ?client_id=1&response_type=code
            &code_challenge=<BASE64URL(SHA256(verifier))>
            &code_challenge_method=S256&state=<random>
     -> 200 consent page. This requires an already logged-in web session,
        so OAuth sits *on top of* the normal SSO login, it does not replace it.

  2. POST the form that carries the "allow" button:
            security_token, state, client_id, auth_token, allow=""
     Note: `allow` must be present with an EMPTY value - `allow=1` is read as
     a rejection and yields `error=access_denied`.
     -> 302 Location: studyloft://oauth?code=<code>&state=<state>

  3. POST /dispatch.php/api/oauth2/token
            grant_type=authorization_code&code=..&client_id=1&code_verifier=..
     Note: `redirect_uri` must be OMITTED. Sending it (even the correct
     value) makes the plugin answer `invalid_request`.

  4. POST the same endpoint with
            grant_type=refresh_token&refresh_token=..&client_id=1

The resulting access token is a JWT and is accepted by the JSON:API only
(`/jsonapi.php/v1/...`); regular `dispatch.php` HTML pages still need the
cookie session.
"""

from __future__ import annotations

import asyncio
import contextvars
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from studip_http import THROTTLE, parse_retry_after, user_agent
from studip_utils import route_proxy_url

logger = logging.getLogger("StudIPOAuth")

BASE_URL = "https://elearning.uni-oldenburg.de"
AUTHORIZE_URL = f"{BASE_URL}/dispatch.php/api/oauth2/authorize"
TOKEN_URL = f"{BASE_URL}/dispatch.php/api/oauth2/token"
CLIENT_ID = os.getenv("STUDIP_OAUTH_CLIENT_ID", "1")
TOKEN_FILE = os.getenv("STUDIP_OAUTH_TOKEN_FILE", "studip_oauth_tokens.json")

# Refresh this many seconds before the access token actually expires.
EXPIRY_LEEWAY = 120
# After a refresh fails for a transient reason (network, 5xx), wait this long
# before trying again instead of hitting the token endpoint on every API call.
REFRESH_RETRY_BACKOFF = 60

# one lock and one backoff per account: a failed refresh of one student must
# not hold back everybody else's token (None is the shared admin account)
_refresh_locks: dict = {}
_refresh_blocked: dict = {}
_token_generations: dict[Optional[int], int] = {}
_refresh_generation_override: contextvars.ContextVar[Optional[tuple[Optional[int], int]]] = (
    contextvars.ContextVar("oauth_refresh_generation", default=None)
)


def _token_generation(user_id: Optional[int]) -> int:
    return _token_generations.get(user_id, 0)


def _refresh_lock_for(user_id) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _refresh_locks.get(user_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _refresh_locks[user_id] = entry
    return entry[1]


class RefreshRejected(RuntimeError):
    """The server refused the refresh token itself (revoked, rotated, expired)."""


def oauth_enabled() -> bool:
    """Whether JSON:API calls should carry the OAuth2 bearer token (USE_STUDIP_OAUTH)."""
    return os.getenv("USE_STUDIP_OAUTH", "false").strip().lower() in ("true", "1", "yes")


# ── helpers ───────────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _now() -> float:
    return time.time()


def _oauth_error(text: str) -> str:
    """Extract the human-readable reason from the plugin's HTML error page."""
    soup = BeautifulSoup(text, "html.parser")
    box = soup.find(id="messagebox-0")
    if box:
        msg = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
        return msg.replace("Error! Please contact your system administrator.", "").strip()
    return text[:200].replace("\n", " ")


def _validated_tokens(value) -> Optional[dict]:
    """Reject unusable credentials and make malformed expiry metadata expire now."""
    if not isinstance(value, dict):
        return None
    access_token = value.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    tokens = dict(value)
    tokens["access_token"] = access_token
    refresh_token = tokens.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        tokens.pop("refresh_token", None)
    for key in ("expires_at", "refreshed_at"):
        if key not in tokens:
            continue
        raw = tokens.get(key, 0)
        try:
            parsed = float(raw) if not isinstance(raw, bool) else 0.0
        except (TypeError, ValueError, OverflowError):
            parsed = 0.0
        tokens[key] = parsed if math.isfinite(parsed) and parsed >= 0 else 0.0
    return tokens


# ── token storage ─────────────────────────────────────────────────────────────

def _read_token_file(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        from studip_vault import read_vault_file
        tokens = read_vault_file(path, auto_migrate=True)
        if tokens is None:
            with open(path, "r", encoding="utf-8") as fh:
                tokens = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    return _validated_tokens(tokens)


def load_tokens(user_id: Optional[int] = None) -> Optional[dict]:
    """Stored tokens, checking DB vault first if user_id is provided, falling back to files."""
    if user_id is not None:
        try:
            from unibot import db
            tokens = db.get_user_scope(user_id).credential("oauth_tokens")
            tokens = _validated_tokens(tokens)
            if tokens is not None:
                return tokens
        except Exception as exc:
            logger.warning("Could not load tokens from DB for user %s: %s", user_id, exc)

        # The shared token file belongs only to the primary admin from the
        # single-user installation. A second administrator must not inherit
        # that account's bearer token when their own DB credential is absent.
        try:
            from unibot.config import PRIMARY_ADMIN_ID
        except Exception:
            logging.debug("Could not resolve the legacy OAuth token owner", exc_info=True)
            return None
        if user_id != PRIMARY_ADMIN_ID:
            return None

    tokens = _read_token_file(TOKEN_FILE)
    if tokens is not None:
        return tokens
    bak = f"{TOKEN_FILE}.bak"
    tokens = _read_token_file(bak)
    if tokens is not None:
        logger.warning("Restored OAuth tokens from %s", bak)
        try:
            save_tokens(tokens, fresh=False, user_id=user_id)
        except Exception as exc:
            logger.warning("Could not persist restored OAuth tokens for user %s: %s", user_id, exc)
    return tokens


def save_tokens(tokens: dict, fresh: bool = True, user_id: Optional[int] = None) -> None:
    """Write encrypted tokens atomically with 0600 - these are account credentials.

    `fresh` marks a token response the server just issued: only then are
    `expires_at` and `refreshed_at` derived from `expires_in`. Re-saving
    stored tokens (e.g. restoring the .bak file) keeps their real expiry, so
    a stale access token never looks valid again."""
    normalized = _validated_tokens(tokens)
    if normalized is None:
        raise ValueError("OAuth tokens must include a non-empty access_token")
    tokens = normalized
    now = _now()
    if fresh or "expires_at" not in tokens:
        try:
            lifetime = float(tokens.get("expires_in", 3600))
        except (TypeError, ValueError, OverflowError):
            lifetime = 0.0
        if not math.isfinite(lifetime) or lifetime < 0:
            lifetime = 0.0
        tokens["expires_at"] = now + lifetime
    if fresh or "refreshed_at" not in tokens:
        tokens["refreshed_at"] = now

    if user_id is not None:
        try:
            from unibot import db
            db.get_user_scope(user_id).save_credential("oauth_tokens", tokens)
            logger.info("Tokens encrypted and saved to DB for user %s (expires in %ss)", user_id, tokens.get("expires_in", 3600))
        except Exception as exc:
            logger.error("Could not save tokens to DB for user %s: %s", user_id, exc)
            raise

    # A named student's tokens live only in their encrypted credential row
    # (their own key); the shared-key token file is for the account-less
    # single-user setup alone.
    if user_id is None:
        from studip_vault import write_vault_file
        for target in (TOKEN_FILE, f"{TOKEN_FILE}.bak"):
            write_vault_file(target, tokens, mode=0o600)
        logger.info("Tokens encrypted and saved to %s (mode 0600, expires in %ss)", TOKEN_FILE, tokens.get("expires_in", 3600))


def clear_tokens(user_id: Optional[int] = None) -> None:
    """Remove stored OAuth2 token files from disk or DB."""
    # Invalidate token exchanges and refreshes that are currently awaiting
    # the upstream. Their old responses must not recreate a credential after
    # logout or personal-data deletion.
    _token_generations[user_id] = _token_generation(user_id) + 1
    _refresh_blocked.pop(user_id, None)
    if user_id is not None:
        try:
            from unibot import db
            db.get_user_scope(user_id).delete_credential("oauth_tokens")
        except Exception as exc:
            logger.warning("Could not clear tokens from DB for user %s: %s", user_id, exc)

    # During single-user to multi-user migration, the primary admin may still
    # read their old shared token file as a fallback. Leaving it behind after
    # deleting the DB credential makes the next load silently sign them back
    # in, including after the Privacy screen deletes their credentials.
    owns_legacy_file = user_id is None
    if user_id is not None:
        try:
            from unibot.config import PRIMARY_ADMIN_ID
            owns_legacy_file = user_id == PRIMARY_ADMIN_ID
        except Exception:
            logger.debug("Could not resolve the legacy OAuth token owner", exc_info=True)

    if owns_legacy_file:
        for path in (TOKEN_FILE, f"{TOKEN_FILE}.bak", f"{TOKEN_FILE}.tmp", f"{TOKEN_FILE}.bak.tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)


def is_expired(tokens: Optional[dict], leeway: int = EXPIRY_LEEWAY) -> bool:
    if not tokens:
        return True
    expires_at = tokens.get("expires_at", 0)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
        return True
    return expires_at - leeway <= _now()


# ── consent flow (needs a logged-in session) ──────────────────────────────────

async def authorize_with_session(session, timeout: int = 30, user_id: Optional[int] = None) -> dict:
    """Run the full authorization-code flow against a logged-in StudIPSession.

    `session` is a `studip_session.StudIPSession` whose cookie jar already
    carries a valid web session. Returns the token dict.
    """
    effective_user_id = user_id or getattr(session, "user_id", None)
    token_generation = _token_generation(effective_user_id)
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(os.urandom(16))

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    logger.info("Requesting consent page...")
    async with await session.get(AUTHORIZE_URL, params=params, allow_redirects=False, timeout=timeout) as resp:
        status = resp.status
        body = await resp.text()
        location = resp.headers.get("Location", "")

    # Already-authorized clients may skip the consent screen entirely.
    if status in (301, 302, 303, 307) and "code=" in location:
        return await _exchange_code(session, location, verifier, state, user_id=effective_user_id,
                                    token_generation=token_generation)

    if status != 200:
        raise RuntimeError(f"authorize returned HTTP {status}: {_oauth_error(body)}")

    soup = BeautifulSoup(body, "html.parser")
    form = next((f for f in soup.find_all("form")
                 if f.find("button", attrs={"name": "allow"})), None)
    if form is None:
        raise RuntimeError(
            "No consent form found - the web session is probably not logged in. "
            f"Page said: {_oauth_error(body)}"
        )

    payload = {i["name"]: (i.get("value") or "")
               for i in form.find_all("input") if i.get("name")}
    payload["allow"] = ""  # empty value == approve; "1" is read as a rejection

    logger.info("Submitting consent...")
    async with await session.post(AUTHORIZE_URL, data=payload, allow_redirects=False, timeout=timeout) as resp:
        status = resp.status
        location = resp.headers.get("Location", "")
        body = await resp.text()

    if "code=" not in location:
        raise RuntimeError(
            f"Consent rejected (HTTP {status}, Location={location or '-'}): "
            f"{_oauth_error(body)}"
        )
    return await _exchange_code(session, location, verifier, state, user_id=effective_user_id,
                                token_generation=token_generation)


async def _exchange_code(session, redirect_location: str, verifier: str, state: str,
                         user_id: Optional[int] = None, token_generation: Optional[int] = None) -> dict:
    try:
        response_params = parse_qs(
            urlsplit(redirect_location).query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise RuntimeError("Malformed OAuth authorization redirect.") from exc

    codes = response_params.get("code", [])
    states = response_params.get("state", [])
    if len(codes) != 1 or not codes[0]:
        raise RuntimeError("Authorization redirect did not contain exactly one code.")
    if len(states) != 1 or not hmac.compare_digest(states[0], state):
        raise RuntimeError("OAuth state mismatch in the authorization redirect; refusing the code.")
    code = codes[0]
    logger.info("Authorization code received.")

    # redirect_uri is deliberately absent - see the module docstring.
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }
    async with await session.post(TOKEN_URL, data=data, allow_redirects=False, timeout=30) as resp:
        status = resp.status
        text = await resp.text()

    if status != 200 or not text.lstrip().startswith("{"):
        raise RuntimeError(f"Token exchange failed (HTTP {status}): {_oauth_error(text)}")

    tokens = _validated_tokens(json.loads(text))
    if tokens is None:
        raise RuntimeError("Token exchange returned an invalid token payload.")
    if token_generation is not None and token_generation != _token_generation(user_id):
        raise RuntimeError("OAuth authorization was cancelled because the account signed out.")
    save_tokens(tokens, user_id=user_id)
    return tokens


# ── refresh ───────────────────────────────────────────────────────────────────

async def refresh(tokens: Optional[dict] = None, user_id: Optional[int] = None) -> dict:
    """Trade the refresh token for a new access token (rotates the refresh token)."""
    # get_access_token binds the generation captured with its token load.
    # Otherwise a logout between load and refresh could make an old token
    # look current again.
    override = _refresh_generation_override.get()
    token_generation = (
        override[1] if override is not None and override[0] == user_id
        else _token_generation(user_id)
    )
    tokens = tokens or load_tokens(user_id=user_id)
    if not tokens or not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]:
        raise RuntimeError("No refresh token stored; run a fresh login.")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID,
    }
    # Token refresh carries no cookies, so it needs no user session; it is
    # still routed through the optional worker and the shared host throttle.
    routed_tok, tok_headers = route_proxy_url(TOKEN_URL, {"User-Agent": user_agent()})
    host = urlsplit(TOKEN_URL).hostname or ""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
        async with THROTTLE.slot(host):
            try:
                resp_cm = http.post(routed_tok, data=data, headers=tok_headers, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=30))
                resp = await resp_cm
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                THROTTLE.report(host, failed=True)
                raise
        async with resp:
            THROTTLE.report(host, resp.status, parse_retry_after(resp.headers.get("Retry-After")))
            status = resp.status
            text = await resp.text()

    if status == 401 or (status == 400 and "invalid_grant" in text):
        raise RefreshRejected(f"Refresh token rejected (HTTP {status}): {_oauth_error(text)}")
    if status != 200 or not text.lstrip().startswith("{"):
        raise RuntimeError(f"Refresh failed (HTTP {status}): {_oauth_error(text)}")

    new_tokens = _validated_tokens(json.loads(text))
    if new_tokens is None:
        raise RuntimeError("Refresh returned an invalid token payload.")
    if not new_tokens.get("refresh_token") and tokens.get("refresh_token"):
        new_tokens["refresh_token"] = tokens["refresh_token"]
    if token_generation != _token_generation(user_id):
        raise RuntimeError("OAuth refresh was cancelled because the account signed out.")
    save_tokens(new_tokens, user_id=user_id)
    return new_tokens


async def get_access_token(force_refresh: bool = False, user_id: Optional[int] = None) -> Optional[str]:
    """A currently valid access token, refreshing when needed.

    Serialized with an asyncio lock so concurrent API calls never consume the
    same rotating refresh_token twice. Returns None when there is no token
    file at all (caller should fall back to the cookie session).
    """
    tokens = load_tokens(user_id=user_id)
    if tokens is None:
        return None
    if not force_refresh and not is_expired(tokens):
        return tokens.get("access_token")

    async with _refresh_lock_for(user_id):
        token_generation = _token_generation(user_id)
        tokens = load_tokens(user_id=user_id)
        if tokens is None:
            return None
        # If another coroutine just refreshed the token while we waited for the lock,
        # reuse the fresh token instead of rotating again.
        refreshed_at = tokens.get("refreshed_at", 0)
        refresh_age = _now() - refreshed_at if (
            isinstance(refreshed_at, (int, float)) and not isinstance(refreshed_at, bool)
            and math.isfinite(refreshed_at)
        ) else None
        recently_refreshed = (
            refresh_age is not None and -10 <= refresh_age < 10
        )
        if not is_expired(tokens) and (not force_refresh or recently_refreshed):
            return tokens.get("access_token")
        if _now() < _refresh_blocked.get(user_id, 0.0):
            return None if is_expired(tokens, leeway=0) else tokens.get("access_token")
        try:
            generation_token = _refresh_generation_override.set((user_id, token_generation))
            try:
                tokens = await refresh(tokens, user_id=user_id)
            finally:
                _refresh_generation_override.reset(generation_token)
            _refresh_blocked.pop(user_id, None)
            return tokens.get("access_token")
        except RefreshRejected as exc:
            # A dead refresh token never recovers; drop it so callers fall
            # back to the cookie session instead of retrying on every request.
            logger.warning("%s - clearing stored OAuth tokens, a fresh login is needed.", exc)
            clear_tokens(user_id=user_id)
            return None
        except Exception as exc:
            logger.warning("Token refresh failed: %s (retrying in %ss)", exc, REFRESH_RETRY_BACKOFF)
            _refresh_blocked[user_id] = _now() + REFRESH_RETRY_BACKOFF
            if is_expired(tokens, leeway=0):
                return None
        return tokens.get("access_token")


def auth_headers_sync() -> dict:
    """Bearer header for a token that is still valid, else {}."""
    tokens = load_tokens()
    if tokens and not is_expired(tokens):
        return {"Authorization": f"Bearer {tokens['access_token']}"}
    return {}


def describe_tokens() -> str:
    tokens = load_tokens()
    if not tokens:
        return "No token file."
    left = int(tokens.get("expires_at", 0) - _now())
    return (f"access_token: {tokens['access_token'][:20]}...  "
            f"valid for {left}s  "
            f"refresh_token: {'yes' if tokens.get('refresh_token') else 'no'}")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def _cli_login() -> int:
    from dotenv import load_dotenv
    from studip_session import StudIPSession

    load_dotenv()
    user, password, totp = (os.getenv("USERNAME"), os.getenv("PASSWORD"),
                            os.getenv("TOTP_SECRET"))
    if not user or not password:
        print("USERNAME / PASSWORD missing from the environment.")
        return 1

    session = StudIPSession(user, password, totp)
    print("Logging in to Stud.IP (SSO)...")
    if not await session.login(force=True):
        print("SSO login failed.")
        return 1
    print("Running the OAuth consent flow...")
    await authorize_with_session(session)
    await session.close()
    print(f"OK - {describe_tokens()}")
    print(f"Stored in {TOKEN_FILE}")
    return 0


async def _cli_refresh() -> int:
    await refresh()
    print(describe_tokens())
    return 0


def main() -> int:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "login":
        return asyncio.run(_cli_login())
    if cmd == "refresh":
        return asyncio.run(_cli_refresh())
    if cmd == "status":
        print(describe_tokens())
        return 0
    if cmd == "token":
        print(asyncio.run(get_access_token()) or "")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
