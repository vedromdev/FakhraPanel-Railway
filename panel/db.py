"""SQLite data layer: users, sessions, and meta (WAL mode, parameterized SQL)."""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from panel.constants import DEFAULT_SS_METHOD
from panel.xray import keys as x25519_keys

_db_path: Path | None = None

CURRENT_SCHEMA_VERSION = 2

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
    "inbound_id",
    "sub_token",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK (protocol IN ('vless','vmess','trojan','ss')),
  uuid TEXT,
  credential TEXT,
  ss_method TEXT DEFAULT '{DEFAULT_SS_METHOD}',
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
  user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
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


_INBOUNDS_DDL = """
CREATE TABLE IF NOT EXISTS inbounds (
  id TEXT PRIMARY KEY,
  protocol TEXT NOT NULL CHECK (protocol IN ('vless','vmess','trojan','ss')),
  listen TEXT NOT NULL DEFAULT '0.0.0.0',
  port INTEGER NOT NULL UNIQUE,
  remark TEXT NOT NULL DEFAULT '',
  settings TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
"""


def _seed_inbounds() -> list[tuple]:
    """The four v0.2 defaults; protocol-named ids keep stats continuity."""
    now = datetime.now(timezone.utc).isoformat()
    private_key, public_key = x25519_keys.generate_keypair()
    vless_settings = json.dumps(
        {
            "transport": "tcp",
            "security": "reality",
            "flow": "xtls-rprx-vision",
            "private_key": private_key,
            "public_key": public_key,
            "server_names": ["www.apple.com"],
            "dest": "www.apple.com:443",
            "short_ids": [secrets.token_hex(4)],
            "fp": "chrome",
        }
    )
    ws = json.dumps({"transport": "ws", "security": "none", "path": "/"})
    plain = json.dumps({"transport": "tcp", "security": "none"})
    return [
        ("vless", 443, vless_settings, now),
        ("vmess", 10002, ws, now),
        ("trojan", 10003, plain, now),
        ("ss", 10004, plain, now),
    ]


def migrate() -> None:
    """Idempotent v1→v2 upgrade: inbounds table, users columns, seeds, backfill.

    Fresh databases run the same path as old ones (_SCHEMA stays v1-shaped),
    so there is exactly one upgrade code path. Seeds run only inside the
    version bump, so an admin-deleted default is never resurrected.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("schema_version",)
        ).fetchone()
        version = int(row["value"]) if row is not None else 1
        if version >= CURRENT_SCHEMA_VERSION:
            return
        with conn:
            columns = {
                info["name"] for info in conn.execute("PRAGMA table_info(users)")
            }
            if "inbound_id" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN inbound_id TEXT")
            if "sub_token" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN sub_token TEXT")
            conn.execute(_INBOUNDS_DDL)
            for protocol, port, settings_json, created_at in _seed_inbounds():
                conn.execute(
                    "INSERT OR IGNORE INTO inbounds"
                    " (id, protocol, port, settings, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (protocol, protocol, port, settings_json, created_at),
                )
            conn.execute(
                "UPDATE users SET inbound_id = protocol WHERE inbound_id IS NULL"
            )
            conn.execute(
                "UPDATE users SET sub_token = lower(hex(randomblob(16)))"
                " WHERE sub_token IS NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub_token"
                " ON users(sub_token)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(CURRENT_SCHEMA_VERSION)),
            )
    finally:
        conn.close()


def list_inbounds() -> list[dict]:
    conn = _connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM inbounds ORDER BY created_at, id"
            ).fetchall()
        ]
    finally:
        conn.close()


def get_inbound(inbound_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM inbounds WHERE id = ?", (inbound_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


_INBOUND_COLUMNS = (
    "id", "protocol", "listen", "port", "remark", "settings", "enabled",
    "created_at",
)
# columns a PUT may change — id and created_at are immutable identity
_INBOUND_MUTABLE = ("protocol", "listen", "port", "remark", "settings", "enabled")


def create_inbound(data: dict) -> dict:
    """Insert one inbound row (settings as JSON text); returns the row."""
    unknown = set(data) - set(_INBOUND_COLUMNS)
    if unknown:
        raise ValueError(f"unknown inbound columns: {sorted(unknown)}")
    missing = set(_INBOUND_COLUMNS) - set(data)
    if missing:
        raise ValueError(f"missing inbound columns: {sorted(missing)}")
    conn = _connect()
    try:
        with conn:
            columns = ", ".join(_INBOUND_COLUMNS)
            placeholders = ", ".join(f":{c}" for c in _INBOUND_COLUMNS)
            conn.execute(
                f"INSERT INTO inbounds ({columns}) VALUES ({placeholders})", data
            )
        row = conn.execute(
            "SELECT * FROM inbounds WHERE id = ?", (data["id"],)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_inbound(inbound_id: str, fields: dict) -> dict | None:
    """Partial update of mutable columns; None if no such inbound."""
    unknown = set(fields) - set(_INBOUND_MUTABLE)
    if unknown:
        raise ValueError(f"unknown inbound columns: {sorted(unknown)}")
    if not fields:
        return get_inbound(inbound_id)
    conn = _connect()
    try:
        with conn:
            assignments = ", ".join(f"{c} = :{c}" for c in fields)
            cursor = conn.execute(
                f"UPDATE inbounds SET {assignments} WHERE id = :__id",
                {**fields, "__id": inbound_id},
            )
        if cursor.rowcount == 0:
            return None
        return get_inbound(inbound_id)
    finally:
        conn.close()


def delete_inbound(inbound_id: str) -> bool:
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM inbounds WHERE id = ?", (inbound_id,))
        return cursor.rowcount > 0
    finally:
        conn.close()


def count_users_per_inbound() -> dict[str, int]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT inbound_id, COUNT(*) AS n FROM users"
            " WHERE inbound_id IS NOT NULL GROUP BY inbound_id"
        ).fetchall()
        return {row["inbound_id"]: row["n"] for row in rows}
    finally:
        conn.close()


def default_inbound_id(protocol: str) -> str | None:
    """Oldest enabled inbound of this protocol (user auto-assign target)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM inbounds WHERE protocol = ? AND enabled = 1"
            " ORDER BY created_at, id LIMIT 1",
            (protocol,),
        ).fetchone()
        return row["id"] if row is not None else None
    finally:
        conn.close()


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


def read_meta(key: str) -> str | None:
    """Read a meta value without ever writing."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None
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
    secret = read_meta("secret_key")
    if not secret:
        raise RuntimeError("secret_key not initialized; run bootstrap.ensure_runtime")
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
