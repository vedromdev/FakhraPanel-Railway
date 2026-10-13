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
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "ADMIN_PASSWORD" in str(excinfo.value)
