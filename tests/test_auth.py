import pytest
from fastapi.testclient import TestClient

from panel.app import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_wrong_password_rejected(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Invalid" in response.text
    assert "fakhra_admin" not in response.cookies
    assert response.headers.get_list("set-cookie") == []


def test_correct_password_sets_admin_cookie_and_redirects(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-pass-123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "fakhra_admin" in response.cookies
    admin_cookies = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith("fakhra_admin=")
    ]
    assert admin_cookies, "fakhra_admin Set-Cookie header missing"
    assert "HttpOnly" in admin_cookies[0]


def test_root_redirects_to_login_when_unauthenticated(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_api_returns_401_json_when_unauthenticated(client):
    response = client.get("/api/users")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not authenticated"}


def test_valid_admin_cookie_can_call_api(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-pass-123"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    api = client.get("/api/users")
    assert api.status_code == 200
    assert api.json() == {"users": []}


def test_logout_revokes_session(client):
    login = client.post(
        "/login",
        data={"username": "admin", "password": "test-pass-123"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    token = client.cookies["fakhra_admin"]
    logout = client.get("/logout", follow_redirects=False)
    assert logout.status_code == 302
    assert logout.headers["location"] == "/login"
    # Reuse the captured (now logged-out) cookie on the API.
    client.cookies.set("fakhra_admin", token)
    response = client.get("/api/users")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not authenticated"}


def test_login_rate_limited_on_sixth_attempt(client):
    for _ in range(5):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong-pass"},
            follow_redirects=False,
        )
        assert response.status_code == 200
    sixth = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-pass"},
        follow_redirects=False,
    )
    assert sixth.status_code == 429
