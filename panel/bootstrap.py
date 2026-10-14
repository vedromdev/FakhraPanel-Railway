"""One-time runtime setup: data dir, Xray dir, database, secrets, Caddyfile."""

import secrets

from panel import caddy, db
from panel.config import Settings, get_settings


def ensure_runtime(settings: Settings) -> None:
    data_dir = settings.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "xray").mkdir(exist_ok=True)
    db.init_db(data_dir / "panel.db")
    install_token = db.get_or_create_meta(
        "install_token", lambda: secrets.token_hex(16)
    )
    db.get_or_create_meta("secret_key", lambda: secrets.token_urlsafe(32))
    db.get_or_create_meta("schema_version", lambda: "1")
    # Re-rendered on every startup: the token is stable but the port may
    # change between deploys. caddy expands the placeholders from the env.
    (data_dir / "Caddyfile").write_text(
        caddy.render_caddyfile(settings.PORT, install_token), encoding="utf-8"
    )


if __name__ == "__main__":
    # CLI mode for the container entrypoint: ensure everything exists, then
    # print the install token (the only stdout line) for `VPN_TOKEN=...`.
    ensure_runtime(get_settings())
    print(db.get_or_create_meta("install_token", lambda: secrets.token_hex(16)))
