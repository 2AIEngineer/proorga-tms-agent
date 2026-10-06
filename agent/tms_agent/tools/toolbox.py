"""Boîte à outils exposée au LLM : tools MCP découverts dynamiquement + tools locaux, filtrés par mode."""

import json
import logging
from typing import Any

from mcp.types import Tool

from tms_agent.llm import INVALID_ARGUMENTS
from tms_agent.mcp_gateway import McpGateway, McpToolError, McpUnavailable
from tms_agent.rules_engine import RuleBook
from tms_agent.tools.base import LocalTool, ToolInputError
from tms_agent.tools.formatting import first_sentence, prune, strip_titles
from tms_agent.tools.rules import rule_tools

log = logging.getLogger(__name__)


class Toolbox:
    def __init__(
        self,
        gateway: McpGateway,
        mcp_tools: list[Tool],
        local_tools: list[LocalTool] = (),
        *,
        deny: set[str] = frozenset(),
        allow: set[str] | None = None,
        compact: bool = False,
        result_max_chars: int = 24000,
    ):
        self.gateway = gateway
        self.compact = compact
        self.result_max_chars = result_max_chars

        def keep(name: str) -> bool:
            return name not in deny and (allow is None or name in allow)

        self._mcp = {t.name: t for t in mcp_tools if keep(t.name)}
        self._local = {t.name: t for t in local_tools if keep(t.name)}

    @property
    def names(self) -> list[str]:
        return sorted({*self._mcp, *self._local})

    def definitions(self) -> list[dict[str, Any]]:
        """Définitions `{name, description, input_schema}` triées par nom (préfixe stable pour le cache)."""
        defs = []
        for name in self.names:
            if name in self._local:
                tool = self._local[name]
                description, schema = tool.description, tool.input_schema
                d = {
                    "name": name,
                    "description": first_sentence(description)
                    if self.compact
                    else description,
                    "input_schema": schema,
                }
                if tool.strict and not self.compact:
                    d["strict"] = True
            else:
                tool = self._mcp[name]
                description = (tool.description or "").strip()
                if self.compact:
                    d = {
                        "name": name,
                        "description": first_sentence(description),
                        "input_schema": strip_titles(tool.input_schema),
                    }
                else:
                    if tool.title:
                        description = f"{tool.title}. {description}"
                    meta = tool.meta or {}
                    if meta.get("backend"):
                        description += (
                            f"\n[backend: {meta['backend']}, catégorie: {meta.get('category')}, "
                            f"lecture seule: {meta.get('read_only')}]"
                        )
                    d = {
                        "name": name,
                        "description": description,
                        "input_schema": tool.input_schema,
                    }
            defs.append(d)
        return defs

    async def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Exécute un tool ; renvoie (contenu texte, is_error). Ne lève jamais : l'erreur est renvoyée au LLM."""
        if INVALID_ARGUMENTS in arguments:
            return (
                f"Arguments illisibles pour {name} (JSON invalide) : {arguments[INVALID_ARGUMENTS][:300]}. "
                "Relancer l'appel avec un objet JSON valide."
            ), True
        try:
            if name in self._local:
                result = await self._local[name].handler(arguments)
            elif name in self._mcp:
                result = await self.gateway.call(name, arguments)
            else:
                return (
                    f"Tool inconnu ou non autorisé dans ce mode : {name}. Tools disponibles : {', '.join(self.names)}",
                    True,
                )
        except McpToolError as exc:
            return exc.message, True
        except McpUnavailable as exc:
            return (
                f"Serveur MCP indisponible : {exc}. Réessayer plus tard ou conclure avec les données déjà obtenues.",
                True,
            )
        except ToolInputError as exc:
            return str(exc), True
        except Exception as exc:
            log.exception("Tool %s en échec", name)
            return f"Échec du tool {name} : {type(exc).__name__}: {exc}", True
        if isinstance(result, str):
            text = result
        elif self.compact:
            text = json.dumps(
                prune(result), ensure_ascii=False, default=str, separators=(",", ":")
            )
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > self.result_max_chars:
            text = (
                text[: self.result_max_chars]
                + f"\n… [résultat tronqué : {len(text)} caractères ; affiner les filtres]"
            )
        return text, False


async def build_toolbox(
    gateway: McpGateway,
    rulebook: RuleBook,
    *,
    deny: set[str] = frozenset(),
    allow: set[str] | None = None,
    compact: bool = False,
    extra: list[LocalTool] = (),
    result_max_chars: int = 24000,
) -> Toolbox:
    mcp_tools = await gateway.list_tools()
    return Toolbox(
        gateway,
        mcp_tools,
        [*rule_tools(rulebook, gateway), *extra],
        deny=deny,
        allow=allow,
        compact=compact,
        result_max_chars=result_max_chars,
    )
