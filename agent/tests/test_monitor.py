"""Boucle de surveillance de bout en bout : faux TMS → serveur MCP en mémoire → graphe LangGraph."""

from tms_mcp.testing import make_mission, ts

from tests.conftest import running_agent


async def test_nominal_mission_raises_nothing(settings, tms, store):
    async with running_agent(settings, tms, store) as agent:
        state = await agent.run_cycle()
    assert state["summary"]["missions"] == 1
    assert state["summary"]["alerts_emitted"] == 0 and state["summary"]["errors"] == 0
    assert store.list_alerts() == []


async def test_deviation_alert_notified_deduplicated_then_resolved(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    async with running_agent(settings, tms, store) as agent:
        first = await agent.run_cycle(1)
        assert first["summary"]["alerts_emitted"] == 1
        alert = store.list_alerts()[0]
        assert alert["rule_id"] == "deviation_persistent" and alert["severity"] == "high"
        assert alert["observed_data"]["deviation.current_offset_km"] == 6.4
        assert "PL-veh_1" in alert["message"]
        channels = {(n["recipient_role"], n["channel"], n["status"]) for n in store.list_notifications(alert["id"])}
        assert channels == {("driver", "call", "delivered"), ("ops_agent", "in_app", "delivered"),
                            ("ops_supervisor", "sms", "delivered")}

        tms.advance(5)
        second = await agent.run_cycle(2)
        assert second["summary"]["alerts_emitted"] == 0 and second["summary"]["alerts_suppressed"] == 1
        assert "ouverte" in second["suppressed"][0]["reason"]

        tms.set_deviation("msn_1", 0.3, 0)  # le camion a rejoint l'itinéraire
        third = await agent.run_cycle(3)
        assert third["summary"]["alerts_resolved"] == 1
        assert store.get_alert(alert["id"])["status"] == "resolved"

        tms.advance(10)
        tms.set_deviation("msn_1", 7.0, 25)  # nouvelle dérive pendant le cooldown (30 min)
        fourth = await agent.run_cycle(4)
        assert fourth["summary"]["alerts_emitted"] == 0 and fourth["suppressed"][0]["reason"].startswith("cooldown")


async def test_event_rules_fire_once_per_event(settings, tms, store):
    async with running_agent(settings, tms, store) as agent:
        await agent.run_cycle(1)
        tms.add_event("msn_1", {"id": "evt_crit", "type": "driver_message", "occurred_at": ts(118),
                                "position": {"lat": 33.7, "lon": -7.3},
                                "payload": {"severity": "critical", "text": "Accrochage, pas de blessé"}})
        state = await agent.run_cycle(2)
        assert state["summary"]["alerts_emitted"] == 1
        alert = store.list_alerts()[0]
        assert alert["rule_id"] == "critical_incident" and alert["event_id"] == "evt_crit"
        assert "Accrochage" in alert["message"]
        assert {n["management_level"] for n in store.list_notifications(alert["id"])} == {"M+1", "M+2", "M+3", "M+4"}
        again = await agent.run_cycle(3)
        assert again["summary"]["rules_triggered"] == 0  # événement déjà traité


async def test_event_retried_when_alert_cannot_be_stored(settings, tms, store, monkeypatch):
    tms.add_event("msn_1", {"id": "evt_far", "type": "arrived_delivery", "occurred_at": ts(119),
                            "position": {"lat": 34.0, "lon": -6.80}})
    async with running_agent(settings, tms, store) as agent:
        original = agent.gateway.call

        async def flaky(name, args=None):
            if name == "create_alert":
                from tms_agent.mcp_gateway import McpUnavailable
                raise McpUnavailable("serveur MCP coupé")
            return await original(name, args)

        monkeypatch.setattr(agent.gateway, "call", flaky)
        failed = await agent.run_cycle(1)
        assert failed["summary"]["alerts_emitted"] == 0 and failed["act_errors"]
        monkeypatch.setattr(agent.gateway, "call", original)
        retried = await agent.run_cycle(2)
        assert retried["summary"]["alerts_emitted"] == 1
        assert store.list_alerts()[0]["rule_id"] == "arrival_out_of_zone"


async def test_immobilization_and_mission_completion(settings, tms, store):
    tms.add_mission(make_mission("msn_2", vehicle_id="veh_2", driver_id="drv_78"), vehicle_status="stopped")
    tms.advance(70)  # dernier événement à T+55 → 135 min sans événement
    async with running_agent(settings, tms, store) as agent:
        state = await agent.run_cycle(1)
        rules = sorted(a["rule_id"] for a in store.list_alerts())
        assert rules == ["immobilization_anomaly"] and state["summary"]["alerts_emitted"] == 1
        immob = store.list_alerts()[0]
        assert immob["mission_id"] == "msn_2"
        tms.missions["msn_2"]["status"] = "completed"
        done = await agent.run_cycle(2)
        assert done["resolved"][0]["reason"] == "mission completed"


async def test_tms_outage_is_reported_not_raised(settings, tms, store):
    tms.fail.add("list_missions")
    async with running_agent(settings, tms, store) as agent:
        state = await agent.run_cycle(1)
        assert "lecture des missions impossible" in state["summary"]["fatal"]
        tms.fail.clear()
        assert (await agent.run_cycle(2))["summary"]["fatal"] is None


async def test_partial_snapshot_does_not_resolve_alerts(settings, tms, store):
    tms.set_deviation("msn_1", 6.4, 27)
    async with running_agent(settings, tms, store) as agent:
        await agent.run_cycle(1)
        tms.fail.add("get_mission_deviation")  # écart illisible : on ne conclut pas à une résorption
        state = await agent.run_cycle(2)
        assert state["summary"]["alerts_resolved"] == 0
        assert store.list_alerts(status="open")


async def test_rules_hot_reload(settings, tms, store, tmp_path):
    import shutil
    rules_dir = tmp_path / "rules"
    shutil.copytree(settings.rules_dir, rules_dir)
    settings = settings.model_copy(update={"rules_dir": rules_dir})
    tms.set_deviation("msn_1", 4.0, 27)  # sous le seuil de 5 km
    async with running_agent(settings, tms, store) as agent:
        assert (await agent.run_cycle(1))["summary"]["alerts_emitted"] == 0
        path = rules_dir / "deviation_persistent.yaml"
        path.write_text(path.read_text().replace("value: 5\n", "value: 3\n").replace("version: 2", "version: 3"))
        state = await agent.run_cycle(2)
        assert state["summary"]["alerts_emitted"] == 1
        assert store.list_alerts()[0]["rule_version"] == 3
        path.write_text("id: cassée\n")  # règle invalide : l'ancien jeu reste actif
        assert (await agent.run_cycle(3))["summary"]["fatal"] is None
        assert agent.rulebook.last_error
