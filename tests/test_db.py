import hashlib
import hmac
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from panel import bootstrap, db
from panel.config import Settings

USER_COLUMNS = {
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
}


@pytest.fixture(autouse=True)
def _runtime(settings):
    bootstrap.ensure_runtime(settings)
    yield


def _vless_user(**overrides):
    user = {
        "id": str(uuid.uuid4()),
        "username": "alice",
        "password": "$2b$12$" + "x" * 53,  # bcrypt-format hash; real hashing is a later task
        "protocol": "vless",
        "uuid": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    user.update(overrides)
    return user


def test_create_and_get_user_roundtrip():
    user = _vless_user()
    created = db.create_user(user)
    fetched = db.get_user(user["id"])
    assert fetched is not None
    assert set(fetched) == USER_COLUMNS
    for key, value in user.items():
        assert fetched[key] == value
    assert fetched["enabled"] == 1
    assert fetched["used_bytes"] == 0
    assert fetched["last_raw_bytes"] == 0
    assert fetched["ss_method"] == "aes-256-gcm"
    assert fetched["note"] == ""
    assert fetched["disable_reason"] is None
    assert created == fetched


def test_username_unique_violation_creates_nothing():
    before = len(db.list_users())
    good = _vless_user(username="good-user")
    duplicate = _vless_user(username="good-user")  # same username, different id
    with pytest.raises(sqlite3.IntegrityError):
        db.create_users([good, duplicate])
    assert len(db.list_users()) == before
    assert db.get_user(good["id"]) is None
    assert db.get_user(duplicate["id"]) is None


def test_update_user_sets_expiry_and_quota():
    user = _vless_user()
    db.create_user(user)
    expires_at = "2030-01-01T00:00:00+00:00"
    updated = db.update_user(user["id"], {"expires_at": expires_at, "quota_gb": 50.0})
    assert updated is not None
    assert updated["expires_at"] == expires_at
    assert updated["quota_gb"] == 50.0
    refetched = db.get_user(user["id"])
    assert refetched["expires_at"] == expires_at
    assert refetched["quota_gb"] == 50.0
    # empty fields dict is a no-op
    assert db.update_user(user["id"], {}) is None


def test_install_token_stable_across_restarts(settings):
    assert (settings.data_dir() / "panel.db").exists()
    first = db.get_or_create_meta("install_token", lambda: "unused-factory-call")
    bootstrap.ensure_runtime(settings)
    second = db.get_or_create_meta("install_token", lambda: "unused-factory-call")
    assert first == second
    assert first != "unused-factory-call"
    # ensure_runtime seeds schema_version; a different factory proves it pre-existed
    assert db.get_or_create_meta("schema_version", lambda: "other") == "1"


def test_ensure_runtime_auto_tls_renders_domain_caddyfile(settings):
    auto = _settings_with(settings, AUTO_TLS=True, PUBLIC_HOST="vpn.example.com")
    bootstrap.ensure_runtime(auto)
    rendered = (auto.data_dir() / "Caddyfile").read_text(encoding="utf-8")
    assert "{$PUBLIC_HOST}" in rendered
    assert "auto_https off" not in rendered
    assert ":{$CADDY_PORT}" not in rendered


def test_ensure_runtime_auto_tls_without_public_host_fails_fast(settings):
    with pytest.raises(ValueError, match="PUBLIC_HOST"):
        bootstrap.ensure_runtime(
            _settings_with(settings, AUTO_TLS=True, PUBLIC_HOST="")
        )


def _settings_with(settings, **overrides):
    """Same settings (same DATA_DIR/tmp DB), field-level overrides only."""
    kwargs = settings.model_dump()
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_secret_key_env_is_seeded_into_meta(settings):
    # spec §8: SECRET_KEY is the session-signing secret — when set, the meta
    # row that _hash_token HMACs against must EQUAL it after ensure_runtime.
    bootstrap.ensure_runtime(_settings_with(settings, SECRET_KEY="unit-test-key"))
    assert db.read_meta("secret_key") == "unit-test-key"


def test_rotating_secret_key_replaces_meta_and_invalidates_sessions(settings):
    # a session signed under the old (auto-generated) secret...
    raw = db.create_session("admin", None, ttl_days=1)
    assert db.get_session("admin", raw) is not None
    old_secret = db.read_meta("secret_key")

    # ...then the operator rotates SECRET_KEY and reboots
    bootstrap.ensure_runtime(_settings_with(settings, SECRET_KEY="rotated-key"))

    assert old_secret != "rotated-key"  # sanity: it really rotated
    assert db.read_meta("secret_key") == "rotated-key"
    # in-place overwrite, never a delete: the row was present throughout
    assert db.read_meta("secret_key") is not None
    # the pre-rotation session must no longer validate
    assert db.get_session("admin", raw) is None


def test_blank_secret_key_mints_random_once_and_stays_stable(settings):
    blank = _settings_with(settings, SECRET_KEY="")
    bootstrap.ensure_runtime(blank)
    first = db.read_meta("secret_key")
    assert first  # non-empty random value
    bootstrap.ensure_runtime(blank)
    assert db.read_meta("secret_key") == first  # stable across boots


def test_delete_user_cascades_sessions(settings):
    user = _vless_user()
    db.create_user(user)
    raw = db.create_session("user", user["id"], ttl_days=1)
    assert db.get_session("user", raw) is not None
    assert db.delete_user(user["id"]) is True
    assert db.get_session("user", raw) is None


def test_session_ops_require_initialized_secret(tmp_path):
    # a DB from init_db alone (no ensure_runtime) has no secret_key yet
    db.init_db(tmp_path / "bare.db")
    with pytest.raises(RuntimeError, match="secret_key"):
        db.get_session("admin", "any-token")
    with pytest.raises(RuntimeError, match="secret_key"):
        db.create_session("admin", None, ttl_days=1)
    # ... and session ops must not silently mint one
    conn = sqlite3.connect(tmp_path / "bare.db")
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("secret_key",)
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_session_roundtrip_and_expiry(settings):
    raw = db.create_session("admin", None, ttl_days=1)
    row = db.get_session("admin", raw)
    assert row is not None
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
    assert db.get_session("admin", "wrong-token") is None

    # the raw token is never stored — only its keyed hash
    conn = sqlite3.connect(settings.data_dir() / "panel.db")
    try:
        stored = conn.execute("SELECT token FROM admin_sessions").fetchone()[0]
        secret = conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("secret_key",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored != raw
    assert len(stored) == 64
    assert stored == hmac.new(
        secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # a session whose expiry is in the past resolves to None
    expired_raw = "expired-raw-token"
    expired_digest = hmac.new(
        secret.encode("utf-8"), expired_raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(settings.data_dir() / "panel.db")
    try:
        conn.execute(
            "INSERT INTO admin_sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
            (
                expired_digest,
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert db.get_session("admin", expired_raw) is None
