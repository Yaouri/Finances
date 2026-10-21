"""Settings loaded from environment / .env. Secrets are never logged or repr'd."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    eb_app_id: SecretStr | None = None
    eb_key_path: Path | None = None
    eb_redirect_url: str = "https://localhost:8501/callback"
    eb_api_url: str = "https://api.enablebanking.com"
    fin_data_dir: Path = Field(default=Path("data"))

    @property
    def data_dir(self) -> Path:
        d = self.fin_data_dir
        if not d.is_absolute():
            d = PROJECT_ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fin.db"

    @property
    def key_path(self) -> Path:
        if self.eb_key_path is None:
            raise RuntimeError("EB_KEY_PATH is not set (see .env.example)")
        p = self.eb_key_path
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
