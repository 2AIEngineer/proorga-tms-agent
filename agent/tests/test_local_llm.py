"""Fournisseur local (API compatible OpenAI / vLLM) et adaptations aux petits modèles."""

import json

import httpx
import openai
import pytest
from tms_agent.llm import INVALID_ARGUMENTS, LlmError
from tms_agent.llm.openai_compat import OpenAICompatibleClient, fit_context, to_openai_messages
from tms_agent.rules_engine import mission_facts
from tms_agent.tools import COMPACT_INVESTIGATION_TOOLS, ToolInputError, validate_assessment
from tms_mcp.testing import FakeTmsConnector, make_mission

from tests.conftest import ScriptedLLM, running_agent, tool_use

# --- Traduction du format interne -------------------------------------------------------


def test_messages_translation_roundtrip_shapes():
    messages = [
        {"role": "user", "content": "Situation ?"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "je réfléchis"},
            {"type": "text", "text": "Je regarde."},
            {"type": "tool_use", "id": "c1", "name": "get_mission_eta", "input": {"mission_id": "m1"}},
            {"type": "tool_use", "id": "c2", "name": "oops", "input": {INVALID_ARGUMENTS: "{bad"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{\"delay_minutes\": 12}"},
            {"type": "tool_result", "tool_use_id": "c2", "content": "JSON invalide", "is_error": True},
            {"type": "text", "text": "Et la dérive ?"},
        ]},
    ]
    out = to_openai_messages("SYS", messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool", "user"]
    assistant = out[2]
    assert assistant["content"] == "Je regarde."  # raisonnement non renvoyé
    assert [c["function"]["name"] for c in assistant["tool_calls"]] == ["get_mission_eta", "oops"]
    assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {}
    assert out[4]["content"].startswith("ERREUR")
    assert out[5] == {"role": "user", "content": "Et la dérive ?"}


def test_fit_context_drops_oldest_tool_results_only():
    big = "x" * 3000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "demande"},
            {"role": "tool", "tool_call_id": "a", "content": big}, {"role": "tool", "tool_call_id": "b", "content": big},
            {"role": "tool", "tool_call_id": "c", "content": big}]
    fitted, removed = fit_context(msgs, budget_tokens=2200, chars_per_token=3.0)
    assert removed == 1 and "retiré" in fitted[2]["content"]
    assert fitted[3]["content"] == big and fitted[4]["content"] == big and fitted[1]["content"] == "demande"
    assert msgs[2]["content"] == big  # l'original n'est pas modifié


# --- Client local contre un serveur compatible OpenAI simulé -------------------------------


def local_client(settings, handler) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient(settings)
    client._client = openai.AsyncOpenAI(base_url="http://vllm/v1", api_key="EMPTY", max_retries=0,
                                        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return client


def models_response():
    return httpx.Response(200, json={"object": "list", "data": [
        {"id": "Qwen/Qwen3.5-2B", "object": "model", "created": 0, "owned_by": "vllm", "max_model_len": 8192}]})


def completion(message: dict, finish: str = "stop", prompt_tokens: int = 100) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "Qwen/Qwen3.5-2B",
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20, "total_tokens": prompt_tokens + 20},
    })


async def test_probe_reads_context_window_and_model(settings):
    client = local_client(settings, lambda r: models_response())
    assert await client.probe() and client.context_window == 8192

    other = local_client(settings.model_copy(update={"local_model": "absent"}), lambda r: models_response())
    assert not await other.probe() and other.recoverable and "non servi" in other.disabled_reason


async def test_probe_server_down_is_recoverable(settings):
    def down(request):
        raise httpx.ConnectError("refused")

    client = local_client(settings, down)
    assert not await client.probe()
    assert client.recoverable and "injoignable" in client.disabled_reason


async def test_complete_parses_tool_calls_reasoning_and_thinking_switch(settings):
    sent = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return models_response()
        sent.append(json.loads(request.content))
        return completion({"content": None, "reasoning": "Il faut l'ETA.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_mission_eta", "arguments": "{\"mission_id\": \"m1\"}"}},
            {"id": "call_2", "type": "function", "function": {"name": "get_mission_deviation", "arguments": "{pas du json"}},
        ]}, finish="stop")  # certains serveurs renvoient "stop" avec des tool_calls

    client = local_client(settings, handler)
    await client.probe()
    tools = [{"name": "get_mission_eta", "description": "ETA", "input_schema": {"type": "object", "properties": {}}}]
    turn = await client.complete(system="S", messages=[{"role": "user", "content": "ETA ?"}], tools=tools, effort="high")
    assert turn.stop_reason == "tool_use" and turn.thinking == ["Il faut l'ETA."]
    assert turn.tool_uses[0]["input"] == {"mission_id": "m1"}
    assert INVALID_ARGUMENTS in turn.tool_uses[1]["input"]
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": True}  # effort high → raisonnement
    assert sent[0]["tools"][0]["function"]["name"] == "get_mission_eta"

    await client.complete(system="S", messages=[{"role": "user", "content": "?"}], tools=tools, effort="medium")
    assert sent[1]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_complete_errors_become_llm_errors(settings):
    def handler(request):
        if request.url.path.endswith("/models"):
            return models_response()
        return httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})

    client = local_client(settings, handler)
    await client.probe()
    with pytest.raises(LlmError, match="context length"):
        await client.complete(system="S", messages=[{"role": "user", "content": "?"}], tools=[], effort="low")


