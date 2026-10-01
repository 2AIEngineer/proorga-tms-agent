"""Interface commune des fournisseurs de LLM.

Format interne des conversations (indépendant du fournisseur) : messages `user` / `assistant`
dont le contenu est une chaîne ou une liste de blocs :

- `{"type": "text", "text": ...}`
- `{"type": "thinking", "thinking": ..., ...}`            (raisonnement, propre au fournisseur)
- `{"type": "tool_use", "id": ..., "name": ..., "input": {...}}`
- `{"type": "tool_result", "tool_use_id": ..., "content": str, "is_error"?: bool}`

Les outils sont décrits par `{"name", "description", "input_schema"}`. Chaque client traduit
vers le format de son API. Le graphe ReAct ne connaît que ce format.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

# Clé posée dans `input` quand le modèle a produit des arguments JSON illisibles : le tool n'est
# pas exécuté et l'erreur est renvoyée au modèle pour qu'il corrige.
INVALID_ARGUMENTS = "__invalid_arguments__"


class LlmUnavailable(Exception):
    """Le LLM ne peut pas être utilisé (serveur arrêté, identifiants refusés, modèle inconnu)."""


class LlmError(Exception):
    """Échec d'un appel après réessais : l'opération en cours est abandonnée."""


@dataclass
class ModelTurn:
    content: list[dict[str, Any]]
    stop_reason: str  # end_turn | tool_use | max_tokens | pause_turn | refusal
    text: str
    tool_uses: list[dict[str, Any]]
    thinking: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    refusal: str | None = None


class LlmClient(Protocol):
    provider: str
    model: str
    disabled_reason: str | None
    recoverable: bool  # indisponibilité passagère (serveur arrêté) : nouvel essai possible
    context_window: int | None  # tokens ; None = inconnu / très grand

    @property
    def available(self) -> bool: ...

    async def probe(self) -> bool: ...

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        force_tool: str | None = None,  # demander l'appel de ce tool (selon le fournisseur)
    ) -> ModelTurn: ...
