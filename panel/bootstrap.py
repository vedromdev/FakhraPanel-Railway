"""One-time runtime setup: data dir, Xray dir, database, secrets, Caddyfile."""

import secrets
import sqlite3
from pathlib import Path

from panel import caddy, db
from panel.config import Settings, get_settings


def _align_secret_key(db_path: Path, configured: str) -> None:
    """Seed/align the meta `secret_key` that session tokens are HMAC-signed with.

    - ``SECRET_KEY`` set in the environment: the meta row must EQUAL it. A
      differing existing value is overwritten **in place** — never deleted,
      because ``db._hash_token`` raises for as long as the row is absent — so
      rotating the env var invalidates every session on the next boot, which
      is the documented semantics of a signing key.
    - blank/absent: unchanged behavior — random once, stable across boots.

    The UPDATE lives here (not in ``panel.db``) because ``db`` exposes no
    meta-update path and bootstrap is the only place that owns secret
    alignment; ``get_or_create_meta`` handles the create-if-missing half.
    """
    if configured.strip():
        db.get_or_create_meta("secret_key", lambda: configured)
        if db.read_meta("secret_key") != configured:
            conn = sqlite3.connect(db_path)
            try:
                with conn:  # single UPDATE, committed atomically in place
                    conn.execute(
                        "UPDATE meta SET value = ? WHERE key = ?",
                        (configured, "secret_key"),
                    )
            finally:
                conn.close()
    else:
        db.get_or_create_meta("secret_key", lambda: secrets.token_urlsafe(32))


def ensure_runtime(settings: Settings) -> None:
    data_dir = settings.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "xray").mkdir(exist_ok=True)
    db_path = data_dir / "panel.db"
    db.init_db(db_path)
    install_token = db.get_or_create_meta(
        "install_token", lambda: secrets.token_hex(16)
    )
    _align_secret_key(db_path, settings.SECRET_KEY)
    db.get_or_create_meta("schema_version", lambda: "1")
    db.migrate()  # bumps schema_version to current; no-op once migrated
    # Re-rendered on every startup: the token is stable but the port may
    # change between deploys. caddy expands the placeholders from the env.
    (data_dir / "Caddyfile").write_text(
        caddy.render_caddyfile(
            settings.PORT,
            install_token,
            auto_tls=settings.AUTO_TLS,
            public_host=settings.PUBLIC_HOST,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    # CLI mode for the container entrypoint: ensure everything exists, then
    # print the install token (the only stdout line) for `VPN_TOKEN=...`.
    ensure_runtime(get_settings())
    print(db.get_or_create_meta("install_token", lambda: secrets.token_hex(16)))
