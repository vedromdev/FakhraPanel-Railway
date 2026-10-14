"""Render the Caddyfile for the panel-only reverse proxy.

Caddy fronts ONLY the FastAPI panel on 127.0.0.1:8000. VPN traffic never
touches it — Xray owns its inbound ports directly (A2 topology), so the
template has no ``/ray-`` handles and no install token anywhere.

Two modes:

* **plain** (default, ``auto_tls=False``): a ``:$CADDY_PORT`` listener with
  ``auto_https off`` — the Railway edge (or local dev) owns TLS.
* **auto TLS** (``auto_tls=True``): a ``{$PUBLIC_HOST}:8080`` site with
  Caddy's automatic HTTPS enabled, so Caddy obtains the Let's Encrypt
  certificate itself (HTTP-01 on :80) and serves the panel on :8080 —
  :443 stays free for Xray's REALITY inbound. ``public_host`` must be a
  bare hostname; anything else raises so a broken domain never ships.
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
    *,
    auto_tls: bool = False,
    public_host: str = "",
) -> str:
    """Render ``Caddyfile.template`` for this install.

    ``port`` is validated here — it is the value the entrypoint exports for
    the ``{$CADDY_PORT}`` placeholder, preserved verbatim in the output. In
    ``auto_tls`` mode the site becomes ``{$PUBLIC_HOST}:8080`` and
    automatic HTTPS stays enabled; ``public_host`` must be a bare hostname.
    """
    if not 0 < port < 65536:
        raise ValueError(f"port out of range: {port}")
    if auto_tls:
        _validate_public_host(public_host)
        global_options = ""  # keep Caddy's automatic HTTPS (LE on :80)
        site_address = "{$PUBLIC_HOST}:8080"
    else:
        global_options = "{\n\tauto_https off\n}"
        site_address = ":{$CADDY_PORT}"
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("$CADDY_GLOBAL_OPTIONS", global_options).replace(
        "$CADDY_SITE_ADDRESS", site_address
    )
