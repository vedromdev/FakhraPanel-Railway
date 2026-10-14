"""Tests for panel.paths and the Xray config builder (inbounds-driven)."""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from panel import db
from panel.constants import DEFAULT_SS_METHOD
from panel.paths import PORTS, vpn_path
from panel.xray import keys
from panel.xray.config_builder import build_config, write_config

PUBLIC_HOST = "fakhra.railway.app"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _inbound(protocol: str, *, inbound_id: str | None = None, settings=None,
             port: int = 10001, enabled: int = 1, listen: str = "0.0.0.0") -> dict:
    """An inbound row in the shape db.list_inbounds() returns."""
    if settings is None:
        settings = {"transport": "tcp", "security": "none"}
    return {
        "id": inbound_id or f"{protocol}-in",
        "protocol": protocol,
        "listen": listen,
        "port": port,
        "settings": json.dumps(settings),  # db stores JSON text
        "enabled": enabled,
    }


def _user(protocol: str, inbound_id: str, *, user_id: str | None = None,
          enabled: int = 1) -> dict:
    """A user row in the shape db.list_users() returns."""
    uid = user_id or f"{protocol}-uid"
    return {
        "id": uid,
        "username": uid,
        "password": "hash",
        "protocol": protocol,
        "uuid": str(uuid.uuid4()),
        "credential": f"cred-{uid}",
        "ss_method": "aes-128-gcm",
        "enabled": enabled,
        "inbound_id": inbound_id,
    }


def _reality_settings() -> dict:
    private_key, public_key = keys.generate_keypair()
    return {
        "transport": "tcp", "security": "reality",
        "flow": "xtls-rprx-vision",
        "private_key": private_key, "public_key": public_key,
        "server_names": ["www.apple.com"],
        "dest": "www.apple.com:443", "short_ids": ["abcd1234"],
        "fp": "chrome",
    }


def _find_xray() -> str | None:
    """Locate a real Xray binary: XRAY_BIN env, <repo>/.bin/xray, or PATH."""
    candidates = []
    env_bin = os.environ.get("XRAY_BIN")
    if env_bin:
        candidates.append(env_bin)
    candidates.append(str(REPO_ROOT / ".bin" / "xray"))
    on_path = shutil.which("xray")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


XRAY_BIN = _find_xray()


def test_vpn_path_format():
    assert vpn_path("abc123", "vless") == "/ray-abc123/vless"


def test_disabled_inbound_excluded():
    rows = [
        _inbound("vless", inbound_id="vless", port=443,
                 settings=_reality_settings()),
        _inbound("vmess", inbound_id="vmess", port=10002, enabled=0),
    ]
    users = [_user("vless", "vless"), _user("vmess", "vmess", enabled=0)]
    cfg = build_config(rows, users, PUBLIC_HOST)
    tags = {i["tag"] for i in cfg["inbounds"]}
    assert tags == {"in-vless", "api"}


def test_build_config_renders_all_seed_inbounds():
    rows = [
        _inbound("vless", inbound_id="vless", port=443,
                 settings=_reality_settings()),
        _inbound("vmess", inbound_id="vmess", port=10002,
                 settings={"transport": "ws", "security": "none", "path": "/"}),
        _inbound("trojan", inbound_id="trojan", port=10003),
        _inbound("ss", inbound_id="ss", port=10004),
    ]
    cfg = build_config(rows, [], PUBLIC_HOST)  # no users: fresh-install boot
    by_tag = {i["tag"]: i for i in cfg["inbounds"]}
    assert set(by_tag) == {"in-vless", "in-vmess", "in-trojan", "in-ss", "api"}
    xray_proto = {"vless": "vless", "vmess": "vmess", "trojan": "trojan",
                  "ss": "shadowsocks"}
    for row in rows:
        inbound = by_tag[f"in-{row['id']}"]
        assert inbound["protocol"] == xray_proto[row["protocol"]]
        assert inbound["port"] == row["port"]
        assert inbound["listen"] == "0.0.0.0"
        assert inbound["sniffing"]["enabled"] is True
    vless = by_tag["in-vless"]["streamSettings"]
    assert vless["network"] == "tcp"
    assert vless["security"] == "reality"
    assert vless["realitySettings"]["serverNames"] == ["www.apple.com"]
    assert vless["realitySettings"]["dest"] == "www.apple.com:443"
    assert vless["realitySettings"]["spiderX"] == "/"
    ws = by_tag["in-vmess"]["streamSettings"]
    assert ws["network"] == "ws" and ws["security"] == "none"
    assert ws["wsSettings"]["path"] == "/"
    assert ws["wsSettings"]["headers"]["Host"] == PUBLIC_HOST


