"""Admin JSON API: auth gating, user CRUD, bulk create, toggle, health, stats.

Every mutation must persist to the DB first and then hand the full user list
to `manager.apply_config`; the fake manager below records those calls so the
tests can pin the exact call count and the 500-with-saved rejection contract.
"""

import json
import os
import re
import shutil
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from panel import auth, db
from panel.app import create_app
from panel.config import Settings
from panel.constants import DEFAULT_SS_METHOD

REPO_ROOT = Path(__file__).resolve().parent.parent


def _real_xray() -> str | None:
    for candidate in (
        os.environ.get("XRAY_BIN"),
        str(REPO_ROOT / ".bin" / "xray"),
        shutil.which("xray"),
    ):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None

SAFE_CRED = re.compile(r"^[A-Za-z0-9_-]+$")  # URL-safe charset for credentials


class FakeManager:
    """Records every apply_config call; returns a configurable (ok, msg)."""

    def __init__(self, ok=True, msg="ok"):
        self.ok = ok
        self.msg = msg
        self.applied = []
        self.inbound_changes = []
        self.running = True
        self.last_error = ""

    def apply_config(self, users, inbounds):
        self.applied.append([dict(user) for user in users])
        return self.ok, self.msg

    def apply_inbound_change(self, users, inbounds, commit, revert):
        # faithful stub of the five-gate order: commit happens, revert only
        # exists for completeness (this stub never "fails after commit")
        self.inbound_changes.append(
            ([dict(u) for u in users], [dict(r) for r in inbounds])
        )
        commit()
        return True, "ok"

    def is_running(self):
        return self.running

    def uptime_s(self):
        return 1.25 if self.running else None


@pytest.fixture
def auth_client(settings):
    """App with a fake xray manager and a logged-in admin cookie jar."""
    with TestClient(create_app(settings)) as client:
        client.app.state.manager = FakeManager()
        login = client.post(
            "/login",
            data={
                "username": settings.ADMIN_USERNAME,
                "password": settings.ADMIN_PASSWORD,
            },
            follow_redirects=False,
        )
        assert login.status_code == 302
        yield client


@pytest.fixture
def fake_manager(auth_client):
    return auth_client.app.state.manager


def _create(client, **overrides):
    payload = {"username": "alice", "protocol": "vless"}
    payload.update(overrides)
    return client.post("/api/users", json=payload)


# --- brief Step 1 tests ------------------------------------------------------


def test_create_vless_user_returns_link_and_qr(auth_client):
    response = _create(auth_client, quota_gb=1.5, expiry_days=30)

    assert response.status_code == 201
    body = response.json()
    assert ":443" in body["link"]
    assert "security=tls" in body["link"]
    assert body["qr"].startswith("data:image/png")
    assert body["user"]["uuid"]

    plain = body["password"]
    stored = db.get_user(body["user"]["id"])["password"]
    assert stored.startswith("$2b$")
    assert plain not in stored
    assert auth.verify_password(plain, stored)


def test_create_rejects_bad_username(auth_client):
    response = _create(auth_client, username="bad user!")

    assert response.status_code == 422
    assert "detail" in response.json()


def test_duplicate_username_409(auth_client):
    assert _create(auth_client).status_code == 201

    response = _create(auth_client)

    assert response.status_code == 409
    users = auth_client.get("/api/users").json()["users"]
    assert len(users) == 1


def test_bulk_creates_numbered_users_and_all_links(auth_client, fake_manager):
    response = auth_client.post(
        "/api/users/bulk", json={"prefix": "guest", "count": 3, "protocol": "vless"}
    )

    assert response.status_code == 201
    body = response.json()
    usernames = sorted(user["username"] for user in body["users"])
    assert usernames == ["guest-001", "guest-002", "guest-003"]
    assert len(body["links"]) == 3
    # links stay aligned with their user rows (fragment carries the username)
    for user, link in zip(body["users"], body["links"]):
        assert link.endswith("#" + user["username"])
    assert len(fake_manager.applied) == 1


