"""Profils d'outils : tools interdits au LLM et sous-ensembles du profil compact."""

from typing import Any

# Tools réservés à la boucle déterministe : une alerte naît d'une règle, ses destinataires aussi.
RULE_DRIVEN_TOOLS = {"create_alert", "notify_recipients"}

# Sous-ensembles du profil compact (les tools absents du serveur MCP sont simplement ignorés).
COMPACT_INVESTIGATION_TOOLS = {
    "get_mission_snapshot",
    "get_mission_events",
    "get_vehicle_positions",
    "explain_rules_for_mission",
    "list_alerts",
    "list_agent_actions",
    "call_driver",
    "submit_assessment",
}
COMPACT_ASSISTANT_TOOLS = {
    "get_operations_overview",
    "get_active_missions",
    "get_mission_snapshot",
    "get_mission_events",
    "list_alerts",
    "get_alert",
    "explain_rules_for_mission",
    "list_business_rules",
    "list_agent_actions",
    "resolve_alert",
    "call_driver",
}


def use_compact_toolset(settings: Any, context_window: int | None) -> bool:
    if settings.toolset != "auto":
        return settings.toolset == "compact"
    return context_window is not None and context_window < 32000
