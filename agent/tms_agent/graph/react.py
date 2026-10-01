"""Graphe de raisonnement ReAct (LangGraph) : modèle ⇄ tools, jusqu'à la réponse finale.

    START → model ─┬─ tool_use ──→ tools ─┬─ (tool terminal appelé) → END
                   │                      └─ sinon → model
                   ├─ pause_turn → model
                   ├─ end_turn / max_tokens sans tool terminal (une fois) → nudge → model (appel forcé)
                   └─ end_turn / refusal / max_tokens / erreur → END

Garde-fous :
- nombre maximal d'appels au modèle (`max_turns`) ;
- tools exécutés en parallèle, résultats renvoyés dans un seul message (erreurs comprises, avec
  `is_error`) : un tool en échec n'interrompt jamais le raisonnement ;
- historique append-only (les blocs de réflexion sont renvoyés tels quels) ;
- erreurs du LLM capturées dans l'état (`stop="error"` / `"unavailable"`), jamais propagées ;
- checkpointer optionnel : conversation multi-tours pour l'assistant ;
- relance : un modèle (souvent petit) qui conclut en texte sans appeler le tool terminal attendu
  reçoit une consigne de le faire (`max_nudges` fois au plus) ; l'appel suivant demande au
  fournisseur de forcer ce tool (décodage guidé chez vLLM ; simple consigne chez Anthropic) ;
- dernier tour autorisé : le tool terminal est forcé, pour conclure avec ce qui a été collecté
  plutôt qu'échouer sur la limite d'appels.
"""

import asyncio
import json
import operator
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from tms_agent.llm import LlmClient, LlmError, LlmUnavailable
from tms_agent.tools import Toolbox


class ReactState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]
    turns: int
    nudges: int
    force_tool: bool
    stop: str
    final_text: str
    error: str | None
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    thinking: Annotated[list[str], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]


def initial_state(user_content: str | list[dict[str, Any]]) -> ReactState:
    """État d'entrée d'une question ; les compteurs repartent à zéro (l'historique est conservé par le checkpointer)."""
    return {
        "messages": [{"role": "user", "content": user_content}],
        "turns": 0,
        "nudges": 0,
        "stop": "",
        "final_text": "",
        "error": None,
    }


def build_react_graph(
    llm: LlmClient,
    toolbox: Toolbox,
    *,
    system_prompt: str,
    effort: str,
    max_turns: int,
    terminal_tool: str | None = None,
    max_nudges: int = 1,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    checkpointer: Any = None,
):
    tools = toolbox.definitions()
    emit = on_event or (lambda kind, data: None)

    async def call_model(state: ReactState) -> dict[str, Any]:
        turns = state.get("turns", 0)
        if turns >= max_turns:
            return {
                "stop": "max_turns",
                "error": f"limite de {max_turns} appels au modèle atteinte",
            }
        try:
            last_turn = turns == max_turns - 1
            forced = terminal_tool if (state.get("force_tool") or last_turn) else None
            turn = await llm.complete(
                system=system_prompt,
                messages=state["messages"],
                tools=tools,
                effort=effort,
                force_tool=forced,
            )
        except LlmUnavailable as exc:
            return {"stop": "unavailable", "error": str(exc)}
        except LlmError as exc:
            return {"stop": "error", "error": str(exc)}
        for t in turn.thinking:
            emit("thinking", {"text": t})
        if turn.text and turn.stop_reason != "tool_use":
            emit("text", {"text": turn.text})
        update: dict[str, Any] = {
            "messages": [{"role": "assistant", "content": turn.content}],
            "turns": turns + 1,
            "force_tool": False,
            "stop": turn.stop_reason,
            "thinking": turn.thinking,
            "usage": [turn.usage],
        }
        if turn.text:
            update["final_text"] = turn.text
        if turn.refusal:
            update["error"] = f"requête déclinée : {turn.refusal}"
        return update

    async def run_tools(state: ReactState) -> dict[str, Any]:
        uses = [
            b for b in state["messages"][-1]["content"] if b.get("type") == "tool_use"
        ]
        for u in uses:
            emit("tool_call", {"name": u["name"], "input": u.get("input") or {}})
        results = await asyncio.gather(
            *(toolbox.execute(u["name"], u.get("input") or {}) for u in uses)
        )
        blocks, trace = [], []
        for use, (content, is_error) in zip(uses, results):
            block = {
                "type": "tool_result",
                "tool_use_id": use["id"],
                "content": content,
            }
            if is_error:
                block["is_error"] = True
            blocks.append(block)
            trace.append(
                {
                    "name": use["name"],
                    "input": use.get("input") or {},
                    "is_error": is_error,
                    "result_preview": content[:300],
                }
            )
            emit(
                "tool_result",
                {"name": use["name"], "is_error": is_error, "preview": content[:200]},
            )
        update: dict[str, Any] = {
            "messages": [{"role": "user", "content": blocks}],
            "tool_calls": trace,
        }
        if terminal_tool and any(
            t["name"] == terminal_tool and not t["is_error"] for t in trace
        ):
            update["stop"] = "submitted"
        return update

    async def nudge(state: ReactState) -> dict[str, Any]:
        emit("nudge", {"tool": terminal_tool})
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Termine maintenant en appelant le tool `{terminal_tool}` avec ta conclusion "
                        "(synthèse, niveau de risque low/medium/high/critical, actions recommandées). "
                        "N'écris pas la conclusion en texte libre."
                    ),
                }
            ],
            "nudges": state.get("nudges", 0) + 1,
            "force_tool": True,
        }

    def after_model(state: ReactState) -> str:
        stop = state.get("stop")
        if stop == "tool_use":
            return "tools"
        if stop == "pause_turn":
            return "model"
        if stop in ("end_turn", "max_tokens") and terminal_tool and state.get("nudges", 0) < max_nudges:
            return "nudge"
        return END

    def after_tools(state: ReactState) -> str:
        return END if state.get("stop") == "submitted" else "model"

    graph = StateGraph(ReactState)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    graph.add_node("nudge", nudge)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model",
        after_model,
        {"tools": "tools", "model": "model", "nudge": "nudge", END: END},
    )
    graph.add_edge("nudge", "model")
    graph.add_conditional_edges("tools", after_tools, {"model": "model", END: END})
    return graph.compile(checkpointer=checkpointer)


def recursion_limit(max_turns: int) -> int:
    return 2 * max_turns + 10


def terminal_input(state: ReactState, tool_name: str) -> dict[str, Any] | None:
    """Arguments du dernier appel réussi au tool terminal (ex. l'évaluation structurée)."""
    for call in reversed(state.get("tool_calls") or []):
        if call["name"] == tool_name and not call["is_error"]:
            return call["input"]
    return None


def usage_totals(state: ReactState) -> dict[str, int]:
    totals: dict[str, int] = {}
    for u in state.get("usage") or []:
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if isinstance(u.get(k), int):
                totals[k] = totals.get(k, 0) + u[k]
    return totals


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
