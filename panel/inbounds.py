"""Inbound domain rules shared by the admin routes.

Pure functions over plain row dicts — no DB, no settings. `validate_settings`
normalizes (transport/security defaults, REALITY keypair/shortIds/fp/flow)
and rejects impossible combinations with actionable ValueError text; the
routes map those to 400s.
"""

import secrets

from panel.xray import keys as x25519_keys

RESERVED_PORTS = frozenset({80, 8080})  # edge (ACME/redirect) and panel
PRE_OPENED = frozenset({80, 443, 8080, 10002, 10003, 10004})  # install.sh

_TRANSPORTS = ("tcp", "ws", "grpc", "xhttp")
_SECURITIES = ("none", "tls", "reality")


def firewall_hint(port: int) -> str | None:
    """ufw line for a port install.sh didn't pre-open; None when already open."""
    if port in PRE_OPENED:
        return None
    return f"ufw allow {port}/tcp"


def validate_settings(protocol: str, settings: dict) -> dict:
    """Validate + normalize inbound settings; ValueError text becomes the 400."""
    s = dict(settings or {})
    transport = s.get("transport") or "tcp"
    security = s.get("security") or "none"
    if transport not in _TRANSPORTS:
        raise ValueError(
            f"transport must be one of {'|'.join(_TRANSPORTS)}, got {transport!r}"
        )
    if security not in _SECURITIES:
        raise ValueError(
            f"security must be one of {'|'.join(_SECURITIES)}, got {security!r}"
        )
    s["transport"] = transport
    s["security"] = security
    if security == "reality" and protocol != "vless":
        raise ValueError("reality is only available for vless inbounds")
    if security == "tls" and not (s.get("cert_file") and s.get("key_file")):
        raise ValueError("tls requires cert_file and key_file (your PEM paths)")
    if security == "reality":
        if not s.get("private_key"):
            private, public = x25519_keys.generate_keypair()
            s["private_key"], s["public_key"] = private, public
        elif not s.get("public_key"):
            s["public_key"] = x25519_keys.public_from_private(s["private_key"])
        if not s.get("server_names") or not s.get("dest"):
            raise ValueError(
                "reality requires server_names (e.g. [\"www.apple.com\"]) and "
                "dest (e.g. \"www.apple.com:443\") — a site your VPS and "
                "clients can both reach"
            )
        s.setdefault("short_ids", [secrets.token_hex(4)])
        s.setdefault("fp", "chrome")
        s.setdefault("flow", "xtls-rprx-vision")
    if s.get("flow") and not (protocol == "vless" and security == "reality"):
        raise ValueError("flow requires a vless inbound with reality security")
    return s


def resolve_inbound_id(
    protocol: str, requested: str | None, inbounds: list[dict]
) -> str:
    """Pick the inbound a new user of `protocol` attaches to.

    `requested` must name an ENABLED inbound of the same protocol; otherwise
    the oldest enabled inbound of that protocol is chosen. ValueError carries
    the route's 400 message verbatim.
    """
    matching = [
        row
        for row in inbounds
        if row["protocol"] == protocol and row.get("enabled", 1)
    ]
    if requested is not None:
        if any(row["id"] == requested for row in matching):
            return requested
        raise ValueError(
            f"inbound {requested!r} is not an enabled {protocol} inbound"
        )
    if not matching:
        raise ValueError(f"no enabled {protocol} inbound — create one first")
    chosen = min(matching, key=lambda row: (row["created_at"], row["id"]))
    return chosen["id"]
