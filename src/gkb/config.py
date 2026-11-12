from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GKB_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://gkb:gkb@localhost:5432/gkb"

    nextcloud_url: str = ""
    nextcloud_user: str = ""
    nextcloud_app_password: str = ""
    nextcloud_roots: str = ""

    storage_dir: str = "./data/originale"

    vault_key: str = ""
    vault_hmac_key: str = ""

    partition_depth: int = 1

    @property
    def roots(self) -> list[str]:
        return [r.strip().strip("/") for r in self.nextcloud_roots.split(",") if r.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
