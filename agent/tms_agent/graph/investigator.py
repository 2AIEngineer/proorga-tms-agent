"""Enquêteur : analyse par le LLM d'une alerte émise par les règles.

Sous-graphe ReAct avec les tools MCP (lecture du TMS, historique de l'agent, appel chauffeur sous
garde-fous) et les tools de règles ; se termine par `submit_assessment` (schéma strict).
"""

from typing import Any

from tms_agent.config import AgentSettings
from tms_agent.graph.react import (
    build_react_graph,
    initial_state,
    pretty,
    recursion_limit,
    terminal_input,
    usage_totals,
)
from tms_agent.llm import LlmClient
from tms_agent.prompts import INVESTIGATION_REQUEST, INVESTIGATOR_SYSTEM
from tms_agent.tools import (
    SUBMIT_ASSESSMENT,
    Toolbox,
    ToolInputError,
    validate_assessment,
)


class InvestigationFailed(Exception):
    pass


# Champs de l'alerte utiles à l'enquête (le reste est du bruit pour le modèle).
_ALERT_FIELDS = (
    "id",
    "rule_id",
    "rule_version",
    "mission_id",
    "event_id",
    "severity",
    "category",
    "title",
    "message",
    "observed_data",
    "created_at",
)


class Investigator:
    def __init__(
        self, llm: LlmClient, toolbox: Toolbox, settings: AgentSettings, on_event=None
    ):
        self.llm = llm
        self.max_turns = settings.llm_max_turns
        self.graph = build_react_graph(
            llm,
            toolbox,
            system_prompt=INVESTIGATOR_SYSTEM,
            effort=settings.llm_effort_investigation,
            max_turns=settings.llm_max_turns,
            terminal_tool=SUBMIT_ASSESSMENT,
            on_event=on_event,
        )

    async def investigate(
        self,
        alert: dict[str, Any],
        notifications: list[dict[str, Any]],
        tms_time: str,
        facts: list[str] | None = None,
    ) -> dict[str, Any]:
        request = INVESTIGATION_REQUEST.format(
            tms_time=tms_time,
            facts="\n".join(f"- {f}" for f in facts)
            if facts
            else "(non disponibles : utiliser get_mission_snapshot)",
            alert=pretty({k: alert.get(k) for k in _ALERT_FIELDS}),
            notifications=pretty(
                [
                    {
                        k: n.get(k)
                        for k in (
                            "recipient_role",
                            "recipient_name",
                            "management_level",
                            "channel",
                            "status",
                            "error",
                        )
                    }
                    for n in notifications
                ]
            )
            if notifications
            else "(aucune)",
        )
        state = await self.graph.ainvoke(
            initial_state(request), {"recursion_limit": recursion_limit(self.max_turns)}
        )
        assessment = terminal_input(state, SUBMIT_ASSESSMENT)
        meta = {
            "model": self.llm.model,
            "tool_calls": [
                c["name"]
                for c in state.get("tool_calls") or []
                if c["name"] != SUBMIT_ASSESSMENT
            ],
            "model_turns": state.get("turns", 0),
            "usage": usage_totals(state),
        }
        if assessment is not None:
            try:
                normalized = validate_assessment(assessment).model_dump()
            except ToolInputError:  # ne devrait pas arriver : le tool valide déjà
                normalized = {
                    "summary": str(assessment.get("summary") or assessment)[:2000]
                }
            return {**normalized, **meta, "status": "complete"}
        if state.get("final_text"):
            # Le modèle a conclu sans le tool terminal : on garde sa conclusion, marquée partielle.
            return {
                "summary": state["final_text"][:2000],
                **meta,
                "status": "partial",
                "note": state.get("error") or f"arrêt : {state.get('stop')}",
            }
        raise InvestigationFailed(
            state.get("error") or f"enquête interrompue ({state.get('stop')})"
        )
