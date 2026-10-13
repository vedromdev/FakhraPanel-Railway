"""Where Xray listens inside the container (loopback only; the edge terminates TLS)."""

PORTS = {
    "vless": 10001,
    "vmess": 10002,
    "trojan": 10003,
    "ss": 10004,
    "stats": 10010,
}


def vpn_path(install_token: str, protocol: str) -> str:
    """Per-install, per-protocol websocket path for an inbound."""
    return f"/ray-{install_token}/{protocol}"
