"""Self-service account area: user login, own-data page, session isolation.

The security contract under review (Focus #5): `fakhra_user` and
`fakhra_admin` are separate namespaces. Neither cookie unlocks the other's
routes — a user cookie never reaches the admin API, and an admin cookie never
opens the account page.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from panel import db
from panel.app import create_app
from panel.config import Settings
from panel.ratelimit import RateLimiter


class FakeManager:
    """Minimal manager stub: the account area never touches xray."""

    def __init__(self):
        self.running = True
        self.last_error = ""

    def is_running(self):
        return self.running

    def uptime_s(self):
        return 1.0 if self.running else None

    def apply_config(self, users, inbounds):
        return True, "ok"


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.manager = FakeManager()
        yield test_client


def _admin_login(client, settings):
    response = client.post(
        "/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": settings.ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302


def _create_user(client, username="alice", **overrides):
    payload = {"username": username, "protocol": "vless"}
    payload.update(overrides)
    response = client.post("/api/users", json=payload)
    assert response.status_code == 201
    return response.json()


def _user_login(client, username, password):
    return client.post(
        "/account/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_login_with_user_credentials_sets_separate_cookie(client, settings):
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()  # from here on the jar must only gain fakhra_user

    response = _user_login(client, "alice", created["password"])

    assert response.status_code == 302
    assert response.headers["location"] == "/account"
    assert "fakhra_user" in response.cookies
    assert "fakhra_admin" not in response.cookies


def test_account_page_shows_own_link_and_usage(client, settings):
    _admin_login(client, settings)
    created = _create_user(client, "alice", quota_gb=2.5)
    client.cookies.clear()
    assert _user_login(client, "alice", created["password"]).status_code == 302

    response = client.get("/account")

    assert response.status_code == 200
    html = response.text
    assert created["user"]["uuid"] in html  # their own credential, via the link
    assert ":443" in html
    assert "data-usage" in html
    assert "alice" in html
    # The bcrypt hash is part of the DB row but must never be rendered.
    assert "$2b$" not in html


def test_account_page_excludes_other_users(client, settings):
    """(Review Focus #5) The template gets exactly one row: the caller's own."""
    _admin_login(client, settings)
    alice = _create_user(client, "alice")
    bob = _create_user(client, "bob")
    client.cookies.clear()
    assert _user_login(client, "alice", alice["password"]).status_code == 302

    response = client.get("/account")

    assert response.status_code == 200
    html = response.text
    assert bob["user"]["uuid"] not in html
    assert "bob" not in html
    assert alice["user"]["uuid"] in html


def test_user_cookie_cannot_reach_admin_api(client, settings):
    """(Review Focus #5) A user session must never authenticate require_admin."""
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()
    assert _user_login(client, "alice", created["password"]).status_code == 302
    assert "fakhra_admin" not in client.cookies

    response = client.get("/api/users")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not authenticated"}


def test_admin_cookie_cannot_open_account_page(client, settings):
    """(Review Focus #5) An admin session must never authenticate the user area."""
    _admin_login(client, settings)
    assert "fakhra_user" not in client.cookies

    response = client.get("/account", follow_redirects=False)

    assert response.status_code == 200  # renders the login form, not the page
    assert 'action="/account/login"' in response.text
    assert "fakhra_user" not in response.cookies


def test_admin_credentials_do_not_open_account(client, settings):
    _admin_login(client, settings)
    client.cookies.clear()

    response = _user_login(
        client, settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD
    )

    assert response.status_code == 200
    assert "Invalid" in response.text
    assert "fakhra_user" not in response.cookies
    assert response.headers.get_list("set-cookie") == []


def test_wrong_user_password_rejected(client, settings):
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()

    response = _user_login(client, "alice", "wrong-pass")

    assert response.status_code == 200
    assert "Invalid" in response.text
    assert "fakhra_user" not in response.cookies
    assert response.headers.get_list("set-cookie") == []


def test_logout_clears_cookie_and_revokes_session(client, settings):
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()
    assert _user_login(client, "alice", created["password"]).status_code == 302
    token = client.cookies["fakhra_user"]

    response = client.get("/account/logout", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/account"
    assert "fakhra_user" not in client.cookies
    # Reuse the captured (now logged-out) cookie: the session is revoked.
    client.cookies.set("fakhra_user", token)
    page = client.get("/account", follow_redirects=False)
    assert page.status_code == 200
    assert 'action="/account/login"' in page.text


def test_account_login_rate_limited_on_sixth_attempt(client, settings):
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()
    client.app.state.rate = RateLimiter()  # setup consumed hits; fresh budget

    for _ in range(5):
        response = _user_login(client, "alice", "wrong-pass")
        assert response.status_code == 200
    sixth = _user_login(client, "alice", "wrong-pass")

    assert sixth.status_code == 429
    assert "Too many attempts" in sixth.text


def test_account_page_shows_computed_status(client, settings):
    """Status comes from the jobs.evaluate rules: active, then expired, then limited."""
    _admin_login(client, settings)
    created = _create_user(client, "alice", quota_gb=1.0)
    user_id = created["user"]["id"]
    client.cookies.clear()
    assert _user_login(client, "alice", created["password"]).status_code == 302

    active = client.get("/account")
    assert 'data-status="active"' in active.text

    db.update_user(user_id, {"expires_at": "2000-01-01T00:00:00+00:00"})
    expired = client.get("/account")
    assert 'data-status="expired"' in expired.text

    db.update_user(
        user_id,
        {"expires_at": None, "used_bytes": int(1.5e9)},
    )
    limited = client.get("/account")
    assert 'data-status="limited"' in limited.text


# --- review fixes: per-surface rate-limit budgets ----------------------------


def test_account_login_budget_does_not_lock_out_admin_login(client, settings):
    """(Review Important 1) Exhausting the user-login budget from one IP must
    not deny the operator's admin login: the two surfaces have separate
    per-IP budgets, while each stays throttled at 5/60s."""
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()
    client.app.state.rate = RateLimiter()  # setup consumed hits; fresh budget

    for _ in range(5):
        assert _user_login(client, "alice", "wrong-pass").status_code == 200
    assert _user_login(client, "alice", "wrong-pass").status_code == 429

    admin = client.post(
        "/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": settings.ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert admin.status_code == 302  # admin budget untouched by account fails


def test_admin_login_budget_does_not_lock_out_account_login(client, settings):
    """(Review Important 1) The symmetric direction: exhausted admin budget
    leaves the user-login budget intact."""
    _admin_login(client, settings)
    created = _create_user(client, "alice")
    client.cookies.clear()
    client.app.state.rate = RateLimiter()  # setup consumed hits; fresh budget

    for _ in range(5):
        response = client.post(
            "/login",
            data={
                "username": settings.ADMIN_USERNAME,
                "password": "wrong-pass",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
    sixth = client.post(
        "/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": "wrong-pass",
        },
        follow_redirects=False,
    )
    assert sixth.status_code == 429

    user = _user_login(client, "alice", created["password"])
    assert user.status_code == 302  # account budget untouched by admin fails


# --- review addition: blank PUBLIC_HOST warns at startup ---------------------


def test_blank_public_host_logs_error_at_startup(tmp_path, caplog):
    """(Controller addition) An unset PUBLIC_HOST must be loud at boot, not
    a healthy-looking panel that 500s on the first share link."""
    blank = Settings(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="test-pass-123",
        PUBLIC_HOST="",
        DATA_DIR=str(tmp_path),
        COOKIE_SECURE=False,
        XRAY_BIN="xray",
        ENV="test",
    )
    with caplog.at_level(logging.ERROR):
        with TestClient(create_app(blank)):
            pass

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("PUBLIC_HOST" in record.message for record in errors)


def test_set_public_host_logs_no_startup_error(settings, caplog):
    with caplog.at_level(logging.ERROR):
        with TestClient(create_app(settings)):
            pass

    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "PUBLIC_HOST" in record.message
    ]
