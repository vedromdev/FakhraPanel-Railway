"""One-time runtime setup: data dir, Xray dir, database, shared secrets."""

import secrets

from panel import db
from panel.config import Settings


def ensure_runtime(settings: Settings) -> None:
    data_dir = settings.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "xray").mkdir(exist_ok=True)
    db.init_db(data_dir / "panel.db")
    db.get_or_create_meta("install_token", lambda: secrets.token_hex(16))
    db.get_or_create_meta("secret_key", lambda: secrets.token_urlsafe(32))
    db.get_or_create_meta("schema_version", lambda: "1")
