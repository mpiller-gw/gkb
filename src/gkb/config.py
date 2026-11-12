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

    # Baustein 1b: Rechte aus "nextcloud" (Freigaben + Gruppenordner) oder "pfad" (Fallback)
    rechte_modus: str = "nextcloud"
    # Optional: Admin-Zugang nur zum Lesen der Gruppenordner-Konfiguration
    nextcloud_admin_user: str = ""
    nextcloud_admin_password: str = ""

    # Baustein 2: Aufbereitung
    modell_klein: str = ""                    # LiteLLM-Modellname für Tags, Klingelschilder, Fakten
    modell_klein_api_base: str = ""           # nur für lokale Modelle, z. B. http://llm:8000/v1
    embedding_modell: str = "intfloat/multilingual-e5-large"
    kunde_ebene: int = 1                      # Ordnerebene unter dem Wurzelordner mit dem Kundennamen (0 = aus)
    projekt_ebene: int = 2                    # Ordnerebene mit dem Projekt (0 = aus)

    @property
    def roots(self) -> list[str]:
        return [r.strip().strip("/") for r in self.nextcloud_roots.split(",") if r.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