def test_bulk_conflict_creates_nothing(auth_client, fake_manager):
    assert _create(auth_client, username="p-001").status_code == 201
    fake_manager.applied.clear()  # count only the bulk attempt below

    response = auth_client.post(
        "/api/users/bulk", json={"prefix": "p", "count": 1, "protocol": "vless"}
    )

    assert response.status_code == 409
    assert response.json() == {"conflicts": ["p-001"]}
    users = auth_client.get("/api/users").json()["users"]
    assert [user["username"] for user in users] == ["p-001"]
    assert fake_manager.applied == []


def test_toggle_sets_and_clears_disable_reason(auth_client):
    user = _create(auth_client).json()["user"]

    disabled = auth_client.post(
        f"/api/users/{user['id']}/toggle", json={"enabled": False}
    )
    assert disabled.status_code == 200
    row = disabled.json()["user"]
    assert row["enabled"] == 0
    assert row["disable_reason"] == "manual"

    enabled = auth_client.post(
        f"/api/users/{user['id']}/toggle", json={"enabled": True}
    )
    assert enabled.status_code == 200
    row = enabled.json()["user"]
    assert row["enabled"] == 1
    assert row["disable_reason"] is None


def test_unauthenticated_and_user_cookie_get_401(auth_client):
    auth_client.cookies.clear()

    assert auth_client.get("/api/users").status_code == 401
    assert auth_client.get("/api/stats").status_code == 401

    auth_client.cookies.set("fakhra_user", "user-session", domain="testserver")
    assert auth_client.get("/api/users").status_code == 401


def test_health_is_public_and_reports_xray(auth_client, fake_manager):
    auth_client.cookies.clear()

    response = auth_client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["xray"] == {"running": True, "last_error": "", "uptime_s": 1.25}

    fake_manager.running = False
    fake_manager.last_error = "xray not running; restart attempt in 1s"

    degraded = auth_client.get("/api/health")
    assert degraded.status_code == 200
    assert degraded.json() == {
        "status": "degraded",
        "xray": {
            "running": False,
            "last_error": "xray not running; restart attempt in 1s",
            "uptime_s": None,
        },
    }


def test_config_rejection_returns_500_with_saved_true(auth_client, fake_manager):
    fake_manager.ok = False
    fake_manager.msg = "boom"

    response = _create(auth_client)

    assert response.status_code == 500
    body = response.json()
    assert "boom" in body["detail"]
    assert body["saved"] is True
    # DB is the source of truth: the rejected swap must not undo the insert.
    assert any(
        user["username"] == "alice" for user in db.list_users()
    ), "user missing from DB after rejected apply"


# --- binding-note coverage ---------------------------------------------------


def test_every_protocol_link_contains_no_none(auth_client):
    # Protocol fields must never be NULL for a created user (binding note 2):
    # vless/vmess always carry uuid, trojan/ss always carry credential.
    for index, protocol in enumerate(("vless", "vmess", "trojan", "ss"), start=1):
        response = _create(auth_client, username=f"u{index}", protocol=protocol)
        assert response.status_code == 201
        body = response.json()
        user = body["user"]
        if protocol in ("vless", "vmess"):
            assert user["uuid"]
        else:
            assert user["credential"]
            if protocol == "ss":
                assert user["ss_method"] == DEFAULT_SS_METHOD
        assert "None" not in body["link"]


def test_admin_provided_password_validated_and_used(auth_client):
    for unsafe in ("bad@pass", "has space", "no:pass"):
        response = _create(auth_client, username="safe", password=unsafe)
        assert response.status_code == 422, unsafe

    response = _create(auth_client, password="SafePass_99")

    assert response.status_code == 201
    body = response.json()
    assert body["password"] == "SafePass_99"
    stored = db.get_user(body["user"]["id"])["password"]
    assert stored.startswith("$2b$")
    assert auth.verify_password("SafePass_99", stored)


