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

import gzip
import json
import logging
import os
import shutil
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

-- every use of an invite code (a code may admit several students; used_by
-- in invite_tokens only ever held the latest one)
CREATE TABLE IF NOT EXISTS invite_redemptions (
    code        TEXT NOT NULL,
    telegram_id INTEGER NOT NULL,
    used_at     REAL NOT NULL,
    PRIMARY KEY (code, telegram_id)
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

-- Course registry: one row per Stud.IP course, scanned once for everyone.
-- Holds no user data; who follows a course lives in user_courses.
CREATE TABLE IF NOT EXISTS global_courses (
    course_id       TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    semester        TEXT,
    last_scanned_at REAL,
    scanner_uid     INTEGER,
    file_hash       TEXT,
    forum_hash      TEXT
);

-- Who follows which course, with that user's notification preferences.
CREATE TABLE IF NOT EXISTS user_courses (
    user_id              INTEGER NOT NULL,
    course_id            TEXT NOT NULL,
    enrolled_at          REAL NOT NULL,
    is_muted             INTEGER NOT NULL DEFAULT 0,
    notify_files         INTEGER NOT NULL DEFAULT 1,
    notify_announcements INTEGER NOT NULL DEFAULT 1,
    notify_forum         INTEGER NOT NULL DEFAULT 1,
    format               TEXT NOT NULL DEFAULT 'silent',
    PRIMARY KEY (user_id, course_id)
);
CREATE INDEX IF NOT EXISTS idx_user_courses_course ON user_courses(course_id);

-- Private student data, one encrypted row per student and kind; never part
-- of the shared document store or the course registry.
CREATE TABLE IF NOT EXISTS user_private_data (
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    encrypted_blob TEXT NOT NULL,
    updated_at     REAL NOT NULL,
    PRIMARY KEY (user_id, kind)
);
"""

DB_BUSY_TIMEOUT = 30.0

_lock = threading.RLock()
_conn = None
_conn_path = None


def _db_path() -> str:
    return DB_FILE if os.path.isabs(DB_FILE) else os.path.join(PROJECT_ROOT, DB_FILE)


def _connection() -> sqlite3.Connection:
    global _conn, _conn_path
    # opening (or switching) the connection is guarded too: two threads must
    # never both open one, or close the other's (_lock is reentrant)
    with _lock:
        path = _db_path()
        if _conn is None or _conn_path != path:
            if _conn is not None:
                _conn.close()
            # 30 s instead of the 5 s default, so a backup or a slow disk
            # makes writers wait rather than fail with "database is locked"
            _conn = sqlite3.connect(path, timeout=DB_BUSY_TIMEOUT, check_same_thread=False,
                                    isolation_level=None)
            _conn.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT * 1000)}")
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=FULL")
            _conn.executescript(_SCHEMA)
            _migrate_columns(_conn)
            _conn_path = path
        return _conn


# Columns added after a table first shipped: (table, column, definition).
_ADDED_COLUMNS = (
    ("users", "lifecycle", "TEXT NOT NULL DEFAULT 'active'"),
    ("users", "lifecycle_reason", "TEXT"),
    ("users", "lifecycle_changed_at", "REAL"),
    ("invite_tokens", "max_uses", "INTEGER NOT NULL DEFAULT 1"),
    ("invite_tokens", "use_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # the uses recorded before invite_redemptions existed (idempotent)
    conn.execute(
        "INSERT OR IGNORE INTO invite_redemptions (code, telegram_id, used_at) "
        "SELECT code, used_by, COALESCE(used_at, 0) FROM invite_tokens WHERE used_by IS NOT NULL")


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
        # the database is the only copy from now on; the renamed file stays as
        # a backup but can never be read (or edited) by mistake again
        try:
            os.replace(path, path + ".imported")
        except OSError:
            logging.debug("Could not rename imported %s", path, exc_info=True)
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


def retire_imported_files() -> int:
    """Rename every JSON file whose document already lives in the database to
    <file>.imported, so stale copies can't be mistaken for the live data.
    Returns how many files were renamed."""
    renamed = 0
    with _lock:
        for (key,) in _connection().execute("SELECT key FROM documents").fetchall():
            path = key if os.path.isabs(key) else os.path.join(PROJECT_ROOT, key)
            if not os.path.isfile(path):
                continue
            try:
                os.replace(path, path + ".imported")
                renamed += 1
            except OSError:
                logging.debug("Could not rename %s", path, exc_info=True)
    if renamed:
        logging.info("Renamed %s JSON file(s) already stored in %s to *.imported",
                     renamed, os.path.basename(_db_path()))
    return renamed


def close() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _conn_path = None



BACKUP_DIR = "backups"
BACKUP_KEEP = 7


def backup(dest_dir: Optional[str] = None, keep: int = BACKUP_KEEP, stamp: Optional[str] = None) -> str:
    """Write a consistent, gzipped copy of the database to
    <dest_dir>/bot_data_<stamp>.sqlite3.gz and keep only the newest `keep`.

    Uses SQLite's online backup, so it is safe while the bot keeps writing
    (blocking work: call it from a thread).
    Logins in the copy stay encrypted; restoring also needs .vault_key.
    Returns the path of the new backup."""
    dest_dir = dest_dir or os.path.join(PROJECT_ROOT, BACKUP_DIR)
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    stamp = stamp or time.strftime("%Y-%m-%d")
    final = os.path.join(dest_dir, f"bot_data_{stamp}.sqlite3.gz")
    raw = final[:-3] + ".tmp"
    try:
        with _lock:
            _connection()  # make sure the file and schema exist
        # its own read connection: with WAL the bot keeps reading and writing
        source = sqlite3.connect(_db_path())
        target = sqlite3.connect(raw)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with open(raw, "rb") as src, gzip.open(final + ".tmp", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        os.chmod(final + ".tmp", 0o600)
        os.replace(final + ".tmp", final)
    finally:
        for leftover in (raw, final + ".tmp"):
            if os.path.exists(leftover):
                os.remove(leftover)
    backups = sorted(f for f in os.listdir(dest_dir) if f.startswith("bot_data_") and f.endswith(".sqlite3.gz"))
    for old in backups[:-keep] if keep > 0 else []:
        os.remove(os.path.join(dest_dir, old))
    return final


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
    is_active: Optional[int] = None,
) -> dict:
    """Create or update a user. `is_active` None keeps an existing user's
    state (a blocked user stays blocked) and makes a new user active."""
    now = time.time()
    with _lock:
        existing = get_user(telegram_id)
        if existing:
            effective_role = role if role == "admin" else existing["role"]
            effective_uname = username if username is not None else existing.get("username")
            effective_fname = full_name if full_name is not None else existing.get("full_name")
            effective_studip = studip_username if studip_username is not None else existing.get("studip_username")
            effective_active = is_active if is_active is not None else (1 if existing.get("is_active") else 0)
            _connection().execute(
                "UPDATE users SET role = ?, username = ?, full_name = ?, studip_username = ?, is_active = ?, last_active_at = ? "
                "WHERE telegram_id = ?",
                (effective_role, effective_uname, effective_fname, effective_studip, effective_active, now, telegram_id),
            )
        else:
            _connection().execute(
                "INSERT INTO users (telegram_id, role, username, full_name, studip_username, is_active, created_at, last_active_at, interaction_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (telegram_id, role, username, full_name, studip_username, 1 if is_active is None else is_active, now, now, 0),
            )
        user = get_user(telegram_id)
        assert user is not None  # just written in this transaction
        return user


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


ACTIVITY_KEEP_DAYS = 30  # the admin panel only shows the latest few actions


def prune_activity(keep_days: float = ACTIVITY_KEEP_DAYS, now: Optional[float] = None) -> int:
    """Drop activity rows older than keep_days (they hold what students
    typed); returns how many went."""
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    with _lock:
        cur = _connection().execute("DELETE FROM user_activity WHERE created_at < ?", (cutoff,))
        return cur.rowcount or 0


def forget_user_identity(telegram_id: int) -> None:
    """A student's own data deletion: their activity log and the Telegram
    name and username kept with their account row (the row itself stays, it
    holds their access and role)."""
    with _lock:
        _connection().execute("DELETE FROM user_activity WHERE telegram_id = ?", (telegram_id,))
        _connection().execute("UPDATE users SET username = NULL, full_name = NULL WHERE telegram_id = ?",
                              (telegram_id,))


def set_user_active(telegram_id: int, is_active: bool) -> None:
    with _lock:
        _connection().execute(
            "UPDATE users SET is_active = ? WHERE telegram_id = ?",
            (1 if is_active else 0, telegram_id),
        )


def set_user_role(telegram_id: int, role: str) -> None:
    with _lock:
        _connection().execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?",
            (role, telegram_id),
        )


def update_user_studip(telegram_id: int, studip_username: str) -> None:
    with _lock:
        _connection().execute(
            "UPDATE users SET studip_username = ? WHERE telegram_id = ?",
            (studip_username, telegram_id),
        )


def is_user_admin_db(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return bool(user and user.get("role") == "admin" and user.get("is_active"))


def is_user_allowed_db(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return bool(user and user.get("is_active"))


# ── Invite Code helpers ───────────────────────────────────────────────────────

def normalize_invite_code(code: str) -> str:
    """"uniabc123", "UNI-ABC-123", " uni abc 123 " all mean UNI-ABC-123
    (the deep link carries the code without dashes)."""
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if raw.startswith("UNI") and len(raw) == 9:
        return f"UNI-{raw[3:6]}-{raw[6:]}"
    return (code or "").strip().upper()


def create_invite_token(created_by: int, label: Optional[str] = None, expires_days: float = 7.0,
                        max_uses: int = 1) -> str:
    """A new invite code, valid for `max_uses` students until it expires."""
    import secrets
    if max_uses < 1:
        raise ValueError("an invite needs at least one use")
    raw_code = secrets.token_hex(3).upper()
    code = f"UNI-{raw_code[:3]}-{raw_code[3:]}"
    expires_at = time.time() + (expires_days * 86400)
    with _lock:
        _connection().execute(
            "INSERT INTO invite_tokens (code, created_by, label, expires_at, is_used, max_uses, use_count) "
            "VALUES (?, ?, ?, ?, 0, ?, 0)",
            (code, created_by, label, expires_at, int(max_uses)),
        )
    return code


def redeem_invite_token(code: str, telegram_id: int, full_name: Optional[str] = None, username: Optional[str] = None) -> tuple[bool, str]:
    clean_code = normalize_invite_code(code)
    now = time.time()
    with _lock:
        row = _connection().execute(
            "SELECT code, created_by, label, expires_at, is_used, max_uses, use_count FROM invite_tokens WHERE code = ?",
            (clean_code,),
        ).fetchone()
        if not row:
            return False, "Invalid invite code."
        if row[4] or row[6] >= row[5]:  # used up or revoked
            return False, "This invite code has already been used."
        if now > row[3]:  # expires_at
            return False, "This invite code has expired."
        existing = get_user(telegram_id)
        if existing and not existing.get("is_active"):
            # an invite code never lifts a block set by an administrator
            return False, "This account was blocked by an administrator."
        if existing:
            return True, "You already have access."  # no quota used

        uses = row[6] + 1
        _connection().execute(
            "UPDATE invite_tokens SET use_count = ?, is_used = ?, used_by = ?, used_at = ? WHERE code = ?",
            (uses, 1 if uses >= row[5] else 0, telegram_id, now, clean_code),
        )
        _connection().execute(
            "INSERT OR IGNORE INTO invite_redemptions (code, telegram_id, used_at) VALUES (?, ?, ?)",
            (clean_code, telegram_id, now),
        )
        upsert_user(
            telegram_id=telegram_id,
            role="user",
            username=username,
            full_name=full_name,
        )
        set_lifecycle(telegram_id, "active", "invite redeemed")
        return True, "Invite code verified successfully. Welcome!"


def revoke_invite_token(code: str) -> bool:
    """Make a code unusable at once; False when there is no such code."""
    with _lock:
        cur = _connection().execute(
            "UPDATE invite_tokens SET is_used = 1, expires_at = MIN(expires_at, ?) WHERE code = ?",
            (time.time(), normalize_invite_code(code)),
        )
    return cur.rowcount > 0


def list_invite_tokens(active_only: bool = False) -> list[dict]:
    with _lock:
        query = ("SELECT code, created_by, label, expires_at, is_used, used_by, used_at, max_uses, use_count "
                 "FROM invite_tokens")
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
                "max_uses": r[7],
                "use_count": r[8],
                "redeemed_by": invite_redemptions(r[0]),
            })
        return res


def invite_redemptions(code: str) -> list[int]:
    """The Telegram ids that used the code, oldest first."""
    with _lock:
        rows = _connection().execute(
            "SELECT telegram_id FROM invite_redemptions WHERE code = ? ORDER BY used_at, telegram_id",
            (normalize_invite_code(code),),
        ).fetchall()
    return [r[0] for r in rows]


# ── User Credentials (Encrypted Vault in SQLite) ──────────────────────────────

def _require_uid(user_id) -> int:
    """Personal data is always read and written for one named student."""
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("personal data needs an explicit user id")
    return user_id


def _save_user_credential(user_id: int, credential_type: str, data: Any) -> None:
    from studip_vault import encrypt_user_payload
    uid = _require_uid(user_id)
    encrypted_blob = encrypt_user_payload(uid, data)
    now = time.time()
    with _lock:
        _connection().execute(
            "INSERT INTO user_credentials (user_id, credential_type, encrypted_blob, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, credential_type) DO UPDATE SET "
            "encrypted_blob = excluded.encrypted_blob, updated_at = excluded.updated_at",
            (uid, credential_type, encrypted_blob, now),
        )


def _load_user_credential(user_id: int, credential_type: str) -> Optional[Any]:
    """The student's decrypted credential; a record still under the old shared
    key is re-encrypted with the student's own key on this read."""
    from studip_vault import decrypt_user_payload
    uid = _require_uid(user_id)
    with _lock:
        row = _connection().execute(
            "SELECT encrypted_blob FROM user_credentials WHERE user_id = ? AND credential_type = ?",
            (uid, credential_type),
        ).fetchone()
        if not row:
            return None
        legacy: list = []
        data = decrypt_user_payload(uid, row[0], legacy)
        if legacy and data is not None:
            _save_user_credential(uid, credential_type, data)
        return data


# ── Private student data (grades, exam registrations, fees) ──────────────────
# Kept apart from the shared document store: one row per student and kind,
# encrypted with that student's own key, and only reachable with their id.

def _save_private(user_id: int, kind: str, data: Any) -> None:
    from studip_vault import encrypt_user_payload
    uid = _require_uid(user_id)
    with _lock:
        _connection().execute(
            "INSERT INTO user_private_data (user_id, kind, encrypted_blob, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, kind) DO UPDATE SET "
            "encrypted_blob = excluded.encrypted_blob, updated_at = excluded.updated_at",
            (uid, kind, encrypt_user_payload(uid, data), time.time()),
        )


def _load_private(user_id: int, kind: str) -> Optional[Any]:
    from studip_vault import decrypt_user_payload
    uid = _require_uid(user_id)
    with _lock:
        row = _connection().execute(
            "SELECT encrypted_blob FROM user_private_data WHERE user_id = ? AND kind = ?", (uid, kind),
        ).fetchone()
    return decrypt_user_payload(uid, row[0]) if row else None


def _has_private(user_id: int, kind: str) -> bool:
    uid = _require_uid(user_id)
    with _lock:
        return _connection().execute(
            "SELECT 1 FROM user_private_data WHERE user_id = ? AND kind = ?", (uid, kind),
        ).fetchone() is not None


def _delete_private(user_id: int, kind: Optional[str] = None, keep: tuple = ()) -> None:
    uid = _require_uid(user_id)
    with _lock:
        if kind is None:
            marks = ",".join("?" * len(keep))
            _connection().execute(
                "DELETE FROM user_private_data WHERE user_id = ?" + (f" AND kind NOT IN ({marks})" if keep else ""),
                (uid, *keep))
        else:
            _connection().execute("DELETE FROM user_private_data WHERE user_id = ? AND kind = ?", (uid, kind))


def _delete_user_credential(user_id: int, credential_type: str) -> None:
    uid = _require_uid(user_id)
    with _lock:
        _connection().execute("DELETE FROM user_credentials WHERE user_id = ? AND credential_type = ?",
                              (uid, credential_type))


def _delete_user_credentials(user_id: int) -> None:
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



# ── Course registry (global_courses / user_courses) ───────────────────────────

COURSE_PREF_COLUMNS = ("is_muted", "notify_files", "notify_announcements", "notify_forum", "format")
COURSE_FORMATS = ("full", "title", "silent")


def upsert_global_course(course_id: str, title: str, semester: Optional[str] = None) -> None:
    """Add a course to the registry or refresh its title (and semester when known)."""
    with _lock:
        _connection().execute(
            "INSERT INTO global_courses (course_id, title, semester) VALUES (?, ?, ?) "
            "ON CONFLICT(course_id) DO UPDATE SET title = excluded.title, "
            "semester = COALESCE(excluded.semester, global_courses.semester)",
            (course_id, title, semester),
        )


def get_global_course(course_id: str) -> Optional[dict]:
    with _lock:
        row = _connection().execute(
            "SELECT course_id, title, semester, last_scanned_at, scanner_uid, file_hash, forum_hash "
            "FROM global_courses WHERE course_id = ?",
            (course_id,),
        ).fetchone()
    if not row:
        return None
    keys = ("course_id", "title", "semester", "last_scanned_at", "scanner_uid", "file_hash", "forum_hash")
    return dict(zip(keys, row))


def mark_course_scanned(course_id: str, scanner_uid: Optional[int], *, file_hash: Optional[str] = None,
                        forum_hash: Optional[str] = None, ts: Optional[float] = None) -> None:
    """Record a scan: when, by whose session, and the content hashes it saw."""
    with _lock:
        _connection().execute(
            "UPDATE global_courses SET last_scanned_at = ?, scanner_uid = ?, "
            "file_hash = COALESCE(?, file_hash), forum_hash = COALESCE(?, forum_hash) WHERE course_id = ?",
            (ts if ts is not None else time.time(), scanner_uid, file_hash, forum_hash, course_id),
        )


def _sync_user_courses(user_id: int, course_ids, now: Optional[float] = None) -> None:
    """Make the user's followed courses exactly `course_ids`: new ones get the
    default preferences, existing ones keep theirs, dropped ones go."""
    wanted = set(course_ids)
    with _lock:
        conn = _connection()
        have = {r[0] for r in conn.execute("SELECT course_id FROM user_courses WHERE user_id = ?", (user_id,))}
        stamp = now if now is not None else time.time()
        for cid in wanted - have:
            conn.execute("INSERT OR IGNORE INTO user_courses (user_id, course_id, enrolled_at) VALUES (?, ?, ?)",
                         (user_id, cid, stamp))
        for cid in have - wanted:
            conn.execute("DELETE FROM user_courses WHERE user_id = ? AND course_id = ?", (user_id, cid))


def course_followers(course_id: str) -> list[int]:
    with _lock:
        return [r[0] for r in _connection().execute(
            "SELECT user_id FROM user_courses WHERE course_id = ? ORDER BY enrolled_at, user_id", (course_id,))]


def _user_course_ids(user_id: int) -> list[str]:
    with _lock:
        return [r[0] for r in _connection().execute(
            "SELECT course_id FROM user_courses WHERE user_id = ? ORDER BY course_id", (user_id,))]


def _get_course_prefs(user_id: int, course_id: str) -> Optional[dict]:
    """The user's preferences for a followed course, or None when not followed."""
    with _lock:
        row = _connection().execute(
            f"SELECT {', '.join(COURSE_PREF_COLUMNS)} FROM user_courses WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        ).fetchone()
    if not row:
        return None
    prefs = dict(zip(COURSE_PREF_COLUMNS, row))
    for k in ("is_muted", "notify_files", "notify_announcements", "notify_forum"):
        prefs[k] = bool(prefs[k])
    return prefs


def _set_course_pref(user_id: int, course_id: str, column: str, value) -> bool:
    """Change one preference of a followed course; False when not followed."""
    if column not in COURSE_PREF_COLUMNS:
        raise ValueError(f"unknown course preference {column!r}")
    if column == "format":
        if value not in COURSE_FORMATS:
            raise ValueError(f"unknown format {value!r}")
    else:
        value = 1 if value else 0
    with _lock:
        cur = _connection().execute(
            f"UPDATE user_courses SET {column} = ? WHERE user_id = ? AND course_id = ?",
            (value, user_id, course_id),
        )
    return cur.rowcount > 0


def registry_counts() -> dict:
    """{"courses": unique followed courses, "pairs": user-course pairs}."""
    with _lock:
        conn = _connection()
        courses = conn.execute("SELECT COUNT(DISTINCT course_id) FROM user_courses").fetchone()[0]
        pairs = conn.execute("SELECT COUNT(*) FROM user_courses").fetchone()[0]
    return {"courses": courses, "pairs": pairs}


def privacy_counts() -> dict:
    """Record counts only, never content: credentials under a student's own
    key, ones still under the old shared key, and private data rows."""
    with _lock:
        conn = _connection()
        own, legacy = conn.execute(
            "SELECT COALESCE(SUM(encrypted_blob LIKE 'VAULT:u1:%'), 0), "
            "COALESCE(SUM(encrypted_blob NOT LIKE 'VAULT:u1:%'), 0) FROM user_credentials").fetchone()
        private, students = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_private_data").fetchone()
    return {"credentials_own_key": own, "credentials_shared_key": legacy,
            "private_rows": private, "private_students": students}


# ── UserScope: the only way to a student's own records ───────────────────────
# Every per-student read and write goes through a scope opened for one
# student id. The scope adds the user_id filter and the student's key to each
# query itself, so a caller cannot forget either, and nothing outside this
# module reads the per-student tables directly (tests/test_data_boundary.py
# enforces that).

LEGACY_TASKS_DOC = "tasks_cache.json"
TASKS_KIND = "tasks"


class UserScope:
    """One student's isolated view of the database."""

    __slots__ = ("user_id",)

    def __init__(self, user_id: int):
        self.user_id = _require_uid(user_id)

    def __repr__(self) -> str:
        return f"UserScope({self.user_id})"

    # credentials: cookies, OAuth tokens, StuMS state
    def credential(self, kind: str) -> Optional[Any]:
        return _load_user_credential(self.user_id, kind)

    def save_credential(self, kind: str, data: Any) -> None:
        _save_user_credential(self.user_id, kind, data)

    def delete_credential(self, kind: str) -> None:
        _delete_user_credential(self.user_id, kind)

    def delete_credentials(self) -> None:
        _delete_user_credentials(self.user_id)

    # private records: exam cache (grades, registrations, fees), tasks, ...
    def private(self, kind: str) -> Optional[Any]:
        return _load_private(self.user_id, kind)

    def save_private(self, kind: str, data: Any) -> None:
        _save_private(self.user_id, kind, data)

    def has_private(self, kind: str) -> bool:
        return _has_private(self.user_id, kind)

    def delete_private(self, kind: Optional[str] = None, keep: tuple = ()) -> None:
        """Delete one kind, or every kind except those in `keep`."""
        _delete_private(self.user_id, kind, keep)

    def grades(self) -> dict:
        """The grades seen so far, {exam number: fingerprint}."""
        cache = self.private("exam_cache") or {}
        return dict(cache.get("known_grades") or {})

    # tasks
    def tasks(self) -> list:
        _migrate_shared_tasks()
        return list(self.private(TASKS_KIND) or [])

    def save_tasks(self, tasks: list) -> None:
        for task in tasks:
            owner = task.get("user_id")
            if owner is not None and owner != self.user_id:
                raise ValueError("a task of another student cannot be saved in this scope")
        self.save_private(TASKS_KIND, [{**t, "user_id": self.user_id} for t in tasks])

    def add_task(self, task: dict) -> None:
        self.save_tasks(self.tasks() + [task])

    def remove_tasks(self, ids) -> list:
        """Drop the student's tasks with these ids; returns the removed ones."""
        ids = set(ids)
        tasks = self.tasks()
        removed = [t for t in tasks if t.get("id") in ids]
        if removed:
            self.save_tasks([t for t in tasks if t.get("id") not in ids])
        return removed

    # course registry rows of this student
    def course_ids(self) -> list[str]:
        return _user_course_ids(self.user_id)

    def course_prefs(self, course_id: str) -> Optional[dict]:
        return _get_course_prefs(self.user_id, course_id)

    def set_course_pref(self, course_id: str, column: str, value) -> bool:
        return _set_course_pref(self.user_id, course_id, column, value)

    def sync_courses(self, course_ids, now: Optional[float] = None) -> None:
        _sync_user_courses(self.user_id, course_ids, now)


def get_user_scope(user_id: int) -> UserScope:
    """Open one student's scope; raises ValueError without a valid user id."""
    return UserScope(user_id)


def private_owner_ids(kind: str) -> list[int]:
    """Ids of the students that have a private record of `kind`: ids only,
    for jobs that then open each student's own scope."""
    with _lock:
        return [r[0] for r in _connection().execute(
            "SELECT user_id FROM user_private_data WHERE kind = ? ORDER BY user_id", (kind,))]


def task_owner_ids() -> list[int]:
    """Ids of the students that have tasks (ids only, no task content)."""
    _migrate_shared_tasks()
    return private_owner_ids(TASKS_KIND)


def _migrate_shared_tasks() -> None:
    """Move the old shared task list into each owner's private record once
    (tasks without an owner belong to the primary admin, as before)."""
    with _lock:
        # the database row only: task files from the JSON era were imported long ago
        row = _row(_key(LEGACY_TASKS_DOC))
        if row is None:
            return
        try:
            shared = json.loads(row[0]) or []
        except Exception:
            logging.warning("The shared task list could not be read; left in place")
            return
        from unibot.config import PRIMARY_ADMIN_ID
        fallback = PRIMARY_ADMIN_ID
        by_owner: dict[int, list] = {}
        for task in shared if isinstance(shared, list) else []:
            owner = task.get("user_id") if isinstance(task.get("user_id"), int) else fallback
            if owner is not None:
                by_owner.setdefault(owner, []).append({**task, "user_id": owner})
        for owner, tasks in by_owner.items():
            have = list(_load_private(owner, TASKS_KIND) or [])
            known = {t.get("id") for t in have}
            _save_private(owner, TASKS_KIND, have + [t for t in tasks if t.get("id") not in known])
        _connection().execute("DELETE FROM documents WHERE key = ?", (_key(LEGACY_TASKS_DOC),))
        logging.info("Moved the shared task list into %s private task records", len(by_owner))


# ── user lifecycle: active, needs_reauth, inactive ───────────────────────────

LIFECYCLE_STATES = ("active", "needs_reauth", "inactive")


def get_lifecycle(telegram_id: int) -> dict:
    """{"state", "reason", "changed_at"}; "active" for unknown users."""
    with _lock:
        row = _connection().execute(
            "SELECT lifecycle, lifecycle_reason, lifecycle_changed_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if not row:
        return {"state": "active", "reason": None, "changed_at": None}
    return {"state": row[0] or "active", "reason": row[1], "changed_at": row[2]}


def set_lifecycle(telegram_id: int, lifecycle: str, reason: Optional[str] = None) -> bool:
    """Move a user to a lifecycle state; True when the state changed."""
    if lifecycle not in LIFECYCLE_STATES:
        raise ValueError(f"unknown lifecycle state {lifecycle!r}")
    with _lock:
        conn = _connection()
        row = conn.execute("SELECT lifecycle FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row is None:
            now = time.time()
            conn.execute(
                "INSERT INTO users (telegram_id, role, is_active, created_at, last_active_at, lifecycle, "
                "lifecycle_reason, lifecycle_changed_at) VALUES (?, 'user', 1, ?, ?, ?, ?, ?)",
                (telegram_id, now, now, lifecycle, reason, now),
            )
            return lifecycle != "active"
        if row[0] == lifecycle:
            return False
        conn.execute(
            "UPDATE users SET lifecycle = ?, lifecycle_reason = ?, lifecycle_changed_at = ? WHERE telegram_id = ?",
            (lifecycle, reason, time.time(), telegram_id),
        )
        return True


def users_in_lifecycle(lifecycle: str) -> list[int]:
    with _lock:
        return [r[0] for r in _connection().execute(
            "SELECT telegram_id FROM users WHERE lifecycle = ? ORDER BY telegram_id", (lifecycle,))]
