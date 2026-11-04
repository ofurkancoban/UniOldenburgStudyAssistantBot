"""Encryption vault for Stud.IP session cookies and OAuth2 tokens at rest.

Provides AES-128-CBC + HMAC-SHA256 authenticated encryption via cryptography.fernet.Fernet.
Derives encryption key from:
  1. SESSION_ENCRYPTION_KEY environment variable (if set)
  2. TELEGRAM_TOKEN environment variable (primary default)
  3. Stable local fallback secret

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


def _derive_fernet_key() -> bytes:
    """Derive a 32-byte URL-safe base64-encoded key required by Fernet."""
    secret = (
        os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
        or os.getenv("TELEGRAM_TOKEN", "").strip()
        or "uni-oldenburg-studip-vault-salt-key-2026"
    )
    # HKDF-style domain-separated SHA-256 digest
    digest = hashlib.sha256(b"studip-vault-key-derivation-v1:" + secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_payload(data: Any) -> str:
    """Serialize data to JSON and encrypt with Fernet, prefixing with ENCRYPTED_MAGIC."""
    from cryptography.fernet import Fernet

    key = _derive_fernet_key()
    f = Fernet(key)
    raw_json = json.dumps(data, ensure_ascii=False).encode("utf-8")
    token = f.encrypt(raw_json).decode("utf-8")
    return f"{ENCRYPTED_MAGIC}{token}"


def decrypt_payload(raw_text: str) -> Optional[Any]:
    """Decrypt payload if encrypted, or parse legacy plain JSON if not."""
    raw_text = raw_text.strip()
    if not raw_text:
        return None

    if raw_text.startswith(ENCRYPTED_MAGIC):
        cipher_token = raw_text[len(ENCRYPTED_MAGIC):].strip()
        from cryptography.fernet import Fernet

        key = _derive_fernet_key()
        f = Fernet(key)
        try:
            plain_bytes = f.decrypt(cipher_token.encode("utf-8"))
            return json.loads(plain_bytes.decode("utf-8"))
        except Exception as exc:
            logger.error("Failed to decrypt vault payload: %s", exc)
            return None

    # Check if raw text is raw Fernet token without prefix (Fernet tokens start with gAAAAA)
    if raw_text.startswith("gAAAAA"):
        from cryptography.fernet import Fernet

        key = _derive_fernet_key()
        f = Fernet(key)
        try:
            plain_bytes = f.decrypt(raw_text.encode("utf-8"))
            return json.loads(plain_bytes.decode("utf-8"))
        except Exception:
            pass

    # Fallback: Plaintext legacy JSON (migration scenario)
    try:
        return json.loads(raw_text)
    except Exception as exc:
        logger.warning("Failed to parse plaintext payload: %s", exc)
        return None


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

    data = decrypt_payload(raw_text)
    if data is None:
        return None

    # If it was unencrypted or has looser permissions than 0600, migrate it immediately
    is_plain = not raw_text.startswith(ENCRYPTED_MAGIC) and not raw_text.startswith("gAAAAA")
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
