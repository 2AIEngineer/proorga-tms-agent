"""Client Claude (API Anthropic, cloud) — SDK officiel `anthropic`.

À n'utiliser que si la politique de données l'autorise (AGENT_LLM_PROVIDER=anthropic).

- Modèle par défaut : Claude Opus 5.5, réflexion adaptative, `effort` explicite par usage.
- Cache de prompt automatique (`cache_control` de premier niveau) : prompt système figé et
  liste de tools triée, donc préfixe stable d'un appel à l'autre.
- Fallback côté serveur (`fallbacks: "default"`) si une requête est déclinée par un classifieur.
- Historique append-only : le contenu de la réponse (blocs de réflexion compris) est renvoyé tel quel.
- Dégradation : sans identifiants ou si l'API refuse l'authentification, le client se déclare
  indisponible et l'agent continue en mode déterministe (règles + notifications).
"""

import logging
from typing import Any

import anthropic

from tms_agent.config import AgentSettings
from tms_agent.llm.base import LlmError, LlmUnavailable, ModelTurn

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeClient:
    provider = "anthropic"

    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.model = settings.anthropic_model
        self.disabled_reason: str | None = None
        self.recoverable = False
        self.context_window: int | None = None  # 1M : aucune contrainte pratique
        self._client: anthropic.AsyncAnthropic | None = None
        if not settings.llm_enabled:
            self.disabled_reason = "LLM désactivé par la configuration (AGENT_LLM_ENABLED=false)"
            return
        try:
            self._client = anthropic.AsyncAnthropic(timeout=settings.llm_timeout_s, max_retries=settings.llm_max_retries)
        except Exception as exc:  # noqa: BLE001 — identifiants introuvables, configuration invalide
            self.disabled_reason = f"client Anthropic indisponible : {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None and self.disabled_reason is None

    def _disable(self, reason: str) -> LlmUnavailable:
        self.disabled_reason = reason
        log.error("LLM désactivé : %s", reason)
        return LlmUnavailable(reason)

    async def probe(self) -> bool:
        """Vérifie identifiants et modèle au démarrage (comptage de tokens : gratuit, sans génération)."""
        if not self.available:
            return False
        try:
            await self._client.messages.count_tokens(model=self.model, messages=[{"role": "user", "content": "ping"}])
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            self._disable(f"accès à l'API refusé ({exc.status_code}) : vérifier ANTHROPIC_API_KEY")
        except anthropic.NotFoundError:
            self._disable(f"modèle {self.model!r} introuvable")
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.APIStatusError) as exc:
            log.warning("Vérification du LLM non concluante (%s) ; nouvel essai au premier appel", exc)
        except TypeError as exc:
            self._disable(f"aucun identifiant Anthropic (définir ANTHROPIC_API_KEY) : {exc}")
        return self.available

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        force_tool: str | None = None,  # ignoré : Opus 5.5 refuse le tool_choice forcé, la consigne suffit
    ) -> ModelTurn:
        if not self.available:
            raise LlmUnavailable(self.disabled_reason or "LLM indisponible")
        thinking: dict[str, Any] = {"type": "adaptive"}
        if self.settings.llm_show_thinking:
            thinking["display"] = "summarized"
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=self.settings.anthropic_max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                thinking=thinking,
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise self._disable(f"accès à l'API refusé ({exc.status_code}) : vérifier ANTHROPIC_API_KEY") from exc
        except anthropic.NotFoundError as exc:
            raise self._disable(f"modèle {self.model!r} introuvable : {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise LlmError(f"requête refusée par l'API : {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LlmError("limite de débit atteinte malgré les réessais") from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(f"erreur API {exc.status_code} : {exc.message}") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LlmError(f"API injoignable : {exc}") from exc
        except TypeError as exc:  # identifiants non résolus au premier appel
            if "auth" in str(exc).lower() or "api_key" in str(exc).lower():
                raise self._disable(f"aucun identifiant Anthropic : {exc}") from exc
            raise

        data = response.to_dict()
        content = data.get("content") or []
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        refusal = None
        if response.stop_reason == "refusal":
            details = data.get("stop_details") or {}
            refusal = details.get("explanation") or details.get("category") or "requête déclinée"
        usage = data.get("usage") or {}
        log.debug(
            "Claude %s : stop=%s in=%s cache_read=%s out=%s",
            response.model, response.stop_reason, usage.get("input_tokens"),
            usage.get("cache_read_input_tokens"), usage.get("output_tokens"),
        )
        return ModelTurn(
            content=content,
            stop_reason=response.stop_reason or "end_turn",
            text=text,
            tool_uses=[b for b in content if b.get("type") == "tool_use"],
            thinking=[b["thinking"] for b in content if b.get("type") == "thinking" and b.get("thinking")],
            usage=usage,
            refusal=refusal,
        )
