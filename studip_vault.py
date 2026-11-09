"""Encryption vault for Stud.IP session cookies and OAuth2 tokens at rest.

Provides AES-128-CBC + HMAC-SHA256 authenticated encryption via cryptography.fernet.Fernet.
Encryption key:
  1. SESSION_ENCRYPTION_KEY environment variable, if set
  2. otherwise a random key generated once into `.vault_key` (mode 0600)
     next to this file

Files written by older versions (key derived from TELEGRAM_TOKEN, or the
built-in fallback secret) are still readable and get re-encrypted with the
current key on first read. Rotating TELEGRAM_TOKEN therefore no longer
locks the stored session out.

Note: the key lives on the same machine as the encrypted files, so this
protects copies of the files (backups, accidental commits), not a full
compromise of the host.

Supports:
  - Atomic file write with strict 0600 (read/write only by owner) permissions
  - Transparent in-place migration of unencrypted legacy JSON files
  - Graceful fallback and clear logging
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("StudIPVault")

# Signature prefix for encrypted vault files
ENCRYPTED_MAGIC = "VAULT:v1:"


KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vault_key")


def _key_from_secret(secret: str) -> bytes:
    """Domain-separated SHA-256 of a high-entropy secret, as a Fernet key."""
    digest = hashlib.sha256(b"studip-vault-key-derivation-v1:" + secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _load_or_create_key_file() -> bytes:
    from cryptography.fernet import Fernet

    try:
        with open(KEY_FILE, "rb") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = Fernet.generate_key()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(fd, "wb") as fh:
        fh.write(key)
    logger.info("Generated a new vault key in %s", KEY_FILE)
    return key


def _primary_key() -> bytes:
    explicit = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
    if explicit:
        return _key_from_secret(explicit)
    return _load_or_create_key_file()


def _legacy_keys() -> list[bytes]:
    """Keys older versions encrypted with, accepted for reading only. (The
    hard-coded fallback secret of the first versions is gone: no record on
    it was left once every student's data moved to per-user keys.)"""
    keys = []
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if telegram_token:
        keys.append(_key_from_secret(telegram_token))
    return keys


def encrypt_payload(data: Any) -> str:
    """Serialize data to JSON and encrypt with Fernet, prefixing with ENCRYPTED_MAGIC."""
    from cryptography.fernet import Fernet

    f = Fernet(_primary_key())
    raw_json = json.dumps(data, ensure_ascii=False).encode("utf-8")
    token = f.encrypt(raw_json).decode("utf-8")
    return f"{ENCRYPTED_MAGIC}{token}"


def _decrypt_token(cipher_token: str) -> tuple[Optional[Any], bool]:
    """(data, used_legacy_key). data is None when no known key fits."""
    from cryptography.fernet import Fernet, InvalidToken

    raw = cipher_token.encode("utf-8")
    primary = _primary_key()
    for key in [primary] + [k for k in _legacy_keys() if k != primary]:
        try:
            plain = Fernet(key).decrypt(raw)
        except InvalidToken:
            continue
        return json.loads(plain.decode("utf-8")), key != primary
    return None, False


def decrypt_payload(raw_text: str, _legacy_flag: Optional[list] = None) -> Optional[Any]:
    """Decrypt payload if encrypted, or parse legacy plain JSON if not.

    `_legacy_flag`, when given, gets True appended if an old key was needed,
    so the caller can re-encrypt the file with the current key."""
    raw_text = raw_text.strip()
    if not raw_text:
        return None

    if raw_text.startswith(ENCRYPTED_MAGIC) or raw_text.startswith("gAAAAA"):
        cipher_token = raw_text[len(ENCRYPTED_MAGIC):].strip() if raw_text.startswith(ENCRYPTED_MAGIC) else raw_text
        try:
            data, used_legacy = _decrypt_token(cipher_token)
        except Exception as exc:
            logger.error("Failed to decrypt vault payload: %s", exc)
            return None
        if data is None:
            logger.error("Failed to decrypt vault payload: no known key matches (key changed?)")
            return None
        if used_legacy and _legacy_flag is not None:
            _legacy_flag.append(True)
        return data

    # Fallback: Plaintext legacy JSON (migration scenario)
    try:
        return json.loads(raw_text)
    except Exception as exc:
        logger.warning("Failed to parse plaintext payload: %s", exc)
        return None


# ── per-user keys ─────────────────────────────────────────────────────────────
# A student's credentials and private data are encrypted with a key of their
# own: HKDF-SHA256 of the vault key with a random per-record salt and the
# student's Telegram id as context. A blob copied into another student's row
# does not decrypt there, and the database alone reveals nothing.
USER_MAGIC = "VAULT:u1:"


def _user_key(user_id: int, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=f"unibot-user-vault:{int(user_id)}".encode("ascii"))
    return base64.urlsafe_b64encode(hkdf.derive(base64.urlsafe_b64decode(_primary_key())))


def encrypt_user_payload(user_id: int, data: Any) -> str:
    """Encrypt JSON-able data with `user_id`'s own key (fresh salt per call)."""
    from cryptography.fernet import Fernet

    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("a user id is required for personal data")
    salt = os.urandom(16)
    token = Fernet(_user_key(user_id, salt)).encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return f"{USER_MAGIC}{base64.urlsafe_b64encode(salt).decode('ascii')}:{token.decode('utf-8')}"


def decrypt_user_payload(user_id: int, raw_text: str, _legacy_flag: Optional[list] = None) -> Optional[Any]:
    """Decrypt a per-user blob of `user_id`. Older shared-key blobs are still
    read; `_legacy_flag` then gets True appended so the caller re-encrypts."""
    from cryptography.fernet import Fernet, InvalidToken

    raw_text = (raw_text or "").strip()
    if not raw_text:
        return None
    if not raw_text.startswith(USER_MAGIC):
        if _legacy_flag is not None:
            _legacy_flag.append(True)
        return decrypt_payload(raw_text)
    try:
        salt_b64, token = raw_text[len(USER_MAGIC):].split(":", 1)
        plain = Fernet(_user_key(user_id, base64.urlsafe_b64decode(salt_b64))).decrypt(token.encode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        logger.error("Failed to decrypt a personal vault record (%s)", type(exc).__name__)
        return None
    return json.loads(plain.decode("utf-8"))


def write_vault_file(path: str, data: Any, mode: int = 0o600) -> None:
    """Atomically write encrypted data with strict file permissions (0600)."""
    cipher_text = encrypt_payload(data)
    tmp_path = f"{path}.tmp"

    # Open with O_CREAT | O_WRONLY | O_TRUNC and exact permissions 0600
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(cipher_text)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    try:
        os.chmod(tmp_path, mode)
    except OSError:
        pass

    os.replace(tmp_path, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def read_vault_file(path: str, auto_migrate: bool = True) -> Optional[Any]:
    """Read and decrypt file. If file is legacy unencrypted JSON, decrypt and auto-migrate if requested."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except OSError as exc:
        logger.warning("Could not read vault file %s: %s", path, exc)
        return None

    legacy_flag: list = []
    data = decrypt_payload(raw_text, legacy_flag)
    if data is None:
        return None

    # Unencrypted, encrypted with an old key, or looser than 0600: rewrite now
    is_plain = bool(legacy_flag) or (
        not raw_text.startswith(ENCRYPTED_MAGIC) and not raw_text.startswith("gAAAAA")
    )
    needs_chmod = False
    try:
        st = os.stat(path)
        if stat.S_IMODE(st.st_mode) != 0o600:
            needs_chmod = True
    except OSError:
        pass

    if auto_migrate and (is_plain or needs_chmod):
        logger.info("Migrating vault file %s to encrypted at-rest storage with 0600 permissions", path)
        try:
            write_vault_file(path, data)
        except Exception as exc:
            logger.warning("Could not auto-migrate %s: %s", path, exc)

    return data
