"""Tests for panel.links — share links whose port/security come from the inbound."""

import base64
import json
from urllib.parse import quote, urlparse

import pytest

from panel.links import build_link, build_qr_data_uri, build_vmess_obj

PUBLIC_HOST = "fakhra.railway.app"
UUID = "3f0a8f6e-1111-4222-8333-444455556666"

REALITY = {
    "transport": "tcp",
    "security": "reality",
    "server_names": ["www.apple.com"],
    "dest": "www.apple.com:443",
    "private_key": "PRIVKEY",
    "public_key": "PUBKEY-VALUE",
    "short_ids": ["abcd1234"],
    "fp": "chrome",
    "flow": "xtls-rprx-vision",
}


def _user(protocol: str) -> dict:
    """A user row in the shape db.list_users() returns."""
    return {
        "id": f"{protocol}-uid",
        "username": f"{protocol}-user",
        "password": "hash",
        "protocol": protocol,
        "uuid": UUID,
        "credential": "s3cr3t-pass",
        "ss_method": None,  # NULL in db -> falls back to DEFAULT_SS_METHOD
        "inbound_id": f"{protocol}-in",
    }


def _inbound(protocol: str = "vless", *, port: int = 443, settings=None) -> dict:
    """An inbound row; settings passed as dict OR db-style JSON text."""
    if settings is None:
        settings = {"transport": "tcp", "security": "none"}
    return {
        "id": f"{protocol}-in",
        "protocol": protocol,
        "listen": "0.0.0.0",
        "port": port,
        "settings": settings,
        "enabled": 1,
    }


def test_link_matrix_reality_exact_fields():
    """Full exact pin: every REALITY link field (pbk/sni/sid/flow/type)."""
    link = build_link(_user("vless"), _inbound(port=8443, settings=REALITY),
                      PUBLIC_HOST)
    assert link == (
        f"vless://{UUID}@{PUBLIC_HOST}:8443"
        "?security=reality&sni=www.apple.com&fp=chrome&pbk=PUBKEY-VALUE"
        "&sid=abcd1234&flow=xtls-rprx-vision&type=tcp"
        "#vless-user"
    )


def test_link_matrix_ws_tls():
    inbound = _inbound("trojan", port=443, settings={
        "transport": "ws", "security": "tls", "path": "/ray",
        "cert_file": "/c/cert.pem", "key_file": "/c/key.pem",
    })
    link = build_link(_user("trojan"), inbound, PUBLIC_HOST)
    assert link == (
        f"trojan://s3cr3t-pass@{PUBLIC_HOST}:443"
        f"?security=tls&sni={PUBLIC_HOST}&type=ws"
        f"&host={PUBLIC_HOST}&path=%2Fray#trojan-user"
    )


def test_link_matrix_grpc_service_name():
    inbound = _inbound(port=10001, settings={
        "transport": "grpc", "security": "none", "serviceName": "svc9",
    })
    link = build_link(_user("vless"), inbound, PUBLIC_HOST)
    assert link == (
        f"vless://{UUID}@{PUBLIC_HOST}:10001"
        "?security=none&type=grpc&serviceName=svc9#vless-user"
    )


def test_link_matrix_xhttp_mode():
    inbound = _inbound(port=10005, settings={
        "transport": "xhttp", "security": "none",
        "path": "/x", "mode": "packet-up",
    })
    link = build_link(_user("vless"), inbound, PUBLIC_HOST)
    assert link == (
        f"vless://{UUID}@{PUBLIC_HOST}:10005"
        "?security=none&type=xhttp&path=%2Fx&mode=packet-up#vless-user"
    )


def test_link_matrix_trojan_plain_no_query():
    inbound = _inbound("trojan", port=10003,
                       settings={"transport": "tcp", "security": "none"})
    link = build_link(_user("trojan"), inbound, PUBLIC_HOST)
    assert link == f"trojan://s3cr3t-pass@{PUBLIC_HOST}:10003#trojan-user"
    assert "?" not in link  # tcp+none: NO query at all


