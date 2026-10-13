from pathlib import Path

import pytest

from panel.config import Settings

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fake_xray() -> str:
    """Absolute path to an executable fake xray (tests never spawn a real one)."""
    script = _FIXTURES_DIR / "fake_xray.sh"
    script.chmod(script.stat().st_mode | 0o111)
    return str(script)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="test-pass-123",
        PUBLIC_HOST="fakhra.railway.app",
        DATA_DIR=str(tmp_path),
        COOKIE_SECURE=False,
        XRAY_BIN="xray",
        ENV="test",
    )
