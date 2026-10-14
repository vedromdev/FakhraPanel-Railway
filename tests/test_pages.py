"""Dashboard pages: auth gating, user rows, xray banner states, API wiring.

The banner contract is exact: `id="xray-banner"` must be absent from the HTML
while xray is up and present (carrying `last_error`) when it is down.
"""

from fastapi.testclient import TestClient

from panel.app import create_app


class FakeManager:
    """Minimal manager stub: the pages only read is_running/last_error."""

    def __init__(self):
        self.running = True
        self.last_error = ""

    def is_running(self):
        return self.running

    def uptime_s(self):
        return 1.0 if self.running else None

    def apply_config(self, users, inbounds):
        return True, "ok"


def _login(client, settings):
    login = client.post(
        "/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": settings.ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert login.status_code == 302


def _create(client, username="alice", protocol="vless", **overrides):
    payload = {"username": username, "protocol": protocol}
    payload.update(overrides)
    response = client.post("/api/users", json=payload)
    assert response.status_code == 201
    return response.json()


def test_dashboard_renders_users_and_title(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        _create(client, quota_gb=1.5, expiry_days=30)

        response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "Fakhra Panel" in html
        assert "alice" in html
        assert "data-user-id" in html
        # /dashboard is the same page.
        alias = client.get("/dashboard")
        assert alias.status_code == 200
        assert "data-user-id" in alias.text


def test_dashboard_requires_auth(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/login"


def test_banner_hidden_when_xray_up_shown_when_down(settings):
    with TestClient(create_app(settings)) as client:
        manager = FakeManager()
        client.app.state.manager = manager
        _login(client, settings)

        up = client.get("/")
        assert up.status_code == 200
        assert "xray-banner" not in up.text

        manager.running = False
        manager.last_error = "xray exited: config rejected"
        down = client.get("/")
        assert down.status_code == 200
        assert "xray-banner" in down.text
        assert "xray exited: config rejected" in down.text


def test_dashboard_js_calls_api_paths(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)

        response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "/api/users" in html
        assert "/api/health" in html


def test_dashboard_js_surfaces_bulk_conflicts(settings):
    """api() must fall back to body.conflicts: bulk 409s carry only
    {"conflicts": [...]} with no detail key, so detail-only error handling
    would show the operator an opaque "Request failed (409)".

    The JS isn't executed in these tests (TestClient serves HTML; no
    browser), so this pins the served fallback logic itself — same seam as
    test_dashboard_js_calls_api_paths.
    """
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)

        response = client.get("/")

        assert response.status_code == 200
        html = response.text
        # api()'s error branch reads body.conflicts when body.detail is absent.
        assert "body.conflicts" in html
        # The dead indexOf("already exists") special-case is removed.
        assert 'indexOf("already exists")' not in html


# --- A3 Task 2: /inbounds page -----------------------------------------------


def test_inbounds_page_requires_auth(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/inbounds", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/login"


def test_inbounds_page_renders_seeds_and_editor_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        response = client.get("/inbounds")
        assert response.status_code == 200
        html = response.text
        for inbound_id, port in (("vless", "443"), ("vmess", "10002"),
                                 ("trojan", "10003"), ("ss", "10004")):
            assert f'data-inbound-id="{inbound_id}"' in html
            assert port in html
        assert 'id="add-inbound"' in html
        assert 'id="inbound-modal"' in html
        assert 'id="inbounds-list"' in html
        for group in ("ws", "grpc", "xhttp", "tls", "reality"):
            assert f'data-group="{group}"' in html


def test_nav_links_between_dashboard_and_inbounds(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        assert 'href="/inbounds"' in client.get("/").text
        page = client.get("/inbounds")
        assert page.status_code == 200
        assert 'href="/"' in page.text
