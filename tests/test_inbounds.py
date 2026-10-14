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