# --- Adaptations aux petits modèles ------------------------------------------------------


def test_assessment_validation_is_tolerant_but_strict_on_essentials():
    a = validate_assessment({"summary": "Dérive confirmée.", "risk_level": "Élevé", "confidence": "80%",
                             "recommended_actions": "- Appeler le chauffeur\n- Prévenir le client"})
    assert a.risk_level == "high" and a.confidence == 0.8
    assert a.recommended_actions == ["Appeler le chauffeur", "Prévenir le client"]
    with pytest.raises(ToolInputError, match="risk_level"):
        validate_assessment({"summary": "Dérive confirmée.", "risk_level": "inconnu"})


def test_mission_facts_are_unambiguous():
    tms = FakeTmsConnector()
    tms.add_mission(make_mission("m1"), offset_km=6.4, offset_minutes=27, delay_minutes=-7.5)
    import asyncio
    snapshot = {"tms_time": tms.now_iso(), "mission": asyncio.run(tms.get_mission("m1")),
                "vehicle": tms.vehicles["veh_1"], "deviation": tms.deviations["m1"], "eta": tms.etas["m1"],
                "driver": asyncio.run(tms.get_driver("drv_77")), "errors": {}}
    facts = "\n".join(mission_facts(snapshot))
    assert "AVANCE de 7.5 min" in facts and "cap 45° (direction de la route, pas une vitesse)" in facts
    assert "6.4 km depuis 27 min" in facts and "il y a 65 min" in facts


ASSESSMENT = {"summary": "Dérive confirmée de 6,4 km.", "risk_level": "high", "recommended_actions": ["Appeler"]}


async def test_nudge_then_compact_toolset_and_facts(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    llm = ScriptedLLM(
        lambda m: [{"type": "text", "text": "La dérive est confirmée."}],  # conclut en texte libre
        lambda m: [tool_use("submit_assessment", **ASSESSMENT)],          # après relance
        context_window=8192,
    )
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    assert set(llm.tools_seen[0]) <= COMPACT_INVESTIGATION_TOOLS and "get_mission_route" not in llm.tools_seen[0]
    assert "Faits clés" in llm.requests[0][0]["content"] and "6.4 km" in llm.requests[0][0]["content"]
    assert "submit_assessment" in llm.requests[1][-1]["content"]  # consigne de relance
    assert llm.forced == [None, "submit_assessment"]  # appel forcé après la relance
    analysis = store.list_alerts()[0]["analysis"]
    assert analysis["status"] == "complete" and analysis["risk_level"] == "high"
    assert analysis["probable_cause"] == "indéterminée"  # champ secondaire complété par défaut


async def test_invalid_assessment_is_sent_back_for_correction(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    seen = {}

    def corrected(messages):
        seen["error"] = messages[-1]["content"][0]
        return [tool_use("submit_assessment", **ASSESSMENT)]

    llm = ScriptedLLM(lambda m: [tool_use("submit_assessment", summary="ok ok", risk_level="???")], corrected)
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    assert seen["error"]["is_error"] and "risk_level" in seen["error"]["content"]
    assert store.list_alerts()[0]["analysis"]["status"] == "complete"


async def test_background_investigations_do_not_block_detection(settings, tms, store):
    import asyncio
    settings = settings.model_copy(update={"background_investigations": True})
    release = asyncio.Event()

    class SlowLLM(ScriptedLLM):
        async def complete(self, **kw):
            self.requests.append(kw["messages"])
            await release.wait()
            return await super().complete(**kw)

    tms.set_deviation("msn_1", 6.4, 27)
    llm = SlowLLM(lambda m: [tool_use("submit_assessment", **ASSESSMENT)])
    async with running_agent(settings, tms, store, llm=llm) as agent:
        state = await agent.run_cycle(1)  # rend la main malgré l'enquête bloquée
        assert state["summary"]["alerts_emitted"] == 1 and state["summary"]["investigations_queued"] == 1
        assert agent.pending_investigations == 1 and store.list_alerts()[0]["analysis"] is None
        second = await agent.run_cycle(2)
        assert second["summary"]["fatal"] is None
        release.set()
        await agent.wait_investigations(5)
        assert agent.pending_investigations == 0
        assert store.list_alerts()[0]["analysis"]["risk_level"] == "high"


async def test_llm_reprobed_after_server_restart(settings, tms, store):
    settings = settings.model_copy(update={"llm_reprobe_every_cycles": 2})
    events = []

    class RestartingLLM(ScriptedLLM):
        probes = 0

        async def probe(self):
            RestartingLLM.probes += 1
            up = RestartingLLM.probes > 1
            self.disabled_reason = None if up else "serveur LLM injoignable"
            self.recoverable = not up
            return up

        @property
        def available(self):
            return self.disabled_reason is None

    llm = RestartingLLM()
    async with running_agent(settings, tms, store, llm=llm, events=events) as agent:
        assert agent.investigator is None
        await agent.run_cycle(1)
        await agent.run_cycle(2)
        assert agent.investigator is not None
    kinds = [k for k, _ in events]
    assert kinds.index("llm_disabled") < kinds.index("llm_ready")


async def test_context_overflow_recalibrates_and_retries(settings):
    calls = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return models_response()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": (
                "This model's maximum context length is 8192 tokens. However, you requested 2048 output tokens "
                "and your prompt contains at least 6145 input tokens, for a total of at least 8193 tokens.")}})
        # Compte réel du serveur : 2 caractères par token (tokenizer moins efficace que prévu).
        return completion({"content": "ok"}, prompt_tokens=len(request.content) // 2)

    client = local_client(settings, handler)
    await client.probe()
    history = [{"role": "user", "content": "demande"}]
    for i in range(6):
        history += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"c{i}", "name": "t", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "x" * 2500}]},
        ]
    before = client.chars_per_token
    turn = await client.complete(system="S", messages=history, tools=[], effort="low")
    assert turn.text == "ok" and len(calls) == 2
    assert client.chars_per_token < before  # recalibré sur le compte réel
    assert calls[1]["max_tokens"] < calls[0]["max_tokens"]
    retired = sum(1 for m in calls[1]["messages"] if m["role"] == "tool" and "retiré" in m["content"])
    assert retired > sum(1 for m in calls[0]["messages"] if m["role"] == "tool" and "retiré" in m["content"])


