import logging
from pathlib import Path

import pytest

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


def test_auto_tls_defaults_off_and_accepts_env_style_override():
    assert _settings().AUTO_TLS is False
    assert _settings(AUTO_TLS=True).AUTO_TLS is True


def test_admin_credentials_default_to_admin_admin(monkeypatch):
    # Hermetic: neither the ambient .env nor shell env may provide the
    # fields. _env_file=None disables the model's env_file=".env"; delenv
    # removes the shell vars. The panel must now boot with admin/admin.
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    settings = Settings(_env_file=None)
    assert settings.ADMIN_USERNAME == "admin"
    assert settings.ADMIN_PASSWORD == "admin"


def test_default_admin_password_logs_warning(monkeypatch, caplog):
    # Booting on the default password must say so out loud — the docs tell
    # users to change it before exposing the host.
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with caplog.at_level(logging.WARNING, logger="panel.config"):
        Settings(_env_file=None)
    assert "default admin/admin" in caplog.text


def test_overridden_admin_password_is_silent(caplog):
    # An explicit password (the documented install paths) warns about
    # nothing.
    with caplog.at_level(logging.WARNING, logger="panel.config"):
        _settings()
    assert "default admin/admin" not in caplog.text
