"""Client share links (vless://, vmess://, trojan://, ss://) and QR PNG data URIs.

Every link derives its port, transport, and security FROM the inbound row the
user belongs to: REALITY links carry sni/pbk/sid/flow, plain tcp+none links
carry no query at all. There is no install token — links never hardcode
anything the inbound already knows.
"""

import base64
import io
import json
from urllib.parse import quote

import qrcode

from panel.constants import DEFAULT_SS_METHOD


def _parsed_settings(inbound: dict) -> dict:
    settings = inbound.get("settings") or {}
    if isinstance(settings, str):
        settings = json.loads(settings) if settings else {}
    return settings


def _security_parts(security: str, settings: dict, public_host: str) -> list[str]:
    if security == "reality":
        return [
            "security=reality",
            f"sni={settings['server_names'][0]}",
            f"fp={settings.get('fp', 'chrome')}",
            f"pbk={settings['public_key']}",
            f"sid={settings['short_ids'][0]}",
            f"flow={settings.get('flow', 'xtls-rprx-vision')}",
        ]
    if security == "tls":
        return ["security=tls", f"sni={settings.get('server_name') or public_host}"]
    return ["security=none"]


def _transport_parts(transport: str, settings: dict, public_host: str) -> list[str]:
    parts = [f"type={transport}"]
    if transport == "ws":
        parts.append(f"host={settings.get('host') or public_host}")
        parts.append(f"path={quote(settings.get('path') or '/', safe='')}")
    elif transport == "grpc":
        parts.append(
            f"serviceName={quote(settings.get('serviceName') or '', safe='')}"
        )
    elif transport == "xhttp":
        parts.append(f"path={quote(settings.get('path') or '/', safe='')}")
        parts.append(f"mode={settings.get('mode') or 'auto'}")
    return parts


def _query(settings: dict, public_host: str) -> str:
    """The inbound's full query string; plain tcp+none gets NONE."""
    transport = settings.get("transport", "tcp")
    security = settings.get("security", "none")
    if transport == "tcp" and security == "none":
        return ""
    parts = _security_parts(security, settings, public_host) + _transport_parts(
        transport, settings, public_host
    )
    return "?" + "&".join(parts)


def build_link(user: dict, inbound: dict, public_host: str) -> str:
    """Build the standard client share URI for a user on a given inbound.

    Raises ValueError when public_host is empty or blank — a link without a
    host is unusable and must never be produced silently.
    """
    if not public_host or not public_host.strip():
        raise ValueError("public_host must not be empty or blank")

    protocol = user["protocol"]
    settings = _parsed_settings(inbound)
    port = inbound["port"]
    query = _query(settings, public_host)
    fragment = quote(user["username"])

    if protocol == "vmess":
        payload = base64.b64encode(
            json.dumps(
                build_vmess_obj(user, inbound, public_host), separators=(",", ":")
            ).encode()
        ).decode()
        return "vmess://" + payload
    if protocol == "vless":
        return f"vless://{user['uuid']}@{public_host}:{port}{query}#{fragment}"
    if protocol == "trojan":
        return f"trojan://{user['credential']}@{public_host}:{port}{query}#{fragment}"
    if protocol == "ss":
        method = user["ss_method"] or DEFAULT_SS_METHOD
        userinfo = base64.urlsafe_b64encode(
            f"{method}:{user['credential']}".encode()
        ).decode()
        return f"ss://{userinfo}@{public_host}:{port}{query}#{fragment}"
    raise ValueError(f"unsupported protocol: {protocol!r}")


def build_vmess_obj(user: dict, inbound: dict, public_host: str) -> dict:
    """The JSON body inside a vmess:// link. aid is "0": Xray is AEAD-only."""
    settings = _parsed_settings(inbound)
    transport = settings.get("transport", "tcp")
    security = settings.get("security", "none")
    obj: dict = {
        "v": "2",
        "ps": user["username"],
        "add": public_host,
        "port": str(inbound["port"]),
        "id": user["uuid"],
        "aid": "0",
        "net": transport,
        "type": "none",
    }
    if transport == "ws":
        obj["host"] = settings.get("host") or public_host
        obj["path"] = settings.get("path") or "/"
    elif transport == "grpc":
        obj["serviceName"] = settings.get("serviceName") or ""
    elif transport == "xhttp":
        obj["path"] = settings.get("path") or "/"
    if security == "tls":
        obj["tls"] = "tls"
        obj["sni"] = settings.get("server_name") or public_host
    elif security == "reality":  # unreachable for vmess (validation blocks it)
        obj["tls"] = "reality"
        obj["sni"] = settings["server_names"][0]
        obj["pbk"] = settings["public_key"]
        obj["sid"] = settings["short_ids"][0]
        obj["fp"] = settings.get("fp", "chrome")
    else:
        obj["tls"] = ""
    return obj


def build_qr_data_uri(uri: str) -> str:
    """Render a share link as a PNG QR code data URI (data:image/png;base64,...)."""
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/png;base64," + encoded