async def test_forced_tool_uses_named_tool_choice_without_thinking(settings):
    sent = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return models_response()
        sent.append(json.loads(request.content))
        return completion({"content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "submit_assessment", "arguments": "{}"}}]})

    client = local_client(settings, handler)
    await client.probe()
    tools = [{"name": "submit_assessment", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    await client.complete(system="S", messages=[{"role": "user", "content": "?"}], tools=tools, effort="high",
                          force_tool="submit_assessment")
    assert sent[0]["tool_choice"] == {"type": "function", "function": {"name": "submit_assessment"}}
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_calibration_is_bounded(settings):
    client = OpenAICompatibleClient(settings)
    for _ in range(20):
        client._calibrate(sent_chars=100_000, prompt_tokens=10)  # usage aberrant
    assert client.chars_per_token == 4.0
    for _ in range(20):
        client._calibrate(sent_chars=1000, prompt_tokens=1000)
    assert 1.2 <= client.chars_per_token < 1.5


async def test_last_turn_forces_conclusion(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    settings = settings.model_copy(update={"llm_max_turns": 3})
    reads = [lambda m: [tool_use("get_mission_eta", mission_id="msn_1")]] * 2
    llm = ScriptedLLM(*reads, lambda m: [tool_use("submit_assessment", **ASSESSMENT)])
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    assert llm.forced == [None, None, "submit_assessment"]
    assert store.list_alerts()[0]["analysis"]["status"] == "complete"


async def test_truncated_output_triggers_forced_conclusion(settings, tms, store):
    from tms_agent.llm import ModelTurn
    tms.set_deviation("msn_1", 6.4, 27)

    class TruncatingLLM(ScriptedLLM):
        async def complete(self, **kw):
            if not self.requests:
                self.requests.append(kw["messages"])
                self.forced.append(kw.get("force_tool"))
                return ModelTurn(content=[{"type": "thinking", "thinking": "..." * 100}], stop_reason="max_tokens",
                                 text="", tool_uses=[])
            return await super().complete(**kw)

    llm = TruncatingLLM(lambda m: [tool_use("submit_assessment", **ASSESSMENT)])
    async with running_agent(settings, tms, store, llm=llm) as agent:
        await agent.run_cycle(1)
    assert llm.forced == [None, "submit_assessment"]
    assert store.list_alerts()[0]["analysis"]["status"] == "complete"
