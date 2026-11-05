"""SQLite document store for the bot's persistent state.

Every cache and settings document (general_cache.json, tasks_cache.json, ...)
used to be its own JSON file, rewritten as a whole on each save. They now live
as rows of one SQLite database (one row per document, the JSON text as value),
which gives:

  * transactions: a save is all or nothing, even on a full disk or a crash
  * one file to back up instead of a dozen
  * a single place to add real tables later (e.g. for multiple users)

The API mirrors the file operations it replaces, so call sites stay simple:
exists(path), load(path), save(path, data) and delete(path), where path is
the old file name and serves as the document key.

Migration is automatic and lazy: the first load of a document that is not in
the database yet reads the old JSON file and imports it. The file itself is
left in place as a backup; it is never read again once imported (delete()
removes both, so a cleared setting cannot come back from the old file).

Part of the unibot package (split out of the former single-file studip_bot.py).
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from unibot.config import DB_FILE, PROJECT_ROOT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id       INTEGER PRIMARY KEY,
    role              TEXT NOT NULL DEFAULT 'user',
    username          TEXT,
    full_name         TEXT,
    studip_username   TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    last_active_at    REAL NOT NULL,
    interaction_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL,
    label       TEXT,
    expires_at  REAL NOT NULL,
    is_used     INTEGER NOT NULL DEFAULT 0,
    used_by     INTEGER,
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id         INTEGER NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_blob  TEXT NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (user_id, credential_type)
);

CREATE TABLE IF NOT EXISTS user_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    action      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_activity_tg_created ON user_activity(telegram_id, created_at);
"""

_lock = threading.RLock()
_conn = None
_conn_path = None


def _db_path() -> str:
    return DB_FILE if os.path.isabs(DB_FILE) else os.path.join(PROJECT_ROOT, DB_FILE)


def _connection() -> sqlite3.Connection:
    global _conn, _conn_path
    path = _db_path()
    if _conn is None or _conn_path != path:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=FULL")
        _conn.executescript(_SCHEMA)
        _conn_path = path
    return _conn


def _key(path: str) -> str:
    """Document key: the file name relative to the project root."""
    absolute = os.path.abspath(path)
    rel = os.path.relpath(absolute, PROJECT_ROOT)
    return absolute if rel.startswith("..") else rel


def _row(key: str):
    return _connection().execute("SELECT value FROM documents WHERE key = ?", (key,)).fetchone()


def _write(key: str, text: str) -> None:
    _connection().execute(
        "INSERT INTO documents (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, text, time.time()),
    )


def exists(path: str) -> bool:
    """Whether the document exists (in the database or as a legacy file)."""
    with _lock:
        return _row(_key(path)) is not None or os.path.exists(path)


def load(path: str):
    """Parsed document. Raises FileNotFoundError when it does not exist and
    ValueError when a legacy file holds invalid JSON, like json.load(open())."""
    key = _key(path)
    with _lock:
        row = _row(key)
        if row is not None:
            return json.loads(row[0])
        # Not migrated yet: import the old JSON file once.
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        data = json.loads(text)
        _write(key, json.dumps(data, ensure_ascii=False))
        logging.info("Imported %s into %s", key, os.path.basename(_db_path()))
        return data


def save(path: str, data, indent=None) -> None:
    """Store the document. Raises TypeError for data JSON can't encode, and
    then leaves the stored version unchanged. indent is accepted for
    compatibility with atomic_write_json and ignored."""
    text = json.dumps(data, ensure_ascii=False)
    with _lock:
        _write(_key(path), text)


def delete(path: str) -> None:
    """Remove the document and its legacy file, if any."""
    with _lock:
        _connection().execute("DELETE FROM documents WHERE key = ?", (_key(path),))
        if os.path.exists(path):
            os.remove(path)


def keys() -> list[str]:
    with _lock:
        return [r[0] for r in _connection().execute("SELECT key FROM documents ORDER BY key")]


def close() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn_path = None


# ── User & Credential Multi-Tenancy helpers ───────────────────────────────────