def test_generated_credentials_are_url_safe(auth_client):
    response = _create(auth_client, username="tro", protocol="trojan")

    assert response.status_code == 201
    body = response.json()
    assert SAFE_CRED.fullmatch(body["password"]), body["password"]
    assert SAFE_CRED.fullmatch(body["user"]["credential"])


def test_quota_gb_validation_range(auth_client):
    # DEPLOY E2E uses 0.001 GB: the floor is 0.001, not 0.1 (binding note 3).
    assert _create(auth_client, quota_gb=0.001).status_code == 201
    assert _create(auth_client, username="tiny", quota_gb=0.0005).status_code == 422
    assert _create(auth_client, username="huge", quota_gb=100001).status_code == 422
    assert _create(auth_client, username="nolimit", quota_gb=None).status_code == 201


def test_list_and_get_never_leak_plaintext_password(auth_client):
    plain = _create(auth_client).json()["password"]
    user_id = db.list_users()[0]["id"]

    listing = auth_client.get("/api/users")
    assert listing.status_code == 200
    assert plain not in listing.text

    fetched = auth_client.get(f"/api/users/{user_id}")
    assert fetched.status_code == 200
    assert plain not in fetched.text
    assert fetched.json()["user"]["password"].startswith("$2b$")


def test_verify_password_tolerates_malformed_hash():
    assert auth.verify_password("x", "not-a-hash") is False
    assert auth.verify_password("x", None) is False
    assert auth.verify_password("x", "") is False

    hashed = auth.hash_password("secret")
    assert hashed.startswith("$2b$")
    assert auth.verify_password("secret", hashed) is True
    assert auth.verify_password("wrong", hashed) is False


# --- routes without a brief-listed test --------------------------------------