def test_stream_settings_matrix():
    reality = _reality_settings()
    tls = {"transport": "tcp", "security": "tls",
           "cert_file": "/c/cert.pem", "key_file": "/c/key.pem"}
    cases = [
        ("tcp-none", {"transport": "tcp", "security": "none"},
         {"network": "tcp", "security": "none"}),
        ("tcp-tls", tls,
         {"network": "tcp", "security": "tls",
          "tlsSettings": {"serverName": PUBLIC_HOST,
                          "certificates": [{"certificateFile": "/c/cert.pem",
                                            "privateKeyFile": "/c/key.pem"}]}}),
        ("tcp-reality", reality,
         {"network": "tcp", "security": "reality",
          "realitySettings": {"dest": "www.apple.com:443",
                              "serverNames": ["www.apple.com"],
                              "privateKey": reality["private_key"],
                              "shortIds": ["abcd1234"], "spiderX": "/"}}),
        ("ws-none", {"transport": "ws", "security": "none", "path": "/p"},
         {"network": "ws", "security": "none",
          "wsSettings": {"path": "/p", "headers": {"Host": PUBLIC_HOST}}}),
        ("ws-tls-host", {"transport": "ws", "security": "tls", "path": "/p",
                         "host": "cdn.example.com",
                         "cert_file": "/c/cert.pem", "key_file": "/c/key.pem"},
         {"network": "ws", "security": "tls",
          "wsSettings": {"path": "/p", "headers": {"Host": "cdn.example.com"}},
          "tlsSettings": {"serverName": PUBLIC_HOST,
                          "certificates": [{"certificateFile": "/c/cert.pem",
                                            "privateKeyFile": "/c/key.pem"}]}}),
        ("grpc-none", {"transport": "grpc", "security": "none",
                       "serviceName": "svc1"},
         {"network": "grpc", "security": "none",
          "grpcSettings": {"serviceName": "svc1"}}),
        ("xhttp-none", {"transport": "xhttp", "security": "none",
                        "path": "/x", "mode": "packet-up"},
         {"network": "xhttp", "security": "none",
          "xhttpSettings": {"path": "/x", "mode": "packet-up"}}),
        ("xhttp-default-mode", {"transport": "xhttp", "security": "none"},
         {"network": "xhttp", "security": "none",
          "xhttpSettings": {"path": "/", "mode": "auto"}}),
        ("tls-server-name-override",
         {"transport": "tcp", "security": "tls", "server_name": "sni.example.com",
          "cert_file": "/c/cert.pem", "key_file": "/c/key.pem"},
         {"network": "tcp", "security": "tls",
          "tlsSettings": {"serverName": "sni.example.com",
                          "certificates": [{"certificateFile": "/c/cert.pem",
                                            "privateKeyFile": "/c/key.pem"}]}}),
    ]
    for name, settings, expected in cases:
        row = _inbound("vless", settings=settings)
        cfg = build_config([row], [], PUBLIC_HOST)
        stream = cfg["inbounds"][0]["streamSettings"]
        assert stream == expected, f"case {name}: {stream}"


def test_stream_settings_extra_merged_last():
    settings = {
        "transport": "ws", "security": "tls", "path": "/p",
        "cert_file": "/c/cert.pem", "key_file": "/c/key.pem",
        "stream_settings_extra": {
            "tlsSettings": {"serverName": "extra.example.com"},
            "sockopt": {"tcpFastOpen": True},
        },
    }
    cfg = build_config([_inbound("vless", settings=settings)], [], PUBLIC_HOST)
    stream = cfg["inbounds"][0]["streamSettings"]
    assert stream["tlsSettings"]["serverName"] == "extra.example.com"
    # deep merge: structured keys not named in extra survive
    assert stream["tlsSettings"]["certificates"][0]["certificateFile"] == (
        "/c/cert.pem"
    )
    assert stream["sockopt"] == {"tcpFastOpen": True}
    assert stream["wsSettings"]["path"] == "/p"


def test_users_grouped_by_inbound():
    rows = [
        _inbound("vless", inbound_id="in-a", port=443,
                 settings=_reality_settings()),
        _inbound("vless", inbound_id="in-b", port=10005),
    ]
    user_a = _user("vless", "in-a", user_id="alice")
    user_b = _user("vless", "in-b", user_id="bob")
    orphan = _user("vless", "deleted-in", user_id="orphan")
    cfg = build_config(rows, [user_a, user_b, orphan], PUBLIC_HOST)
    by_tag = {i["tag"]: i for i in cfg["inbounds"]}
    clients_a = by_tag["in-in-a"]["settings"]["clients"]
    clients_b = by_tag["in-in-b"]["settings"]["clients"]
    assert [c["email"] for c in clients_a] == ["alice"]
    assert [c["email"] for c in clients_b] == ["bob"]
    # a user pointing at a nonexistent inbound renders nowhere (never crash)
    assert all("orphan" not in json.dumps(i) for i in cfg["inbounds"])


