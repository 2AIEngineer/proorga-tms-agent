"""Chemins LLM (Claude remplacé par un modèle scripté) : enquête, garde-fous, assistant."""

import json

from tms_agent.graph.assistant import OperatorAssistant
from tms_agent.llm import LlmError
from tms_agent.mcp_gateway import McpGateway
from tms_agent.rules_engine import RuleBook
from tms_mcp.config import Settings as ServerSettings
from tms_mcp.server import create_server

from tests.conftest import ScriptedLLM, running_agent, tool_use

ASSESSMENT = {
    "summary": "Dérive de 6,4 km confirmée.", "situation": "Écart depuis 27 min.", "probable_cause": "Détour",
    "risk_level": "high", "customer_impact": "Faible", "recommended_actions": ["Agent de suivi : confirmer avec le chauffeur"],
    "evidence": ["current_offset_km = 6.4"], "driver_called": True, "false_positive_suspected": False, "confidence": 0.8,
}


def tool_results(messages):
    return [b for b in messages[-1]["content"] if b.get("type") == "tool_result"]


async def test_investigation_reads_tms_calls_driver_and_annotates(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    seen = {}

    def investigate(messages):
        prompt = messages[0]["content"]
        seen["prompt"] = prompt
        alert_id = prompt.split('"id": "')[1].split('"')[0]
        return [{"type": "text", "text": "Je vérifie."},
                tool_use("get_mission_snapshot", mission_id="msn_1"),
                tool_use("explain_rules_for_mission", mission_id="msn_1"),
                tool_use("create_alert", rule_id="x", rule_version=1, severity="low", title="improvisée"),
                tool_use("call_driver", mission_id="msn_1", reason="Vérifier la dérive", alert_id=alert_id)]

    def conclude(messages):
        results = tool_results(messages)
        seen["results"] = results
        return [tool_use("submit_assessment", **ASSESSMENT)]

    llm = ScriptedLLM(investigate, conclude)
    events = []
    async with running_agent(settings, tms, store, llm=llm, events=events) as agent:
        state = await agent.run_cycle(1)

    assert "deviation_persistent" in seen["prompt"]
    snapshot, explain, forbidden, call = seen["results"]
    assert not snapshot.get("is_error") and json.loads(snapshot["content"])["deviation"]["current_offset_km"] == 6.4
    explained = json.loads(explain["content"])
    assert any(r["rule_id"] == "deviation_persistent" and r["triggered"] for r in explained["state_rules"])
    assert forbidden["is_error"] and "non autorisé" in forbidden["content"]  # les alertes naissent des règles
    assert not call.get("is_error") and json.loads(call["content"])["placed"]

    alert = store.list_alerts()[0]
    assert alert["analysis"]["summary"] == ASSESSMENT["summary"] and alert["analysis"]["status"] == "complete"
    assert alert["analysis"]["tool_calls"] == ["get_mission_snapshot", "explain_rules_for_mission", "create_alert", "call_driver"]
    assert state["summary"]["analyses"] == 1
    assert len(store.list_alerts()) == 1  # aucune alerte improvisée
    assert any(k == "investigation_done" for k, _ in events)


async def test_tool_results_returned_in_one_message_and_thinking_preserved(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    llm = ScriptedLLM(
        lambda m: [thinking, tool_use("get_mission_eta", mission_id="msn_1"), tool_use("get_mission_details", mission_id="nope")],
        lambda m: [tool_use("submit_assessment", **ASSESSMENT)],
    )
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    second_request = llm.requests[1]
    assert second_request[1]["content"][0] == thinking  # bloc de réflexion renvoyé tel quel
    results = tool_results(second_request)
    assert len(results) == 2 and second_request[-1]["role"] == "user"
    assert results[1]["is_error"] and "introuvable" in results[1]["content"]


async def test_llm_failure_keeps_alerting(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    llm = ScriptedLLM(LlmError("API injoignable"))
    async with running_agent(settings, tms, store, llm=llm) as agent:
        state = await agent.run_cycle(1)
    assert state["summary"]["alerts_emitted"] == 1  # l'alerte et les notifications sont indépendantes du LLM
    assert state["analyses"][0]["status"] == "failed"
    assert store.list_logs(type="investigation_failed")


async def test_max_turns_without_submit_falls_back_to_text(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    settings = settings.model_copy(update={"llm_max_turns": 2})
    llm = ScriptedLLM(lambda m: [tool_use("get_mission_eta", mission_id="msn_1")],
                      lambda m: [{"type": "text", "text": "Dérive probable, données insuffisantes."}])
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    analysis = store.list_alerts()[0]["analysis"]
    assert analysis["status"] == "partial" and "Dérive probable" in analysis["summary"]


async def test_offline_mode_skips_investigation(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    events = []
    async with running_agent(settings, tms, store, events=events) as agent:
        state = await agent.run_cycle(1)
    assert state["summary"]["alerts_emitted"] == 1 and "analyses" not in state
    assert ("llm_disabled", {"reason": "test hors ligne", "retry": False}) in events


async def test_assistant_multi_turn_memory_and_dangling_tool_use(settings, tms, store):
    server = create_server(ServerSettings(), connector=tms, store=store)
    llm = ScriptedLLM(
        lambda m: [tool_use("get_operations_overview")],
        lambda m: [{"type": "text", "text": "1 mission en cours, aucune alerte."}],
        LlmError("coupure"),
        lambda m: [{"type": "text", "text": "Toujours rien à signaler."}],
    )
    async with McpGateway(server) as gateway:
        assistant = OperatorAssistant(settings, gateway, llm, RuleBook(settings.rules_dir))
        try:
            first = await assistant.ask("Quelle est la situation ?")
            assert first.text == "1 mission en cours, aucune alerte." and first.tool_calls == ["get_operations_overview"]
            failed = await assistant.ask("Et maintenant ?")
            assert "incomplète" in failed.text
            third = await assistant.ask("Et maintenant ?")
            assert third.text == "Toujours rien à signaler."
        finally:
            await assistant.close()
    history = llm.requests[-1]
    assert history[0]["content"] == "Quelle est la situation ?"  # mémoire de la conversation
    assert sum(1 for m in history if m["role"] == "assistant") == 2
    tools = [d["name"] for d in (await _toolbox_names(settings, tms, store))]
    assert "create_alert" not in tools and "notify_recipients" not in tools and "resolve_alert" in tools


async def _toolbox_names(settings, tms, store):
    from tms_agent.tools import RULE_DRIVEN_TOOLS, build_toolbox
    server = create_server(ServerSettings(), connector=tms, store=store)
    async with McpGateway(server) as gateway:
        toolbox = await build_toolbox(gateway, RuleBook(settings.rules_dir), deny=RULE_DRIVEN_TOOLS)
        defs = toolbox.definitions()
    assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)  # ordre stable (cache de prompt)
    return defs
