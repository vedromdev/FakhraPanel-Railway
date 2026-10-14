"""Settings for Fakhra Panel, loaded from environment / .env."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PORT: int = 8080
    ADMIN_USERNAME: str
    ADMIN_PASSWORD: str
    SECRET_KEY: str = ""
    PUBLIC_HOST: str = ""
    # VPS mode: Caddy serves PUBLIC_HOST with Let's Encrypt (auto HTTPS on),
    # owning :80/:443 itself instead of hiding behind an edge proxy.
    AUTO_TLS: bool = False
    DATA_DIR: str = ""
    XRAY_BIN: str = "xray"
    COOKIE_SECURE: bool = True
    ENV: str = "development"
    DASHBOARD_TITLE: str = "Fakhra Panel"

    def data_dir(self) -> Path:
        """Explicit DATA_DIR wins; else /data when present; else ./data."""
        if self.DATA_DIR:
            return Path(self.DATA_DIR)
        if os.path.isdir("/data"):
            return Path("/data")
        return Path("./data")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