def test_vless_reality_client_flow():
    row = _inbound("vless", settings=_reality_settings())
    user = _user("vless", row["id"])
    cfg = build_config([row], [user], PUBLIC_HOST)
    client = cfg["inbounds"][0]["settings"]["clients"][0]
    assert client["flow"] == "xtls-rprx-vision"
    assert client["id"] == user["uuid"]
    # flow never leaks onto other protocols
    vmess_row = _inbound("vmess", inbound_id="vm-in")
    cfg2 = build_config([vmess_row], [_user("vmess", "vm-in")], PUBLIC_HOST)
    assert "flow" not in json.dumps(cfg2["inbounds"][0]["settings"])


def test_clients_have_email_and_level():
    row = _inbound("vless", inbound_id="in-a")
    users = [_user("vless", "in-a", user_id=f"u{i}") for i in range(3)]
    cfg = build_config([row], users, PUBLIC_HOST)
    clients = cfg["inbounds"][0]["settings"]["clients"]
    assert len(clients) == 3
    for client in clients:
        assert client["email"] in {u["id"] for u in users}
        assert client["level"] == 0


def test_disabled_users_excluded_from_clients():
    row = _inbound("vless", inbound_id="in-a")
    enabled = _user("vless", "in-a", user_id="enabled-uid")
    disabled = _user("vless", "in-a", user_id="disabled-uid", enabled=0)
    cfg = build_config([row], [enabled, disabled], PUBLIC_HOST)
    serialized = json.dumps(cfg)
    assert disabled["uuid"] not in serialized
    assert enabled["uuid"] in serialized


def test_alterid_absent_everywhere():
    row = _inbound("vmess", inbound_id="vm-in")
    cfg = build_config([row], [_user("vmess", "vm-in")], PUBLIC_HOST)
    serialized = json.dumps(cfg)
    assert "alterId" not in serialized


def test_builder_ss_fallback_uses_shared_constant():
    row = _inbound("ss", inbound_id="ss-in")
    user = _user("ss", "ss-in")
    user["ss_method"] = None  # NULL in db -> builder must use the shared default
    cfg = build_config([row], [user], PUBLIC_HOST)
    client = cfg["inbounds"][0]["settings"]["clients"][0]
    assert client["method"] == DEFAULT_SS_METHOD


def test_schema_default_matches_shared_constant():
    assert f"DEFAULT '{DEFAULT_SS_METHOD}'" in db._SCHEMA


def test_api_and_stats_configured():
    row = _inbound("vless", inbound_id="in-a")
    cfg = build_config([row], [], PUBLIC_HOST)
    assert cfg["log"] == {"loglevel": "error"}
    assert cfg["stats"] == {}
    assert cfg["policy"]["levels"]["0"] == {
        "statsUserUplink": True, "statsUserDownlink": True,
    }
    assert cfg["policy"]["system"] == {
        "statsInboundUplink": True, "statsInboundDownlink": True,
    }
    assert cfg["api"]["services"] == ["StatsService"]
    api_inbounds = [i for i in cfg["inbounds"] if i["protocol"] == "dokodemo-door"]
    assert api_inbounds == [
        {
            "listen": "127.0.0.1",
            "port": PORTS["stats"],
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
            "tag": "api",
        }
    ]
    assert PORTS["stats"] == 10010
    api_rules = [r for r in cfg["routing"]["rules"] if r.get("inboundTag") == ["api"]]
    # AMENDED 2026-10-14: no "protocol" condition — Xray 26 has no "api"
    # protocol sniffer, so "protocol": ["api"] never matched and api traffic
    # looped through freedom (StatsService unreachable; real-binary probe).
    assert api_rules == [{"type": "field", "inboundTag": ["api"],
                          "outboundTag": "api"}]
    assert all("protocol" not in r for r in api_rules)
    # the api outbound is auto-built from api.tag; only freedom is explicit
    assert cfg["outbounds"] == [{"protocol": "freedom"}]


def test_config_json_serializable(tmp_path):
    rows = [_inbound(p, inbound_id=f"in-{p}") for p in
            ("vless", "vmess", "trojan", "ss")]
    cfg = build_config(rows, [], PUBLIC_HOST)
    round_tripped = json.loads(json.dumps(cfg))
    assert round_tripped == cfg
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    assert json.loads(cfg_path.read_text()) == cfg
    assert cfg_path.read_text().strip().startswith("{")


