"""Inbound domain rules shared by routes (the CRUD API grows in Task 5)."""

import pytest

from panel import inbounds


def _row(inbound_id, protocol, *, enabled=1, created_at="2026-01-01T00:00:00+00:00"):
    return {
        "id": inbound_id,
        "protocol": protocol,
        "enabled": enabled,
        "created_at": created_at,
    }


def test_resolve_default_is_oldest_enabled_matching_protocol():
    rows = [
        _row("v2", "vless", created_at="2026-02-01T00:00:00+00:00"),
        _row("v1", "vless", created_at="2026-01-01T00:00:00+00:00"),
        _row("vm", "vmess", created_at="2026-01-01T00:00:00+00:00"),
    ]
    assert inbounds.resolve_inbound_id("vless", None, rows) == "v1"


def test_resolve_requested_enabled_match():
    rows = [_row("v1", "vless"), _row("v2", "vless")]
    assert inbounds.resolve_inbound_id("vless", "v2", rows) == "v2"


def test_resolve_requested_unknown_or_wrong_protocol_raises():
    rows = [_row("vm", "vmess")]
    with pytest.raises(ValueError):
        inbounds.resolve_inbound_id("vless", "vm", rows)
    with pytest.raises(ValueError):
        inbounds.resolve_inbound_id("vless", "ghost", rows)


def test_resolve_all_disabled_raises():
    rows = [_row("v1", "vless", enabled=0)]
    with pytest.raises(ValueError, match="no enabled"):
        inbounds.resolve_inbound_id("vless", None, rows)


def test_resolve_never_picks_disabled_even_when_requested():
    rows = [_row("v1", "vless", enabled=0)]
    with pytest.raises(ValueError):
        inbounds.resolve_inbound_id("vless", "v1", rows)


# --- Task 5: settings validation, reserved ports, firewall hints ------------


def test_settings_normalize_defaults():
    out = inbounds.validate_settings("vless", {})
    assert out["transport"] == "tcp"
    assert out["security"] == "none"
    assert not out.get("flow")


def test_reality_settings_normalize_generated():
    out = inbounds.validate_settings(
        "vless",
        {"security": "reality",
         "server_names": ["www.apple.com"], "dest": "www.apple.com:443"},
    )
    assert out["private_key"] and out["public_key"]
    assert len(out["short_ids"]) == 1 and len(out["short_ids"][0]) == 8
    int(out["short_ids"][0], 16)
    assert out["fp"] == "chrome"
    assert out["flow"] == "xtls-rprx-vision"
    assert out["transport"] == "tcp"  # normalized, not stripped


def test_reality_public_key_derived_from_private():
    from panel.xray import keys

    priv, _ = keys.generate_keypair()
    out = inbounds.validate_settings(
        "vless",
        {"security": "reality", "private_key": priv,
         "server_names": ["www.apple.com"], "dest": "www.apple.com:443"},
    )
    assert out["public_key"] == keys.public_from_private(priv)


def test_reality_requires_server_names_and_dest():
    with pytest.raises(ValueError, match="server_names"):
        inbounds.validate_settings("vless", {"security": "reality"})


def test_reality_vless_only():
    with pytest.raises(ValueError, match="reality"):
        inbounds.validate_settings(
            "vmess",
            {"security": "reality",
             "server_names": ["a.com"], "dest": "a.com:443"},
        )


def test_flow_requires_vless_reality():
    with pytest.raises(ValueError, match="flow"):
        inbounds.validate_settings(
            "vless", {"security": "none", "flow": "xtls-rprx-vision"}
        )
    with pytest.raises(ValueError, match="flow"):
        inbounds.validate_settings("vmess", {"security": "none", "flow": "x"})


def test_tls_requires_cert_paths():
    with pytest.raises(ValueError, match="cert_file"):
        inbounds.validate_settings("vless", {"security": "tls"})


def test_bad_transport_or_security_rejected():
    with pytest.raises(ValueError, match="transport"):
        inbounds.validate_settings("vless", {"transport": "kcp"})
    with pytest.raises(ValueError, match="security"):
        inbounds.validate_settings("vless", {"security": "xtls"})


def test_reserved_and_pre_opened_ports():
    assert inbounds.RESERVED_PORTS == frozenset({80, 8080})
    assert inbounds.PRE_OPENED == frozenset({80, 443, 8080, 10002, 10003, 10004})


def test_firewall_hint():
    assert inbounds.firewall_hint(2048) == "ufw allow 2048/tcp"
    assert inbounds.firewall_hint(443) is None  # pre-opened by install.sh
    assert inbounds.firewall_hint(8080) is None
    assert inbounds.firewall_hint(10004) is None
