"""Public /sub/{token}: base64 link, no session required, 404, rate limit."""

import base64
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from panel import db, links
from panel.app import create_app
from panel.ratelimit import RateLimiter


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _create_user(sub_token: str | None = None) -> dict:
    """A user row written straight to the DB (no API login needed here)."""
    row = {
        "id": str(uuid.uuid4()),
        "username": "alice",
        "password": "$2b$12$" + "x" * 53,
        "protocol": "vless",
        "uuid": str(uuid.uuid4()),
        "credential": None,
        "ss_method": None,
        "enabled": 1,
        "disable_reason": None,
        "expires_at": None,
        "quota_gb": None,
        "used_bytes": 0,
        "last_raw_bytes": 0,
        "note": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inbound_id": "vless",
        "sub_token": sub_token or uuid.uuid4().hex,
    }
    db.create_user(row)
    return row


def test_sub_returns_base64_link(client, settings):
    row = _create_user()

    resp = client.get(f"/sub/{row['sub_token']}")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "no-store"
    expected = links.build_link(
        db.get_user(row["id"]), db.get_inbound("vless"), settings.PUBLIC_HOST
    )
    assert base64.b64decode(resp.content).decode() == expected


def test_sub_needs_no_session(client):
    row = _create_user()
    assert not client.cookies  # fresh client: no admin/user cookie at all

    resp = client.get(f"/sub/{row['sub_token']}")

    assert resp.status_code == 200


def test_sub_unknown_token_404(client):
    assert client.get(f"/sub/{'a' * 32}").status_code == 404


def test_sub_rate_limited_429(client):
    row = _create_user()
    client.app.state.sub_rate = RateLimiter(
        window_s=300, max_calls=30, clock=lambda: 0.0  # frozen clock: window holds
    )

    statuses = [
        client.get(f"/sub/{row['sub_token']}").status_code for _ in range(31)
    ]

    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
