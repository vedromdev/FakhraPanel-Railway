"""SQLite data layer: users, sessions, and meta (WAL mode, parameterized SQL)."""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

_db_path: Path | None = None

_USER_COLUMNS = (
    "id",
    "username",
    "password",
    "protocol",
    "uuid",
    "credential",
    "ss_method",
    "enabled",
    "disable_reason",
    "expires_at",
    "quota_gb",
    "used_bytes",
    "last_raw_bytes",
    "note",
    "created_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK (protocol IN ('vless','vmess','trojan','ss')),
  uuid TEXT,
  credential TEXT,
  ss_method TEXT DEFAULT 'aes-256-gcm',
  enabled INTEGER DEFAULT 1,
  disable_reason TEXT,
  expires_at TEXT,
  quota_gb REAL,
  used_bytes INTEGER DEFAULT 0,
  last_raw_bytes INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def init_db(db_path: Path) -> None:
    """Point the module at db_path and create all tables (WAL mode)."""
    global _db_path
    _db_path = Path(db_path)
    conn = sqlite3.connect(_db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("init_db() must be called before using the database")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _check_columns(data: dict) -> None:
    unknown = set(data) - set(_USER_COLUMNS)
    if unknown:
        raise ValueError(f"unknown user columns: {sorted(unknown)}")


def _insert_user(conn: sqlite3.Connection, data: dict) -> None:
    _check_columns(data)
    columns = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    conn.execute(
        f"INSERT INTO users ({columns}) VALUES ({placeholders})",
        tuple(data.values()),
    )


def create_user(data: dict) -> dict:
    conn = _connect()
    try:
        with conn:
            _insert_user(conn, data)
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (data["id"],)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def create_users(rows: list[dict]) -> list[dict]:
    """Insert all rows in one transaction; any IntegrityError rolls back everything."""
    conn = _connect()
    try:
        with conn:  # commits on success, rolls back on IntegrityError
            for data in rows:
                _insert_user(conn, data)
        new_ids = {data["id"] for data in rows}
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM users ORDER BY created_at, id"
            ).fetchall()
            if row["id"] in new_ids
        ]
    finally:
        conn.close()


def list_users() -> list[dict]:
    conn = _connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM users ORDER BY created_at, id"
            ).fetchall()
        ]
    finally:
        conn.close()


def get_user(user_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def update_user(user_id: str, fields: dict) -> dict | None:
    if not fields:
        return None
    _check_columns(fields)
    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                (*fields.values(), user_id),
            )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def delete_user(user_id: str) -> bool:
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_or_create_meta(key: str, factory: Callable[[], str]) -> str:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return row["value"]
        value = factory()
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
        return value
    finally:
        conn.close()


def _hash_token(raw_token: str) -> str:
    secret = get_or_create_meta("secret_key", lambda: secrets.token_urlsafe(32))
    return hmac.new(
        secret.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def create_session(
    kind: Literal["admin", "user"], user_id: str | None, ttl_days: int
) -> str:
    """Create a session; returns the raw token, but only its hash is stored."""
    if kind not in ("admin", "user"):
        raise ValueError(f"invalid session kind: {kind!r}")
    raw_token = secrets.token_urlsafe(32)
    digest = _hash_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=ttl_days)).isoformat()
    created_at = now.isoformat()
    conn = _connect()
    try:
        with conn:
            if kind == "admin":
                conn.execute(
                    "INSERT INTO admin_sessions (token, created_at, expires_at)"
                    " VALUES (?, ?, ?)",
                    (digest, created_at, expires_at),
                )
            else:
                conn.execute(
                    "INSERT INTO user_sessions"
                    " (token, user_id, created_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    (digest, user_id, created_at, expires_at),
                )
    finally:
        conn.close()
    return raw_token


def get_session(kind: Literal["admin", "user"], raw_token: str) -> dict | None:
    """Look up a session by raw token (hashed before lookup); None if unknown/expired."""
    if kind not in ("admin", "user"):
        raise ValueError(f"invalid session kind: {kind!r}")
    digest = _hash_token(raw_token)
    conn = _connect()
    try:
        if kind == "admin":
            row = conn.execute(
                "SELECT * FROM admin_sessions WHERE token = ?", (digest,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM user_sessions WHERE token = ?", (digest,)
            ).fetchone()
        if row is None:
            return None
        if _parse_ts(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
        return dict(row)
    finally:
        conn.close()


def delete_session(kind: Literal["admin", "user"], raw_token: str) -> None:
    if kind not in ("admin", "user"):
        raise ValueError(f"invalid session kind: {kind!r}")
    digest = _hash_token(raw_token)
    conn = _connect()
    try:
        with conn:
            if kind == "admin":
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token = ?", (digest,)
                )
            else:
                conn.execute(
                    "DELETE FROM user_sessions WHERE token = ?", (digest,)
                )
    finally:
        conn.close()
