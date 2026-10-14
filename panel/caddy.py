"""Render the Caddyfile that front-multiplexes the single public port.

Caddy fronts everything: websocket paths ``/ray-<install_token>/<protocol>``
go to the local Xray inbounds (``panel.paths.PORTS``), everything else to the
FastAPI panel on 127.0.0.1:8000. ``Caddyfile.template`` carries two mode
placeholders plus ``$VPN_TOKEN``; this module fills the mode from the
requested TLS mode and normalizes ``$VPN_TOKEN`` to Caddy's env-placeholder
syntax ``{$VAR}`` so caddy expands it from the environment (the entrypoint
exports it) — the rendered file itself never carries secrets or a fixed port.

Two modes:

* **plain** (default, ``auto_tls=False``): a ``:$CADDY_PORT`` listener with
  ``auto_https off`` — the Railway edge (or local dev) owns TLS.
* **auto TLS** (``auto_tls=True``): a ``{$PUBLIC_HOST}`` site with Caddy's
  automatic HTTPS enabled, so Caddy obtains the Let's Encrypt certificate
  itself (HTTP-01 on :80) and serves :443 — the VPS deployment mode.
  ``public_host`` must be a bare hostname; anything else raises so a broken
  domain never ships into production.
"""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "Caddyfile.template"


def _validate_public_host(public_host: str) -> str:
    """Return the stripped host, or raise with an actionable message."""
    host = public_host.strip()
    if (
        not host
        or any(char.isspace() for char in host)
        or "://" in host
        or "/" in host
        or ":" in host
        or "{" in host
        or "}" in host
    ):
        raise ValueError(
            "AUTO_TLS=true requires PUBLIC_HOST to be a bare hostname "
            f"(e.g. vpn.example.com), got {public_host!r}"
        )
    return host


def render_caddyfile(
    port: int,
    install_token: str,
    *,
    auto_tls: bool = False,
    public_host: str = "",
) -> str:
    """Render ``Caddyfile.template`` for this install.

    ``port`` and ``install_token`` are validated here — they are the values
    the entrypoint exports for the ``{$CADDY_PORT}`` / ``{$VPN_TOKEN}``
    placeholders, which are preserved verbatim in the output. In
    ``auto_tls`` mode the site becomes ``{$PUBLIC_HOST}`` and automatic
    HTTPS stays enabled; ``public_host`` must be a bare hostname.
    """
    if not 0 < port < 65536:
        raise ValueError(f"port out of range: {port}")
    if not install_token or any(
        char.isspace() or char in "{}" for char in install_token
    ):
        raise ValueError(f"unsafe install token: {install_token!r}")
    if auto_tls:
        _validate_public_host(public_host)
        global_options = ""  # keep Caddy's automatic HTTPS (LE on :80)
        site_address = "{$PUBLIC_HOST}"
    else:
        global_options = "{\n\tauto_https off\n}"
        site_address = ":{$CADDY_PORT}"
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("$CADDY_GLOBAL_OPTIONS", global_options)
        .replace("$CADDY_SITE_ADDRESS", site_address)
        .replace("$VPN_TOKEN", "{$VPN_TOKEN}")
    )
