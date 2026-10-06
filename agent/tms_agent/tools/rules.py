"""Tools locaux sur les règles métier : liste des règles actives, explication sur une mission."""

from typing import Any

from tms_agent.mcp_gateway import McpGateway
from tms_agent.rules_engine import RuleBook, enrich_event, snapshot_context
from tms_agent.tools.base import LocalTool


def rule_tools(rulebook: RuleBook, gateway: McpGateway) -> list[LocalTool]:
    async def list_business_rules(_: dict[str, Any]) -> Any:
        return {"rules": rulebook.describe()}

    async def explain_rules_for_mission(args: dict[str, Any]) -> Any:
        snapshot = await gateway.call(
            "get_mission_snapshot", {"mission_id": args["mission_id"]}
        )
        context, now = snapshot_context(snapshot)
        engine = rulebook.engine
        evaluations = [
            {
                "rule_id": e.rule_id,
                "triggered": e.triggered,
                "conditions": [
                    {
                        "field": t.field,
                        "operator": t.operator,
                        "expected": t.expected,
                        "observed": t.actual,
                        "ok": t.result,
                    }
                    for t in e.trace
                ],
            }
            for e in engine.explain(context, now)
        ]
        event_hits = []
        for event in snapshot["mission"].get("events", []):
            ctx = {**context, "event": enrich_event(event, context["mission"], now)}
            for e in engine.explain(ctx, now):
                if e.triggered:
                    event_hits.append(
                        {
                            "rule_id": e.rule_id,
                            "event_id": event["id"],
                            "event_type": event["type"],
                        }
                    )
        return {
            "tms_time": snapshot["tms_time"],
            "derived_fields": {
                "mission.last_event_age_minutes": context["mission"].get(
                    "last_event_age_minutes"
                ),
                "mission.last_event_type": context["mission"].get("last_event_type"),
                "vehicle.status": context.get("vehicle", {}).get("status"),
                "vehicle.last_seen_age_minutes": context.get("vehicle", {}).get(
                    "last_seen_age_minutes"
                ),
                "deviation.current_offset_km": context.get("deviation", {}).get(
                    "current_offset_km"
                ),
                "deviation.duration_minutes": context.get("deviation", {}).get(
                    "duration_minutes"
                ),
                "eta.delay_minutes": context.get("eta", {}).get("delay_minutes"),
            },
            "state_rules": evaluations,
            "event_rules_triggered_on_history": event_hits,
            "snapshot_errors": snapshot.get("errors") or {},
        }

    return [
        LocalTool(
            name="list_business_rules",
            description=(
                "Retourne les règles métier actives (YAML validées par l'équipe métier) : identifiant, version, "
                "sévérité, catégorie, conditions, portée (état ou événement) et destinataires à notifier. Utiliser "
                "ce tool pour savoir ce que l'entreprise considère comme une anomalie et qui doit être prévenu."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_business_rules,
        ),
        LocalTool(
            name="explain_rules_for_mission",
            description=(
                "Évalue toutes les règles métier sur l'état actuel d'une mission (lu dans le TMS via MCP) et "
                "détaille chaque condition : valeur observée, seuil attendu, résultat. Donne aussi les champs "
                "dérivés (âge du dernier événement, durée d'écart, retard). Utiliser ce tool pour expliquer "
                "pourquoi une alerte a été déclenchée, ou à quel point une mission est proche d'un seuil."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mission_id": {
                        "type": "string",
                        "description": "Identifiant de la mission.",
                    }
                },
                "required": ["mission_id"],
                "additionalProperties": False,
            },
            handler=explain_rules_for_mission,
        ),
    ]
