"""Build the full Xray config dict for all enabled users.

Pure functions: plain arguments in, plain dicts out — no settings, env, or DB.
"""

import json
from pathlib import Path

from panel.constants import DEFAULT_SS_METHOD
from panel.paths import PORTS, vpn_path

LISTEN = "127.0.0.1"

# db protocol name -> Xray inbound protocol name
_XRAY_PROTOCOL = {
    "vless": "vless",
    "vmess": "vmess",
    "trojan": "trojan",
    "ss": "shadowsocks",
}

# protocols that get a websocket inbound (PORTS minus the stats/api listener)
_PROTOCOLS = ("vless", "vmess", "trojan", "ss")


def _client(protocol: str, user: dict) -> dict:
    client: dict = {"level": 0, "email": user["id"]}
    if protocol in ("vless", "vmess"):
        client["id"] = user["uuid"]
    elif protocol == "trojan":
        client["password"] = user["credential"]
    else:  # ss
        client["method"] = user["ss_method"] or DEFAULT_SS_METHOD
        client["password"] = user["credential"]
    return client


def _inbound_settings(protocol: str, users: list[dict]) -> dict:
    settings: dict = {"clients": [_client(protocol, u) for u in users]}
    if protocol == "vless":
        settings["decryption"] = "none"
    elif protocol == "ss":
        settings["network"] = "tcp"
    return settings


def _ws_inbound(protocol: str, users: list[dict], install_token: str,
                public_host: str) -> dict:
    return {
        "tag": f"in-{protocol}",
        "listen": LISTEN,
        "port": PORTS[protocol],
        "protocol": _XRAY_PROTOCOL[protocol],
        "settings": _inbound_settings(protocol, users),
        "streamSettings": {
            "network": "ws",
            "security": "none",
            "wsSettings": {
                "path": vpn_path(install_token, protocol),
                "headers": {"Host": public_host},
            },
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
    }


def build_config(users: list[dict], install_token: str, public_host: str) -> dict:
    """Full Xray config: one ws inbound per protocol with >=1 enabled user."""
    enabled: dict[str, list[dict]] = {}
    for user in users:
        if user.get("enabled", 1):
            enabled.setdefault(user["protocol"], []).append(user)

    inbounds = [
        _ws_inbound(protocol, enabled[protocol], install_token, public_host)
        for protocol in _PROTOCOLS
        if protocol in enabled
    ]
    # StatsService api access: dokodemo-door redirecting to the api outbound
    inbounds.append(
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
        "inbounds": inbounds,
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
