"""Configuration de l'agent (variables d'environnement ou fichier `agent/.env`)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from psycopg.conninfo import make_conninfo
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_DIR = Path(__file__).resolve().parent.parent

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Severity = Literal["low", "medium", "high", "critical"]


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=AGENT_DIR / ".env",
        env_prefix="AGENT_",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Accès au TMS : exclusivement via le serveur MCP ---------------------------------
    # stdio : serveur lancé en sous-processus depuis le même environnement (espace de travail uv) ;
    # http  : déploiement, le serveur MCP tourne dans son propre conteneur
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_url: str = "http://127.0.0.1:8002/mcp"  # http
    mcp_call_timeout_s: float = 45.0

    # --- Règles métier ----------------------------------------------------------------------
    rules_dir: Path = AGENT_DIR / "domain" / "rules"

    # --- Base PostgreSQL (variables POSTGRES_*, sans préfixe : partagées avec le serveur MCP) ---
    postgres_db: str = Field("tms_agent_db", validation_alias="POSTGRES_DB")
    postgres_user: str = Field("postgres", validation_alias="POSTGRES_USER")
    postgres_password: str = Field("", validation_alias="POSTGRES_PASSWORD")
    postgres_host: str = Field("localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, validation_alias="POSTGRES_PORT")
    state_db_schema: str = "monitor"  # événements traités, curseurs de la boucle
    assistant_db_schema: str = (
        "assistant"  # conversations de l'assistant (checkpoints LangGraph)
    )

    # --- Boucle de surveillance ---------------------------------------------------------------
    poll_interval_s: float = 10.0
    mission_concurrency: int = 6
    auto_resolve: bool = True  # clore les alertes d'état dont la condition a disparu, et celles des missions terminées

    # --- LLM -----------------------------------------------------------------------------
    # local     : serveur compatible OpenAI hébergé chez nous (vLLM, Ollama...) — les données restent internes
    # anthropic : API Claude (cloud) — seulement si la politique de données l'autorise
    llm_provider: Literal["local", "anthropic"] = "local"
    llm_enabled: bool = True
    llm_effort_investigation: Effort = "high"
    llm_effort_chat: Effort = "medium"
    llm_max_turns: int = 10  # appels au modèle par enquête / par question
    llm_timeout_s: float = 300.0
    llm_max_retries: int = 3
    llm_show_thinking: bool = False  # afficher le raisonnement du modèle (mode -v)
    llm_reprobe_every_cycles: int = (
        6  # LLM indisponible (serveur arrêté) : nouvel essai tous les N cycles
    )

    # Local (vLLM : scripts/run_vllm_container.sh)
    local_base_url: str = "http://localhost:8001/v1"
    local_model: str = "Qwen/Qwen3.5-2B"
    local_api_key: str = "EMPTY"
    local_max_tokens: int = (
        2048  # réponse ; le reste de la fenêtre sert à la conversation
    )
    local_temperature: float = 0.3
    local_thinking: Literal["auto", "on", "off"] = (
        "auto"  # auto : raisonnement si effort >= high
    )
    local_context_window: int | None = None  # None : lu sur /v1/models (max_model_len)

    # Anthropic
    anthropic_model: str = "claude-opus-5-5"
    anthropic_max_tokens: int = 16000

    # Profil d'outils : "compact" = sous-ensemble par mode et descriptions courtes, pour les
    # petites fenêtres de contexte ; "auto" = compact si la fenêtre fait moins de 32k tokens.
    toolset: Literal["auto", "full", "compact"] = "auto"
    tool_result_max_chars: int = 24000
    tool_result_max_chars_compact: int = 5000

    # --- Enquêtes automatiques sur les nouvelles alertes ------------------------------------
    investigate_min_severity: Severity = "medium"
    max_investigations_per_cycle: int = 4
    investigation_concurrency: int = 2
    # Arrière-plan : la boucle de détection n'attend jamais le LLM (un modèle local peut mettre une
    # minute par enquête) ; au-delà de `max_pending_investigations`, les nouvelles enquêtes sont sautées.
    background_investigations: bool = True
    max_pending_investigations: int = 8

    @property
    def database_url(self) -> str:
        return make_conninfo(
            dbname=self.postgres_db,
            user=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
        )


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
