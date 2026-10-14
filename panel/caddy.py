"""Render the Caddyfile that front-multiplexes the single public port.

Caddy is the only listener behind the Railway edge: websocket paths
``/ray-<install_token>/<protocol>`` go to the local Xray inbounds
(``panel.paths.PORTS``), everything else to the FastAPI panel on
127.0.0.1:8000. ``Caddyfile.template`` uses ``$CADDY_PORT`` / ``$VPN_TOKEN``;
this module normalizes them to Caddy's env-placeholder syntax ``{$VAR}`` so
caddy expands them from the environment at parse time (the entrypoint
exports both) — the rendered file itself never carries secrets or a fixed
port.
"""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "Caddyfile.template"


def render_caddyfile(port: int, install_token: str) -> str:
    """Render ``Caddyfile.template`` for this install.

    ``port`` and ``install_token`` are validated here — they are the values
    the entrypoint exports for the ``{$CADDY_PORT}`` / ``{$VPN_TOKEN}``
    placeholders, which are preserved verbatim in the output.
    """
    if not 0 < port < 65536:
        raise ValueError(f"port out of range: {port}")
    if not install_token or any(
        char.isspace() or char in "{}" for char in install_token
    ):
        raise ValueError(f"unsafe install token: {install_token!r}")
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("$CADDY_PORT", "{$CADDY_PORT}")
        .replace("$VPN_TOKEN", "{$VPN_TOKEN}")
    )
