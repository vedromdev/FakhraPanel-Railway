"""Client share links (vless://, vmess://, trojan://, ss://) and QR PNG data URIs.

Every link points at the edge: PUBLIC_HOST:443, TLS-terminated, websocket
transport, per-install path from paths.vpn_path(). The install token is always
passed in by the caller — links never hardcode it.
"""

import base64
import io
import json
from urllib.parse import quote

import qrcode

from panel.constants import DEFAULT_SS_METHOD
from panel.paths import vpn_path

_PORT = "443"


def _stream_query(public_host: str, path: str, *, encryption: str | None = None) -> str:
    """Query string shared by every ws+TLS share link."""
    parts = []
    if encryption is not None:
        parts.append(f"encryption={encryption}")
    parts.extend(
        [
            "security=tls",
            f"sni={public_host}",
            "type=ws",
            f"host={public_host}",
            f"path={quote(path, safe='')}",
        ]
    )
    return "?" + "&".join(parts)


def build_link(user: dict, public_host: str, install_token: str) -> str:
    """Build the standard client share URI for a user.

    Raises ValueError when public_host is empty or blank — a link without a
    host is unusable and must never be produced silently.
    """
    if not public_host or not public_host.strip():
        raise ValueError("public_host must not be empty or blank")

    protocol = user["protocol"]
    path = vpn_path(install_token, protocol)
    fragment = quote(user["username"])

    if protocol == "vmess":
        obj = build_vmess_obj(user, public_host, install_token)
        payload = base64.b64encode(
            json.dumps(obj, separators=(",", ":")).encode()
        ).decode()
        return "vmess://" + payload
    if protocol == "vless":
        query = _stream_query(public_host, path, encryption="none")
        return f"vless://{user['uuid']}@{public_host}:{_PORT}{query}#{fragment}"
    if protocol == "trojan":
        query = _stream_query(public_host, path)
        return f"trojan://{user['credential']}@{public_host}:{_PORT}{query}#{fragment}"
    if protocol == "ss":
        method = user["ss_method"] or DEFAULT_SS_METHOD
        userinfo = base64.urlsafe_b64encode(
            f"{method}:{user['credential']}".encode()
        ).decode()
        query = _stream_query(public_host, path)
        return f"ss://{userinfo}@{public_host}:{_PORT}{query}#{fragment}"
    raise ValueError(f"unsupported protocol: {protocol!r}")


def build_vmess_obj(user: dict, public_host: str, install_token: str) -> dict:
    """The JSON body inside a vmess:// link. aid is "0": Xray is AEAD-only."""
    return {
        "v": "2",
        "ps": user["username"],
        "add": public_host,
        "port": _PORT,
        "id": user["uuid"],
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": public_host,
        "path": vpn_path(install_token, user["protocol"]),
        "tls": "tls",
        "sni": public_host,
    }


def build_qr_data_uri(uri: str) -> str:
    """Render a share link as a PNG QR code data URI (data:image/png;base64,...)."""
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/png;base64," + encoded