def test_get_user_by_id_returns_link_and_404s_unknown(auth_client):
    user = _create(auth_client).json()["user"]

    response = auth_client.get(f"/api/users/{user['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == user["id"]
    assert ":443" in body["link"]
    assert body["qr"].startswith("data:image/png")
    assert auth_client.get("/api/users/no-such-id").status_code == 404


def test_patch_updates_fields_validates_and_404s(auth_client):
    user = _create(auth_client).json()["user"]

    response = auth_client.patch(
        f"/api/users/{user['id']}",
        json={
            "quota_gb": 5,
            "note": "team A",
            "expires_at": "2027-01-01T00:00:00+00:00",
        },
    )
    assert response.status_code == 200
    row = response.json()["user"]
    assert row["quota_gb"] == 5
    assert row["note"] == "team A"
    assert row["expires_at"] == "2027-01-01T00:00:00+00:00"

    cleared = auth_client.patch(f"/api/users/{user['id']}", json={"quota_gb": None})
    assert cleared.status_code == 200
    assert cleared.json()["user"]["quota_gb"] is None

    assert auth_client.patch(
        f"/api/users/{user['id']}", json={"quota_gb": 0.0005}
    ).status_code == 422
    assert auth_client.patch(
        f"/api/users/{user['id']}", json={"expires_at": "not-a-date"}
    ).status_code == 422
    assert auth_client.patch(
        "/api/users/no-such-id", json={"note": "x"}
    ).status_code == 404


def test_delete_removes_user_and_404s_unknown(auth_client):
    user = _create(auth_client).json()["user"]

    response = auth_client.delete(f"/api/users/{user['id']}")

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert db.get_user(user["id"]) is None
    assert auth_client.delete("/api/users/no-such-id").status_code == 404


def test_regenerate_rotates_vpn_credential(auth_client):
    user = _create(auth_client).json()["user"]

    response = auth_client.post(f"/api/users/{user['id']}/regenerate")

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["uuid"] != user["uuid"]
    assert body["user"]["uuid"] in body["link"]
    assert ":443" in body["link"]
    assert db.get_user(user["id"])["uuid"] == body["user"]["uuid"]
    assert auth_client.post(
        "/api/users/no-such-id/regenerate"
    ).status_code == 404


def test_reset_password_returns_new_plaintext_once(auth_client):
    old_plain = _create(auth_client).json()["password"]
    user_id = db.list_users()[0]["id"]

    response = auth_client.post(f"/api/users/{user_id}/reset-password")

    assert response.status_code == 200
    new_plain = response.json()["password"]
    assert new_plain != old_plain
    stored = db.get_user(user_id)["password"]
    assert stored.startswith("$2b$")
    assert auth.verify_password(new_plain, stored)
    assert not auth.verify_password(old_plain, stored)
    assert auth_client.post(
        "/api/users/no-such-id/reset-password"
    ).status_code == 404


def test_stats_counts_and_sums(auth_client):
    alice = _create(auth_client, quota_gb=2.5).json()["user"]
    _create(auth_client, username="bob")
    db.update_user(alice["id"], {"used_bytes": 1234})

    response = auth_client.get("/api/stats")

    assert response.status_code == 200
    assert response.json() == {
        "users_total": 2,
        "users_enabled": 2,
        "used_bytes_total": 1234,
        "quota_bytes_total": 2.5e9,
    }


# --- Task 7: inbound auto-assign --------------------------------------------


def _add_inbound(settings, inbound_id, protocol, port):
    """Insert an inbound row directly (the CRUD API lands in Task 5)."""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(settings.data_dir() / "panel.db")
    try:
        with conn:
            conn.execute(
                "INSERT INTO inbounds (id, protocol, port, settings, created_at)"
                " VALUES (?, ?, ?, '{}', ?)",
                (inbound_id, protocol, port, datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()


def _disable_protocol(settings, protocol):
    import sqlite3

    conn = sqlite3.connect(settings.data_dir() / "panel.db")
    try:
        with conn:
            conn.execute(
                "UPDATE inbounds SET enabled = 0 WHERE protocol = ?", (protocol,)
            )
    finally:
        conn.close()


def test_create_user_auto_assigns_inbound(auth_client):
    response = _create(auth_client)

    assert response.status_code == 201
    assert response.json()["user"]["inbound_id"] == "vless"  # seeded default


def test_create_user_explicit_inbound_id(auth_client, settings):
    _add_inbound(settings, "vless-b", "vless", 20001)

    response = _create(auth_client, inbound_id="vless-b")

    assert response.status_code == 201
    assert response.json()["user"]["inbound_id"] == "vless-b"


def test_create_user_wrong_protocol_inbound_400(auth_client):
    response = _create(auth_client, inbound_id="vmess")

    assert response.status_code == 400
    assert "detail" in response.json()


def test_create_user_no_enabled_inbound_400(auth_client, settings):
    _disable_protocol(settings, "vless")

    response = _create(auth_client)

    assert response.status_code == 400
    assert "vless" in response.json()["detail"]


def test_bulk_create_assigns_default_inbound(auth_client):
    response = auth_client.post(
        "/api/users/bulk",
        json={"prefix": "crew", "count": 3, "protocol": "vless"},
    )

    assert response.status_code == 201
    users = response.json()["users"]
    assert len(users) == 3
    assert all(u["inbound_id"] == "vless" for u in users)


# --- Task 5: inbound CRUD API ------------------------------------------------

def _reality_settings():
    return {
        "transport": "tcp",
        "security": "reality",
        "server_names": ["www.apple.com"],
        "dest": "www.apple.com:443",
    }


def test_create_inbound_ok_with_hint(auth_client):
    resp = auth_client.post(
        "/api/inbounds",
        json={"protocol": "vless", "port": 2048, "settings": _reality_settings()},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    inbound = body["inbound"]
    assert inbound["port"] == 2048
    assert body["firewall_hint"] == "ufw allow 2048/tcp"
    stored = db.get_inbound(inbound["id"])
    assert stored is not None
    parsed = json.loads(stored["settings"])
    assert parsed["private_key"] and parsed["public_key"]  # keypair generated
    assert len(parsed["short_ids"][0]) == 8
    assert parsed["flow"] == "xtls-rprx-vision"


def test_create_inbound_reserved_409(auth_client):
    resp = auth_client.post(
        "/api/inbounds", json={"protocol": "vless", "port": 80, "settings": {}}
    )
    assert resp.status_code == 409
    assert "reserved" in resp.json()["detail"]


def test_create_inbound_duplicate_port_409(auth_client):
    resp = auth_client.post(
        "/api/inbounds",
        json={"protocol": "vmess", "port": 10002, "settings": {"transport": "ws"}},
    )
    assert resp.status_code == 409
    assert "10002" in resp.json()["detail"]


def test_create_inbound_occupied_port_409(auth_client):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        resp = auth_client.post(
            "/api/inbounds",
            json={"protocol": "vless", "port": port, "listen": "127.0.0.1",
                  "settings": {}},
        )
    finally:
        holder.close()
    assert resp.status_code == 409
    assert "in use" in resp.json()["detail"]
    assert not [r for r in db.list_inbounds() if r["port"] == port]  # not persisted


def test_create_inbound_invalid_settings_400(auth_client):
    resp = auth_client.post(
        "/api/inbounds",
        json={"protocol": "vmess", "port": 2049,
              "settings": {"security": "reality",
                           "server_names": ["a.com"], "dest": "a.com:443"}},
    )
    assert resp.status_code == 400
    assert "reality" in resp.json()["detail"]


@pytest.mark.skipif(
    _real_xray() is None,
    reason="xray binary not found (run: bash scripts/fetch_xray.sh)",
)
def test_xray_reject_422_no_persist(tmp_path):
    """Gate 1 proven against the REAL binary: -test fails -> 422, saved=false,
    and the inbound never reaches the DB."""
    real_settings = Settings(
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="test-pass-123",
        PUBLIC_HOST="localhost",
        DATA_DIR=str(tmp_path),
        COOKIE_SECURE=False,
        XRAY_BIN=_real_xray(),
        ENV="test",
    )
    with TestClient(create_app(real_settings)) as client:
        login = client.post(
            "/login",
            data={"username": "admin", "password": "test-pass-123"},
            follow_redirects=False,
        )
        assert login.status_code == 302
        resp = client.post(
            "/api/inbounds",
            json={"protocol": "vless", "port": 2050,
                  "settings": {"security": "tls",
                               "cert_file": "/nonexistent/cert.pem",
                               "key_file": "/nonexistent/key.pem"}},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["saved"] is False
        assert body["detail"]
        assert not [r for r in db.list_inbounds() if r["port"] == 2050]


def test_delete_inbound_with_users_409(auth_client):
    _create(auth_client)  # a user lands on the vless seed

    resp = auth_client.delete("/api/inbounds/vless")

    assert resp.status_code == 409
    assert "users still assigned" in resp.json()["detail"]
    assert db.get_inbound("vless") is not None


def test_delete_inbound_ok(auth_client):
    resp = auth_client.delete("/api/inbounds/ss")

    assert resp.status_code == 200, resp.text
    assert db.get_inbound("ss") is None
    assert "ss" not in {r["id"] for r in db.list_inbounds()}


def test_delete_unknown_inbound_404(auth_client):
    resp = auth_client.delete("/api/inbounds/ghost")
    assert resp.status_code == 404


def test_update_unknown_inbound_404(auth_client):
    resp = auth_client.put(
        "/api/inbounds/ghost", json={"protocol": "vless", "port": 2051, "settings": {}}
    )
    assert resp.status_code == 404


def test_update_inbound_changes_port_and_settings(auth_client):
    resp = auth_client.put(
        "/api/inbounds/ss",
        json={"protocol": "ss", "port": 10004,
              "settings": {"transport": "ws", "security": "none", "path": "/u"}},
    )

    assert resp.status_code == 200, resp.text
    row = db.get_inbound("ss")
    assert row["port"] == 10004
    assert json.loads(row["settings"])["path"] == "/u"


def test_list_includes_user_counts(auth_client):
    _create(auth_client)

    resp = auth_client.get("/api/inbounds")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_counts"]["vless"] == 1
    ids = {r["id"] for r in body["inbounds"]}
    assert {"vless", "vmess", "trojan", "ss"} <= ids


_requires_container = pytest.mark.skipif(
    not os.environ.get("IN_DOCKER"),
    reason="needs the container (real xray binds the new inbound port)",
)


@_requires_container
def test_fresh_reality_inbound_serves_traffic(settings):
    """Spec §10 headline criterion: an inbound created through the API serves
    real client traffic — real xray, REALITY, applied by the five-gate flow."""
    import subprocess
    import time
    from panel.xray.config_builder import write_config

    with TestClient(create_app(settings)) as client:  # REAL manager, no FakeManager
        login = client.post(
            "/login",
            data={"username": settings.ADMIN_USERNAME,
                  "password": settings.ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert login.status_code == 302

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            new_port = probe.getsockname()[1]

        resp = client.post(
            "/api/inbounds",
            json={"protocol": "vless", "port": new_port,
                  "settings": _reality_settings()},
        )
        assert resp.status_code == 201, resp.text
        inbound = resp.json()["inbound"]
        seed = json.loads(inbound["settings"])

        # a user explicitly ON the fresh inbound (auto-assign would pick the
        # oldest vless inbound — the seed — and an empty clients list rejects)
        user_uuid = _first_user_uuid(client, inbound["id"])

        # echo target the tunneled connection must reach
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            echo_port = s.getsockname()[1]
        echo = subprocess.Popen(
            [os.environ.get("PYTHON") or "python3", "-c", f"""
import socket
srv = socket.socket()
srv.bind(("127.0.0.1", {echo_port}))
srv.listen(4)
while True:
    conn, _ = srv.accept()
    with conn:
        conn.sendall(b"HELLO-FROM-ECHO\\n")
"""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            client_port = s.getsockname()[1]
        client_config = {
            "log": {"loglevel": "error"},
            "inbounds": [{"listen": "127.0.0.1", "port": client_port,
                          "protocol": "dokodemo-door",
                          "settings": {"address": "127.0.0.1", "port": echo_port}}],
            "outbounds": [{
                "protocol": "vless",
                "settings": {"vnext": [{"address": "127.0.0.1", "port": new_port,
                    "users": [{"id": user_uuid,
                               "encryption": "none",
                               "flow": seed["flow"]}]}]},
                "streamSettings": {"network": "tcp", "security": "reality",
                    "realitySettings": {
                        "serverName": seed["server_names"][0],
                        "fingerprint": seed.get("fp", "chrome"),
                        "publicKey": seed["public_key"],
                        "shortId": seed["short_ids"][0]}},
            }],
        }
        cfg_path = settings.data_dir() / "fresh-reality-client.json"
        write_config(cfg_path, client_config)
        proc = subprocess.Popen(
            [settings.XRAY_BIN, "run", "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            banner = b""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not banner:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", client_port), timeout=2
                    ) as sock:
                        sock.settimeout(2)
                        banner = sock.recv(64)
                except OSError:
                    time.sleep(0.5)
            assert banner.startswith(b"HELLO-FROM-ECHO"), (
                f"fresh reality inbound never served traffic (port {new_port})"
            )
        finally:
            proc.kill()
            proc.wait()
            echo.kill()
            echo.wait()


def _first_user_uuid(client, inbound_id: str) -> str:
    """A user created through the API, explicitly assigned to inbound_id."""
    resp = _create(client, inbound_id=inbound_id)
    assert resp.status_code == 201, resp.text
    return resp.json()["user"]["uuid"]
