import pytest

from panel.config import Settings


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
