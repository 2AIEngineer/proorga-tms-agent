"""Assistant opérateur : questions en langage naturel, rapports de situation.

Graphe ReAct avec checkpointer LangGraph adossé à PostgreSQL (schéma `assistant`) : chaque fil
de conversation (`thread_id`) garde son historique, y compris après un redémarrage. L'assistant lit le TMS et la base de l'agent via MCP ; il peut, sur demande explicite,
clore une alerte, appeler un chauffeur (garde-fous du serveur) ou journaliser une décision, mais
n'émet jamais d'alerte ni de notification hors des règles métier.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from tms_agent.config import AgentSettings
from tms_agent.db import open_pool
from tms_agent.graph.react import (
    build_react_graph,
    initial_state,
    recursion_limit,
    usage_totals,
)
from tms_agent.llm import LlmClient, LlmUnavailable
from tms_agent.mcp_gateway import McpGateway
from tms_agent.prompts import ASSISTANT_SYSTEM
from tms_agent.rules import RuleBook
from tms_agent.tools import (
    COMPACT_ASSISTANT_TOOLS,
    RULE_DRIVEN_TOOLS,
    build_toolbox,
    use_compact_toolset,
)


@dataclass
class Answer:
    text: str
    stop: str
    error: str | None
    tool_calls: list[str]
    usage: dict[str, int]


class OperatorAssistant:
    def __init__(
        self,
        settings: AgentSettings,
        gateway: McpGateway,
        llm: LlmClient,
        rulebook: RuleBook,
        on_event=None,
        checkpointer: BaseCheckpointSaver | None = None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.llm = llm
        self.rulebook = rulebook
        self.on_event = on_event
        self.thread_id = f"ops-{uuid.uuid4().hex[:8]}"
        self._graph = None
        self._seen_calls = 0
        self._checkpointer = checkpointer
        self._pool: AsyncConnectionPool | None = None

    async def _ensure_graph(self):
        if self._graph is None:
            if not self.llm.available:
                raise LlmUnavailable(self.llm.disabled_reason or "LLM indisponible")
            compact = use_compact_toolset(self.settings, self.llm.context_window)
            toolbox = await build_toolbox(
                self.gateway,
                self.rulebook,
                deny=RULE_DRIVEN_TOOLS,
                allow=COMPACT_ASSISTANT_TOOLS if compact else None,
                compact=compact,
                result_max_chars=self.settings.tool_result_max_chars_compact
                if compact
                else self.settings.tool_result_max_chars,
            )
            self._graph = build_react_graph(
                self.llm,
                toolbox,
                system_prompt=ASSISTANT_SYSTEM,
                effort=self.settings.llm_effort_chat,
                max_turns=self.settings.llm_max_turns,
                on_event=self.on_event,
                checkpointer=await self._ensure_checkpointer(),
            )
        return self._graph

    async def _ensure_checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is None:
            schema = self.settings.assistant_db_schema
            open_pool(self.settings.database_url, schema, "", max_size=1).close()  # crée le schéma
            self._pool = AsyncConnectionPool(
                self.settings.database_url,
                min_size=1,
                max_size=2,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                        "options": f"-c search_path={schema}"},
                open=False,
            )
            await self._pool.open(wait=True, timeout=10)
            saver = AsyncPostgresSaver(self._pool)
            await saver.setup()
            self._checkpointer = saver
        return self._checkpointer

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ask(self, question: str) -> Answer:
        graph = await self._ensure_graph()
        config: dict[str, Any] = {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": recursion_limit(self.settings.llm_max_turns),
        }
        state = await graph.ainvoke(
            initial_state(await self._user_content(graph, config, question)), config
        )
        calls = state.get("tool_calls") or []
        new_calls, self._seen_calls = calls[self._seen_calls :], len(calls)
        text = state.get("final_text") or ""
        if state.get("stop") not in ("end_turn", "stop_sequence") and state.get(
            "error"
        ):
            text = (
                text + "\n\n" if text else ""
            ) + f"[réponse incomplète : {state['error']}]"
        return Answer(
            text=text,
            stop=state.get("stop", ""),
            error=state.get("error"),
            tool_calls=[c["name"] for c in new_calls],
            usage=usage_totals(state),
        )

    @staticmethod
    async def _user_content(
        graph, config: dict[str, Any], question: str
    ) -> str | list[dict[str, Any]]:
        """Si le tour précédent s'est arrêté sur des appels de tools sans résultat (coupure, limite),
        on clôt ces appels avant la nouvelle question : l'API exige un résultat par `tool_use`."""
        previous = (await graph.aget_state(config)).values.get("messages") or []
        if not previous or previous[-1]["role"] != "assistant":
            return question
        pending = [b for b in previous[-1]["content"] if b.get("type") == "tool_use"]
        if not pending:
            return question
        closed = [
            {
                "type": "tool_result",
                "tool_use_id": b["id"],
                "content": "Appel interrompu.",
                "is_error": True,
            }
            for b in pending
        ]
        return [*closed, {"type": "text", "text": question}]

    def new_thread(self) -> None:
        self.thread_id = f"ops-{uuid.uuid4().hex[:8]}"
        self._seen_calls = 0