def test_link_matrix_ss_tcp_and_ws():
    userinfo = base64.urlsafe_b64encode(
        b"aes-256-gcm:s3cr3t-pass"
    ).decode()
    plain = build_link(
        _user("ss"),
        _inbound("ss", port=10004, settings={"transport": "tcp",
                                             "security": "none"}),
        PUBLIC_HOST,
    )
    assert plain == f"ss://{userinfo}@{PUBLIC_HOST}:10004#ss-user"
    assert "?" not in plain

    ws = build_link(
        _user("ss"),
        _inbound("ss", port=20001, settings={"transport": "ws",
                                             "security": "none", "path": "/"}),
        PUBLIC_HOST,
    )
    assert ws == (
        f"ss://{userinfo}@{PUBLIC_HOST}:20001"
        f"?security=none&type=ws&host={PUBLIC_HOST}&path=%2F#ss-user"
    )


def test_link_parses_settings_json_text():
    """db rows carry settings as JSON text — same link as the dict form."""
    inbound = _inbound(port=8443, settings=json.dumps(REALITY))
    link = build_link(_user("vless"), inbound, PUBLIC_HOST)
    assert link == build_link(_user("vless"), _inbound(port=8443,
                                                       settings=REALITY),
                              PUBLIC_HOST)


def test_vless_uri_parses_with_urllib():
    link = build_link(_user("vless"),
                      _inbound(port=8443, settings=REALITY), PUBLIC_HOST)
    parsed = urlparse(link)
    assert parsed.scheme == "vless"
    assert parsed.hostname == PUBLIC_HOST
    assert parsed.username == UUID
    assert parsed.port == 8443


def test_ss_credentials_roundtrip():
    link = build_link(_user("ss"),
                      _inbound("ss", port=10004), PUBLIC_HOST)
    userinfo = link.split("://", 1)[1].split("@", 1)[0]
    decoded = base64.urlsafe_b64decode(userinfo).decode()
    assert decoded == "aes-256-gcm:s3cr3t-pass"  # NULL ss_method -> shared default


def test_qr_is_png_data_uri():
    link = build_link(_user("vless"),
                      _inbound(port=8443, settings=REALITY), PUBLIC_HOST)
    uri = build_qr_data_uri(link)
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw.startswith(b"\x89PNG")


def test_vmess_obj_exact_fields():
    user = _user("vmess")
    inbound = _inbound("vmess", port=443, settings={
        "transport": "ws", "security": "tls", "path": "/ray",
        "cert_file": "/c/cert.pem", "key_file": "/c/key.pem",
    })
    obj = build_vmess_obj(user, inbound, PUBLIC_HOST)
    assert obj == {
        "v": "2",
        "ps": "vmess-user",
        "add": PUBLIC_HOST,
        "port": "443",
        "id": UUID,
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": PUBLIC_HOST,
        "path": "/ray",
        "tls": "tls",
        "sni": PUBLIC_HOST,
    }


def test_vmess_obj_tcp_none():
    obj = build_vmess_obj(_user("vmess"),
                          _inbound("vmess", port=10002), PUBLIC_HOST)
    assert obj["net"] == "tcp"
    assert obj["port"] == "10002"
    assert obj["tls"] == ""  # security=none -> no tls, no sni
    assert "sni" not in obj
    assert "path" not in obj and "host" not in obj


def test_vmess_link_is_base64_json():
    inbound = _inbound("vmess", port=10002)
    link = build_link(_user("vmess"), inbound, PUBLIC_HOST)
    assert link.startswith("vmess://")
    obj = json.loads(base64.b64decode(link[len("vmess://"):]))
    assert obj == build_vmess_obj(_user("vmess"), inbound, PUBLIC_HOST)


@pytest.mark.parametrize("public_host", ["", "   "])
def test_blank_public_host_raises_valueerror(public_host):
    """No silent '@:443' links may ever be produced."""
    with pytest.raises(ValueError):
        build_link(_user("vless"), _inbound(), public_host)