def _xray_test_cases():
    """(id, protocol, settings) the real binary must accept."""
    tls = {"transport": "tcp", "security": "tls",
           "cert_file": str(FIXTURES / "test_cert.pem"),
           "key_file": str(FIXTURES / "test_key.pem")}
    return [
        ("vless-tcp-none", "vless", {"transport": "tcp", "security": "none"}),
        ("vless-tcp-reality", "vless", _reality_settings()),
        ("vless-ws-tls", "vless", {**tls, "transport": "ws", "path": "/"}),
        ("vless-grpc-tls", "vless",
         {**tls, "transport": "grpc", "serviceName": "svc"}),
        ("vless-xhttp-none", "vless",
         {"transport": "xhttp", "security": "none", "path": "/", "mode": "auto"}),
        ("vmess-ws-none", "vmess",
         {"transport": "ws", "security": "none", "path": "/"}),
        ("trojan-tcp-tls", "trojan", dict(tls)),
        ("ss-tcp-none", "ss", {"transport": "tcp", "security": "none"}),
        ("ss-ws-none", "ss", {"transport": "ws", "security": "none", "path": "/"}),
    ]


@pytest.mark.skipif(
    XRAY_BIN is None,
    reason="xray binary not found (run: bash scripts/fetch_xray.sh)",
)
@pytest.mark.parametrize("case", _xray_test_cases(), ids=lambda c: c[0])
def test_config_passes_real_xray_test(tmp_path, case):
    _, protocol, settings = case
    row = _inbound(protocol, settings=settings)
    user = _user(protocol, row["id"]) if protocol != "ss" else _user("ss", row["id"])
    cfg = build_config([row], [user], PUBLIC_HOST)
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    result = subprocess.run(
        [XRAY_BIN, "run", "-test", "-c", str(cfg_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"xray rejected config:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(
    XRAY_BIN is None,
    reason="xray binary not found (run: bash scripts/fetch_xray.sh)",
)
def test_empty_client_inbounds_pass_real_xray(tmp_path):
    """Fresh-install boot: every seed inbound renders with zero users."""
    rows = [
        _inbound("vless", inbound_id="vless", port=443,
                 settings=_reality_settings()),
        _inbound("vmess", inbound_id="vmess", port=10002,
                 settings={"transport": "ws", "security": "none", "path": "/"}),
        _inbound("trojan", inbound_id="trojan", port=10003),
        _inbound("ss", inbound_id="ss", port=10004),
    ]
    cfg = build_config(rows, [], PUBLIC_HOST)
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    result = subprocess.run(
        [XRAY_BIN, "run", "-test", "-c", str(cfg_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"xray rejected config:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(
    XRAY_BIN is None,
    reason="xray binary not found (run: bash scripts/fetch_xray.sh)",
)
def test_combined_config_passes_real_xray_test(tmp_path):
    """The shipped shape — all 4 protocols + disabled users — accepted by real xray."""
    rows = [
        _inbound("vless", inbound_id="vless", port=443,
                 settings=_reality_settings()),
        _inbound("vmess", inbound_id="vmess", port=10002,
                 settings={"transport": "ws", "security": "none", "path": "/"}),
        _inbound("trojan", inbound_id="trojan", port=10003),
        _inbound("ss", inbound_id="ss", port=10004),
    ]
    users = []
    for protocol in ("vless", "vmess", "trojan", "ss"):
        users.append(_user(protocol, protocol, user_id=f"{protocol}-enabled"))
        users.append(_user(protocol, protocol, user_id=f"{protocol}-disabled",
                            enabled=0))
    cfg = build_config(rows, users, PUBLIC_HOST)
    serialized = json.dumps(cfg)
    for user in users:
        secret = (
            user["uuid"] if user["protocol"] in ("vless", "vmess")
            else user["credential"]
        )
        if user["enabled"]:
            assert secret in serialized
        else:
            assert user["uuid"] not in serialized
            assert user["credential"] not in serialized
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    result = subprocess.run(
        [XRAY_BIN, "run", "-test", "-c", str(cfg_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"xray rejected config:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(
    not (REPO_ROOT / ".bin" / "xray").exists(),
    reason="repo .bin/xray not fetched (run: bash scripts/fetch_xray.sh)",
)
def test_find_xray_resolves_from_repo_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("XRAY_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.chdir(tmp_path)  # CWD no longer contains .bin/xray
    found = _find_xray()
    assert found is not None
    resolved = Path(found)
    assert resolved.is_absolute()
    assert REPO_ROOT in resolved.parents