def get_user(telegram_id: int) -> Optional[dict]:
    with _lock:
        row = _connection().execute(
            "SELECT telegram_id, role, username, full_name, studip_username, is_active, created_at, last_active_at, interaction_count "
            "FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "telegram_id": row[0],
            "role": row[1],
            "username": row[2],
            "full_name": row[3],
            "studip_username": row[4],
            "is_active": bool(row[5]),
            "created_at": row[6],
            "last_active_at": row[7],
            "interaction_count": row[8],
        }


def get_all_users() -> list[dict]:
    with _lock:
        cursor = _connection().execute(
            "SELECT telegram_id, role, username, full_name, studip_username, is_active, created_at, last_active_at, interaction_count "
            "FROM users ORDER BY created_at ASC"
        )
        users = []
        for row in cursor.fetchall():
            users.append({
                "telegram_id": row[0],
                "role": row[1],
                "username": row[2],
                "full_name": row[3],
                "studip_username": row[4],
                "is_active": bool(row[5]),
                "created_at": row[6],
                "last_active_at": row[7],
                "interaction_count": row[8],
            })
        return users


def upsert_user(
    telegram_id: int,
    role: str = "user",
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    studip_username: Optional[str] = None,
    is_active: int = 1,
) -> dict:
    now = time.time()
    with _lock:
        existing = get_user(telegram_id)
        if existing:
            effective_role = role if role == "admin" else existing["role"]
            effective_uname = username if username is not None else existing.get("username")
            effective_fname = full_name if full_name is not None else existing.get("full_name")
            effective_studip = studip_username if studip_username is not None else existing.get("studip_username")
            _connection().execute(
                "UPDATE users SET role = ?, username = ?, full_name = ?, studip_username = ?, is_active = ?, last_active_at = ? "
                "WHERE telegram_id = ?",
                (effective_role, effective_uname, effective_fname, effective_studip, is_active, now, telegram_id),
            )
        else:
            _connection().execute(
                "INSERT INTO users (telegram_id, role, username, full_name, studip_username, is_active, created_at, last_active_at, interaction_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (telegram_id, role, username, full_name, studip_username, is_active, now, now, 0),
            )
        return get_user(telegram_id)


def update_user_activity(telegram_id: int, action: Optional[str] = None) -> None:
    now = time.time()
    with _lock:
        _connection().execute(
            "UPDATE users SET last_active_at = ?, interaction_count = interaction_count + 1 WHERE telegram_id = ?",
            (now, telegram_id),
        )
        if action:
            _connection().execute(
                "INSERT INTO user_activity (telegram_id, action, created_at) VALUES (?, ?, ?)",
                (telegram_id, action, now),
            )


def set_user_active(telegram_id: int, is_active: bool) -> None:
    with _lock:
        _connection().execute(
            "UPDATE users SET is_active = ? WHERE telegram_id = ?",
            (1 if is_active else 0, telegram_id),
        )


