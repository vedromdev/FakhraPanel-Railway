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


# --- A3 Task 3: JS core ------------------------------------------------------


def test_inbounds_page_js_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        response = client.get("/inbounds")
        assert response.status_code == 200
        html = response.text
        assert "/api/inbounds" in html
        assert "SECURITY_BY_PROTOCOL" in html
        assert "TRANSPORTS_BY_PROTOCOL" in html
        assert "applyVisibility" in html
        assert "body.conflicts" in html  # api() fallback, dashboard parity


# --- A3 Task 4: save paths ---------------------------------------------------


def test_inbounds_js_save_paths_and_error_copy(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert 'method: "POST"' in html
        assert 'method: "PUT"' in html
        assert "Nothing was saved" in html
        assert "firewall_hint" in html


# --- A3 Task 5: card actions -------------------------------------------------


def test_inbounds_js_card_actions_copy(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert "data-error-for" in html
        assert "cannot connect until re-enabled" in html


# --- A3 Task 6: advanced raw-JSON ---------------------------------------------


def test_inbounds_js_advanced_json_contract(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert "applyJson" in html
        assert "Invalid JSON" in html


def test_inbounds_js_failure_paths_resync_list(settings):
    """Final-review fix pass: every failure path re-syncs the list (stale-row
    races per plan Review Focus 5), a page-level #list-error slot exists for
    load failures, and the REALITY keypair is never pruned (pruning it
    silently regenerates the keypair server-side and breaks every client)."""
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        html = client.get("/inbounds").text
        assert 'id="list-error"' in html
        # the three failure-path catches minimum re-sync after showing errors
        assert html.count("refresh().catch(") >= 3
        assert "NEVER pruned" in html


# --- A3.5: themes -----------------------------------------------------------

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_theme_boot_script_reaches_login(settings):
    """The pre-paint boot script lives in base.html, so even the login pages
    (no head block of their own) load the saved theme with no flash."""
    with TestClient(create_app(settings)) as client:
        html = client.get("/login").text
        assert 'localStorage.getItem("fakhra-theme")' in html
        assert 'href="/static/style.css"' in html


def test_theme_picker_on_app_pages(settings):
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        _login(client, settings)
        for path in ("/", "/inbounds"):
            html = client.get(path).text
            assert 'id="theme-picker"' in html
            for theme in ("dark", "light", "ocean", "solarized"):
                assert f'value="{theme}"' in html
        # account page ships the picker too (template-level contract;
        # serving it needs a user session, covered by test_account)
        account = (_REPO_ROOT / "panel/templates/account.html").read_text()
        assert 'id="theme-picker"' in account


def test_theme_stylesheet_contract():
    css = (_REPO_ROOT / "panel/templates/static/style.css").read_text()
    for theme in ("light", "ocean", "solarized"):
        assert f'[data-theme="{theme}"]' in css
    # variable-ization: alerts and protocol chips route through theme vars
    assert "var(--alert-error-text)" in css
    assert "var(--proto-vless)" in css
