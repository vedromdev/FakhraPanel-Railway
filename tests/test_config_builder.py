"""Tests for panel.paths and the Xray config builder (4 protocols over ws)."""

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
from panel.xray.config_builder import build_config, write_config

INSTALL_TOKEN = "tok-abc123"
PUBLIC_HOST = "fakhra.railway.app"
PROTOCOLS = ["vless", "vmess", "trojan", "ss"]
REPO_ROOT = Path(__file__).resolve().parent.parent


def _user(protocol: str, *, user_id: str | None = None, enabled: int = 1) -> dict:
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


def test_one_inbound_per_enabled_protocol():
    users = [
        _user("vless", enabled=1),
        _user("vmess", enabled=0),
        _user("ss", enabled=1),
    ]
    cfg = build_config(users, INSTALL_TOKEN, PUBLIC_HOST)
    proto_tags = {i["tag"] for i in cfg["inbounds"] if i["tag"].startswith("in-")}
    assert proto_tags == {"in-vless", "in-ss"}
    ports = {i["tag"]: i["port"] for i in cfg["inbounds"]}
    assert ports["in-vless"] == PORTS["vless"]
    assert ports["in-ss"] == PORTS["ss"]


def test_all_four_protocols_and_five_inbounds():
    users = [_user(p) for p in PROTOCOLS]
    cfg = build_config(users, INSTALL_TOKEN, PUBLIC_HOST)
    assert len(cfg["inbounds"]) == 5  # 4 protocol listeners + the api inbound
    by_tag = {i["tag"]: i for i in cfg["inbounds"]}
    xray_proto = {"vless": "vless", "vmess": "vmess", "trojan": "trojan", "ss": "shadowsocks"}
    for protocol in PROTOCOLS:
        inbound = by_tag[f"in-{protocol}"]
        assert inbound["protocol"] == xray_proto[protocol]
        assert inbound["port"] == PORTS[protocol]
        assert inbound["listen"] == "127.0.0.1"
        ws = inbound["streamSettings"]["wsSettings"]
        assert ws["path"] == vpn_path(INSTALL_TOKEN, protocol)
        assert ws["headers"]["Host"] == PUBLIC_HOST
        assert inbound["streamSettings"]["network"] == "ws"
        assert inbound["streamSettings"]["security"] == "none"
        assert inbound["sniffing"]["enabled"] is True
    # when no protocol has an enabled user, its inbound is omitted entirely
    cfg2 = build_config([_user("trojan")], INSTALL_TOKEN, PUBLIC_HOST)
    assert {i["tag"] for i in cfg2["inbounds"]} == {"in-trojan", "api"}


def test_clients_have_email_and_level():
    users = [_user(p) for p in PROTOCOLS]
    cfg = build_config(users, INSTALL_TOKEN, PUBLIC_HOST)
    expected_ids = {u["id"] for u in users}
    clients = [
        c
        for i in cfg["inbounds"]
        for c in i.get("settings", {}).get("clients", [])
    ]
    assert len(clients) == len(users)
    for client in clients:
        assert client["email"] in expected_ids
        assert client["level"] == 0


def test_disabled_users_excluded_from_clients():
    enabled = _user("vless", user_id="enabled-uid")
    disabled = _user("vless", user_id="disabled-uid", enabled=0)
    cfg = build_config([enabled, disabled], INSTALL_TOKEN, PUBLIC_HOST)
    serialized = json.dumps(cfg)
    assert disabled["uuid"] not in serialized
    assert enabled["uuid"] in serialized


def test_alterid_absent_everywhere():
    cfg = build_config([_user("vmess")], INSTALL_TOKEN, PUBLIC_HOST)
    serialized = json.dumps(cfg)
    assert "alterId" not in serialized


def test_builder_ss_fallback_uses_shared_constant():
    user = _user("ss")
    user["ss_method"] = None  # NULL in db -> builder must use the shared default
    cfg = build_config([user], INSTALL_TOKEN, PUBLIC_HOST)
    client = cfg["inbounds"][0]["settings"]["clients"][0]
    assert client["method"] == DEFAULT_SS_METHOD


def test_schema_default_matches_shared_constant():
    assert f"DEFAULT '{DEFAULT_SS_METHOD}'" in db._SCHEMA


def test_api_and_stats_configured():
    cfg = build_config([_user("vless")], INSTALL_TOKEN, PUBLIC_HOST)
    assert cfg["log"] == {"loglevel": "error"}
    assert cfg["stats"] == {}
    assert cfg["policy"]["levels"]["0"] == {
        "statsUserUplink": True,
        "statsUserDownlink": True,
    }
    assert cfg["policy"]["system"] == {
        "statsInboundUplink": True,
        "statsInboundDownlink": True,
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
    cfg = build_config([_user(p) for p in PROTOCOLS], INSTALL_TOKEN, PUBLIC_HOST)
    round_tripped = json.loads(json.dumps(cfg))
    assert round_tripped == cfg
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    assert json.loads(cfg_path.read_text()) == cfg
    assert cfg_path.read_text().strip().startswith("{")


@pytest.mark.skipif(
    XRAY_BIN is None,
    reason="xray binary not found (run: bash scripts/fetch_xray.sh)",
)
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_config_passes_real_xray_test(tmp_path, protocol):
    cfg = build_config([_user(protocol)], INSTALL_TOKEN, PUBLIC_HOST)
    cfg_path = tmp_path / "xray.json"
    write_config(cfg_path, cfg)
    result = subprocess.run(
        [XRAY_BIN, "run", "-test", "-c", str(cfg_path)],
        capture_output=True,
        text=True,
        timeout=60,
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
    users = []
    for protocol in PROTOCOLS:
        users.append(_user(protocol, user_id=f"{protocol}-enabled"))
        users.append(_user(protocol, user_id=f"{protocol}-disabled", enabled=0))
    cfg = build_config(users, INSTALL_TOKEN, PUBLIC_HOST)
    serialized = json.dumps(cfg)
    for user in users:
        # vless/vmess configs carry the uuid; trojan/ss configs carry the credential
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
        capture_output=True,
        text=True,
        timeout=60,
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
