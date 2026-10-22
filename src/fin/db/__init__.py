"""SQLite access. One file, WAL mode so the dashboard and CLI can work side by side."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = Path(__file__).with_name("schema.sql")

# Incremental migrations for DBs created by older versions. schema.sql always describes the
# latest shape, so a fresh DB runs it and jumps straight to SCHEMA_VERSION.
MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE rules ADD COLUMN account_kind TEXT;",
    3: """
        ALTER TABLE lots ADD COLUMN trade_time TEXT;
        ALTER TABLE lots ADD COLUMN total_eur REAL;
        ALTER TABLE lots ADD COLUMN kind TEXT NOT NULL DEFAULT 'trade';
        ALTER TABLE lots ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE IF NOT EXISTS instruments (
            isin TEXT PRIMARY KEY, name TEXT NOT NULL, ticker TEXT, currency TEXT NOT NULL DEFAULT 'EUR',
            asset_class TEXT NOT NULL DEFAULT 'equity');
    """,
}
SCHEMA_VERSION = max(MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(path, detect_types=0, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
    init(con)
    return con


def init(con: sqlite3.Connection) -> None:
    (version,) = con.execute("PRAGMA user_version").fetchone()
    if version == 0:
        con.executescript(_SCHEMA.read_text(encoding="utf-8"))
    else:
        for v in sorted(k for k in MIGRATIONS if k > version):
            con.executescript(MIGRATIONS[v])
    if version < SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()


@contextmanager
def tx(con: sqlite3.Connection):
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise


def store_raw(
    con: sqlite3.Connection,
    kind: str,
    payload: Any,
    *,
    sync_run_id: int | None = None,
    account_id: int | None = None,
    name: str | None = None,
) -> int:
    """Store a raw payload (JSON-able object or bytes), gzip-compressed. Returns its id."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    digest = hashlib.sha256(data).hexdigest()
    cur = con.execute(
        "INSERT INTO raw_payloads (sync_run_id, account_id, kind, name, sha256, body) VALUES (?,?,?,?,?,?)",
        (sync_run_id, account_id, kind, name, digest, gzip.compress(data)),
    )
    return cur.lastrowid


def load_raw(con: sqlite3.Connection, raw_id: int) -> bytes:
    (body,) = con.execute("SELECT body FROM raw_payloads WHERE id = ?", (raw_id,)).fetchone()
    return gzip.decompress(body)


def get_setting(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
