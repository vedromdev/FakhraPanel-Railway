"""Tests for panel.links — share links (4 URI schemes) and QR PNG data URIs."""

import base64
import json
from urllib.parse import quote, urlparse

import pytest

from panel.links import build_link, build_qr_data_uri, build_vmess_obj
from panel.paths import vpn_path

INSTALL_TOKEN = "tok-abc123"
PUBLIC_HOST = "fakhra.railway.app"
PROTOCOLS = ["vless", "vmess", "trojan", "ss"]


def _user(protocol: str) -> dict:
    """A user row in the shape db.list_users() returns."""
    return {
        "id": f"{protocol}-uid",
        "username": f"{protocol}-user",
        "password": "hash",
        "protocol": protocol,
        "uuid": "3f0a8f6e-1111-4222-8333-444455556666",
        "credential": "s3cr3t-pass",
        "ss_method": None,  # NULL in db -> must fall back to DEFAULT_SS_METHOD
    }


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_link_fields_pinned_all_protocols(protocol):
    user = _user(protocol)
    link = build_link(user, PUBLIC_HOST, INSTALL_TOKEN)
    path = vpn_path(INSTALL_TOKEN, protocol)
    if protocol == "vmess":
        # vmess packs everything into one base64 JSON blob
        assert link.startswith("vmess://")
        obj = json.loads(base64.b64decode(link[len("vmess://"):]))
        assert obj["port"] == "443"
        assert obj["tls"] == "tls"
        assert obj["net"] == "ws"
        assert obj["id"] == user["uuid"]
        assert obj["path"] == path
        assert obj["sni"] == PUBLIC_HOST
        assert obj["host"] == PUBLIC_HOST
        assert obj["ps"] == user["username"]
        assert obj["aid"] == "0"  # Xray is AEAD-only: alterId must be 0
        assert set(obj) == {
            "v", "ps", "add", "port", "id", "aid", "net", "type", "host",
            "path", "tls", "sni",
        }
    else:
        assert ":443" in link
        assert "security=tls" in link
        assert "sni=fakhra.railway.app" in link
        assert "type=ws" in link
        assert quote(path, safe="") in link  # percent-encoded /ray-<token>/<proto>
        assert link.endswith("#" + quote(user["username"]))


def test_vless_uri_parses_with_urllib():
    user = _user("vless")
    link = build_link(user, PUBLIC_HOST, INSTALL_TOKEN)
    parsed = urlparse(link)
    assert parsed.scheme == "vless"
    assert parsed.hostname == "fakhra.railway.app"
    assert parsed.username == user["uuid"]


def test_ss_credentials_roundtrip():
    user = _user("ss")
    link = build_link(user, PUBLIC_HOST, INSTALL_TOKEN)
    userinfo = link.split("://", 1)[1].split("@", 1)[0]
    decoded = base64.urlsafe_b64decode(userinfo).decode()
    assert decoded == f"aes-256-gcm:{user['credential']}"  # NULL ss_method -> shared default


def test_qr_is_png_data_uri():
    link = build_link(_user("vless"), PUBLIC_HOST, INSTALL_TOKEN)
    uri = build_qr_data_uri(link)
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw.startswith(b"\x89PNG")


def test_vmess_obj_exact_fields():
    user = _user("vmess")
    obj = build_vmess_obj(user, PUBLIC_HOST, INSTALL_TOKEN)
    assert obj == {
        "v": "2",
        "ps": user["username"],
        "add": PUBLIC_HOST,
        "port": "443",
        "id": user["uuid"],
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": PUBLIC_HOST,
        "path": vpn_path(INSTALL_TOKEN, "vmess"),
        "tls": "tls",
        "sni": PUBLIC_HOST,
    }


@pytest.mark.parametrize("public_host", ["", "   "])
def test_blank_public_host_raises_valueerror(public_host):
    """No silent '@:443' links may ever be produced."""
    with pytest.raises(ValueError):
        build_link(_user("vless"), public_host, INSTALL_TOKEN)
