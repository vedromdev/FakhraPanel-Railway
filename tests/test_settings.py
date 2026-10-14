from pathlib import Path

import pytest
from pydantic import ValidationError

from panel.config import Settings


def _settings(**overrides):
    base = {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "test-pass-123"}
    base.update(overrides)
    return Settings(**base)


def test_data_dir_prefers_explicit_env():
    assert _settings(DATA_DIR="/tmp/x").data_dir() == Path("/tmp/x")


def test_data_dir_falls_back_when_missing(monkeypatch):
    # DATA_DIR unset and /data exists → prefer /data
    monkeypatch.setattr("panel.config.os.path.isdir", lambda p: p == "/data")
    assert _settings(DATA_DIR="").data_dir() == Path("/data")
    # DATA_DIR unset and /data missing → fall back to ./data
    monkeypatch.setattr("panel.config.os.path.isdir", lambda p: False)
    assert _settings(DATA_DIR="").data_dir() == Path("./data")


def test_missing_admin_password_fails_fast(monkeypatch):
    # Hermetic: neither the ambient .env (README's own dev flow creates one)
    # nor ambient shell env may satisfy the required fields. _env_file=None
    # disables the model's env_file=".env"; delenv removes the shell vars.
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "ADMIN_PASSWORD" in str(excinfo.value)


def test_missing_admin_username_fails_fast(monkeypatch):
    # Same hermetic guard for the other required field (spec §8: both are
    # required; the panel must fail fast on either).
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "ADMIN_USERNAME" in str(excinfo.value)