def is_user_admin_db(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return bool(user and user.get("role") == "admin" and user.get("is_active"))


def is_user_allowed_db(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return bool(user and user.get("is_active"))


# ── Invite Code helpers ───────────────────────────────────────────────────────

def create_invite_token(created_by: int, label: Optional[str] = None, expires_days: float = 7.0) -> str:
    import secrets
    raw_code = secrets.token_hex(3).upper()
    code = f"UNI-{raw_code[:3]}-{raw_code[3:]}"
    expires_at = time.time() + (expires_days * 86400)
    with _lock:
        _connection().execute(
            "INSERT INTO invite_tokens (code, created_by, label, expires_at, is_used) VALUES (?, ?, ?, ?, 0)",
            (code, created_by, label, expires_at),
        )
    return code


def redeem_invite_token(code: str, telegram_id: int, full_name: Optional[str] = None, username: Optional[str] = None) -> tuple[bool, str]:
    clean_code = code.strip().upper()
    now = time.time()
    with _lock:
        row = _connection().execute(
            "SELECT code, created_by, label, expires_at, is_used FROM invite_tokens WHERE code = ?",
            (clean_code,),
        ).fetchone()
        if not row:
            return False, "Geçersiz davet kodu."
        if row[4]:  # is_used
            return False, "Bu davet kodu daha önce kullanılmış."
        if now > row[3]:  # expires_at
            return False, "Bu davet kodunun geçerlilik süresi dolmuş."

        _connection().execute(
            "UPDATE invite_tokens SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?",
            (telegram_id, now, clean_code),
        )
        upsert_user(
            telegram_id=telegram_id,
            role="user",
            username=username,
            full_name=full_name,
            is_active=1,
        )
        return True, "Davet kodu başarıyla onaylandı. Hoş geldiniz!"


def list_invite_tokens(active_only: bool = False) -> list[dict]:
    with _lock:
        query = "SELECT code, created_by, label, expires_at, is_used, used_by, used_at FROM invite_tokens"
        if active_only:
            query += " WHERE is_used = 0 AND expires_at > ?"
            cursor = _connection().execute(query, (time.time(),))
        else:
            query += " ORDER BY expires_at DESC"
            cursor = _connection().execute(query)
        res = []
        for r in cursor.fetchall():
            res.append({
                "code": r[0],
                "created_by": r[1],
                "label": r[2],
                "expires_at": r[3],
                "is_used": bool(r[4]),
                "used_by": r[5],
                "used_at": r[6],
            })
        return res


# ── User Credentials (Encrypted Vault in SQLite) ──────────────────────────────

def save_user_credential(user_id: int, credential_type: str, data: Any) -> None:
    from studip_vault import encrypt_payload
    encrypted_blob = encrypt_payload(data)
    now = time.time()
    with _lock:
        _connection().execute(
            "INSERT INTO user_credentials (user_id, credential_type, encrypted_blob, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, credential_type) DO UPDATE SET "
            "encrypted_blob = excluded.encrypted_blob, updated_at = excluded.updated_at",
            (user_id, credential_type, encrypted_blob, now),
        )


def load_user_credential(user_id: int, credential_type: str) -> Optional[Any]:
    from studip_vault import decrypt_payload
    with _lock:
        row = _connection().execute(
            "SELECT encrypted_blob FROM user_credentials WHERE user_id = ? AND credential_type = ?",
            (user_id, credential_type),
        ).fetchone()
        if not row:
            return None
        return decrypt_payload(row[0])


def delete_user_credentials(user_id: int) -> None:
    with _lock:
        _connection().execute("DELETE FROM user_credentials WHERE user_id = ?", (user_id,))


# ── Analytics helpers ─────────────────────────────────────────────────────────

def get_user_analytics() -> dict:
    now = time.time()
    day_ago = now - 86400
    week_ago = now - (7 * 86400)
    with _lock:
        total = _connection().execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = _connection().execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
        dau = _connection().execute("SELECT COUNT(*) FROM users WHERE last_active_at >= ?", (day_ago,)).fetchone()[0]
        wau = _connection().execute("SELECT COUNT(*) FROM users WHERE last_active_at >= ?", (week_ago,)).fetchone()[0]
        pending_invites = _connection().execute(
            "SELECT COUNT(*) FROM invite_tokens WHERE is_used = 0 AND expires_at > ?", (now,)
        ).fetchone()[0]

        top_rows = _connection().execute(
            "SELECT telegram_id, username, full_name, interaction_count, last_active_at "
            "FROM users ORDER BY interaction_count DESC LIMIT 5"
        ).fetchall()

        top_users = [
            {
                "telegram_id": r[0],
                "username": r[1] or "",
                "full_name": r[2] or "",
                "interactions": r[3],
                "last_active_at": r[4],
            }
            for r in top_rows
        ]

        recent_activity_rows = _connection().execute(
            "SELECT a.telegram_id, u.username, u.full_name, a.action, a.created_at "
            "FROM user_activity a LEFT JOIN users u ON a.telegram_id = u.telegram_id "
            "ORDER BY a.created_at DESC LIMIT 6"
        ).fetchall()
        recent_activities = [
            {
                "telegram_id": r[0],
                "username": r[1] or "",
                "full_name": r[2] or "",
                "action": r[3],
                "created_at": r[4],
            }
            for r in recent_activity_rows
        ]

        return {
            "total_users": total,
            "active_users": active,
            "dau": dau,
            "wau": wau,
            "pending_invites": pending_invites,
            "top_users": top_users,
            "recent_activities": recent_activities,
        }


def bootstrap_admins(admin_ids: set[int]) -> None:
    for aid in admin_ids:
        upsert_user(telegram_id=aid, role="admin", is_active=1)

