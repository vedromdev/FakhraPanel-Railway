"""Build the full Xray config dict from inbound rows and their users.

Pure functions: plain arguments in, plain dicts out — no settings, env, or DB.
Every enabled inbound row becomes one xray inbound; users are grouped by their
`inbound_id`. Link-only settings keys (public_key, fp, server_name, ...) are
never rendered — the server config gets exactly what xray's schema wants.
"""

import json
from pathlib import Path

from panel.constants import DEFAULT_SS_METHOD
from panel.paths import PORTS

LISTEN = "127.0.0.1"

# db protocol name -> Xray inbound protocol name
_XRAY_PROTOCOL = {
    "vless": "vless",
    "vmess": "vmess",
    "trojan": "trojan",
    "ss": "shadowsocks",
}


def _client(protocol: str, user: dict, settings: dict) -> dict:
    client: dict = {"level": 0, "email": user["id"]}
    if protocol in ("vless", "vmess"):
        client["id"] = user["uuid"]
    if protocol == "vless" and settings.get("flow"):
        # REALITY+vision: flow is a per-client vless setting (not stream)
        client["flow"] = settings["flow"]
    elif protocol == "trojan":
        client["password"] = user["credential"]
    elif protocol == "ss":
        client["method"] = user["ss_method"] or DEFAULT_SS_METHOD
        client["password"] = user["credential"]
    return client


def _inbound_settings(protocol: str, users: list[dict], settings: dict) -> dict:
    result: dict = {"clients": [_client(protocol, u, settings) for u in users]}
    if protocol == "vless":
        result["decryption"] = "none"
    elif protocol == "ss":
        result["network"] = "tcp"
    return result


def _deep_merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _stream_settings(settings: dict, public_host: str) -> dict:
    transport = settings.get("transport", "tcp")
    security = settings.get("security", "none")
    stream: dict = {"network": transport, "security": security}
    if transport == "ws":
        stream["wsSettings"] = {
            "path": settings.get("path") or "/",
            "headers": {"Host": settings.get("host") or public_host},
        }
    elif transport == "grpc":
        stream["grpcSettings"] = {"serviceName": settings.get("serviceName", "")}
    elif transport == "xhttp":
        stream["xhttpSettings"] = {
            "path": settings.get("path") or "/",
            "mode": settings.get("mode", "auto"),
        }
    if security == "tls":
        stream["tlsSettings"] = {
            "serverName": settings.get("server_name") or public_host,
            "certificates": [
                {
                    "certificateFile": settings["cert_file"],
                    "privateKeyFile": settings["key_file"],
                }
            ],
        }
    elif security == "reality":
        stream["realitySettings"] = {
            "dest": settings["dest"],
            "serverNames": settings["server_names"],
            "privateKey": settings["private_key"],
            "shortIds": settings.get("short_ids", [""]),
            "spiderX": settings.get("spider_x", "/"),
        }
    extra = settings.get("stream_settings_extra")
    if extra:
        stream = _deep_merge(stream, extra)
    return stream


def _xray_inbound(row: dict, users: list[dict], public_host: str) -> dict:
    settings = row["settings"]
    if isinstance(settings, str):
        settings = json.loads(settings) if settings else {}
    protocol = row["protocol"]
    return {
        "tag": f"in-{row['id']}",
        "listen": row.get("listen") or LISTEN,
        "port": row["port"],
        "protocol": _XRAY_PROTOCOL[protocol],
        "settings": _inbound_settings(protocol, users, settings),
        "streamSettings": _stream_settings(settings, public_host),
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
    }


def build_config(inbounds: list[dict], users: list[dict], public_host: str) -> dict:
    """Full Xray config: one inbound per enabled row + the stats api listener.

    Enabled inbounds render even with zero users (fresh-install boot; probed
    against real xray — empty clients are accepted for all four protocols).
    """
    assigned: dict[str | None, list[dict]] = {}
    for user in users:
        if user.get("enabled", 1):
            assigned.setdefault(user.get("inbound_id"), []).append(user)

    rendered = [
        _xray_inbound(row, assigned.get(row["id"], []), public_host)
        for row in inbounds
        if row.get("enabled", 1)
    ]
    # StatsService api access: dokodemo-door redirecting to the api outbound
    rendered.append(
        {
            "listen": LISTEN,
            "port": PORTS["stats"],
            "protocol": "dokodemo-door",
            "settings": {"address": LISTEN},
            "tag": "api",
        }
    )

    return {
        "log": {"loglevel": "error"},
        "stats": {},
        "policy": {
            "levels": {
                "0": {"statsUserUplink": True, "statsUserDownlink": True},
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
            },
        },
        "api": {"tag": "api", "services": ["StatsService"]},
        "inbounds": rendered,
        # Xray auto-builds the "api" outbound from api.tag; freedom is the
        # only explicit outbound (real xray rejects a literal api outbound).
        "outbounds": [{"protocol": "freedom"}],
        # routing.rules: NO "protocol" condition — Xray 26 has no "api"
        # protocol sniffer, so "protocol": ["api"] never matches and api
        # traffic falls through to freedom (looping back into this inbound).
        # InboundTag-only is what Xray's own command_test.go uses.
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api"],
                    "outboundTag": "api",
                }
            ]
        },
    }


def write_config(path: Path, cfg: dict) -> None:
    """Write cfg as pretty JSON (what `xray run -test -c` expects)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
