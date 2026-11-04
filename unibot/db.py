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

from unibot.config import DB_FILE, PROJECT_ROOT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
)
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
        _conn.execute(_SCHEMA)
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
