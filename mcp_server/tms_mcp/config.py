"""Configuration du serveur MCP (variables d'environnement ou fichier `.env`)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_DIR / ".env", extra="ignore")

    # TMS (lecture seule)
    tms_api_url: str = "http://127.0.0.1:8000/v1"
    tms_timeout_s: float = 10.0
    tms_max_retries: int = 3
    tms_max_pages: int = 20
    reference_cache_ttl_s: float = 60.0  # utilisateurs par rôle, itinéraires

    # Base de l'agent (alertes, notifications, journal, appels)
    agent_db_path: Path = PROJECT_DIR / "data" / "agent.db"

    # Garde-fous des actions
    driver_call_min_interval_min: float = 15.0  # temps TMS entre deux appels au même chauffeur pour une mission

    # Transport HTTP (optionnel ; stdio par défaut)
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8002  # convention du POC : TMS 8000, vLLM 8001, MCP 8002


@lru_cache
def get_settings() -> Settings:
    return Settings()
