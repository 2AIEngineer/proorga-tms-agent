from tests.conftest import call, call_error, open_client

EXPECTED_TOOLS = {
    "get_active_missions", "get_mission_details", "get_mission_events", "get_vehicle_position", "get_mission_route",
    "get_mission_deviation", "get_mission_eta", "get_driver_details", "log_agent_action", "list_alerts", "call_driver",
    "notify_recipients", "list_alert_notifications", "create_alert", "get_mission_snapshot", "resolve_alert",
}


async def test_catalogue_is_semantic(tms, store):
    async with open_client(tms, store) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert EXPECTED_TOOLS <= tools.keys()
        for tool in tools.values():
            assert tool.description and len(tool.description) > 80, tool.name  # intention métier, pas une paraphrase
            assert tool.meta["backend"] in ("tms", "agent")
            assert tool.output_schema is not None
            for name, prop in tool.input_schema.get("properties", {}).items():
                assert prop.get("description"), f"{tool.name}.{name} non documenté"
        tms_tools = [t for t in tools.values() if t.meta["backend"] == "tms"]
        assert all(t.annotations.read_only_hint for t in tms_tools)


async def test_active_missions_and_snapshot(tms, store):
    async with open_client(tms, store) as client:
        active = await call(client, "get_active_missions")
        assert active["count"] == 2 and active["tms_time"] == tms.now_iso()
        snap = await call(client, "get_mission_snapshot", {"mission_id": "msn_1"})
        assert snap["deviation"]["current_offset_km"] == 6.4
        assert snap["vehicle"]["status"] == "moving" and snap["driver"]["id"] == "drv_77"
        assert snap["errors"] == {}


async def test_snapshot_is_partial_when_a_resource_fails(tms, store):
    async with open_client(tms, store) as client:
        tms.fail.add("get_mission_eta")
        snap = await call(client, "get_mission_snapshot", {"mission_id": "msn_1"})
        assert snap["eta"] is None and "eta" in snap["errors"]
        assert snap["deviation"] is not None


async def test_tms_errors_are_readable(tms, store):
    async with open_client(tms, store) as client:
        assert "introuvable" in await call_error(client, "get_mission_details", {"mission_id": "nope"})
        tms.fail.add("list_missions")
        assert "indisponible" in await call_error(client, "get_active_missions")
        assert "ISO 8601" in await call_error(client, "get_mission_events", {"mission_id": "msn_1", "since": "hier"})


async def test_alert_lifecycle_with_notifications(tms, store):
    async with open_client(tms, store) as client:
        args = {"rule_id": "deviation_persistent", "rule_version": 2, "severity": "high", "title": "Dérive",
                "mission_id": "msn_1", "deduplication_key": "deviation:msn_1", "cooldown_minutes": 30,
                "observed_data": {"deviation.current_offset_km": 6.4}}
        created = await call(client, "create_alert", args)
        assert created["created"]
        alert_id = created["alert"]["id"]
        duplicate = await call(client, "create_alert", args)
        assert not duplicate["created"] and duplicate["alert"]["id"] == alert_id

        sent = await call(client, "notify_recipients", {"alert_id": alert_id, "recipients": [
            {"role": "driver", "management_level": "M+0", "channel": "call"},
            {"role": "ops_agent", "management_level": "M+1", "channel": "in_app"},
            {"role": "director", "management_level": "M+4", "channel": "email"},  # pas d'e-mail → échec tracé
            {"role": "ghost_role", "management_level": "M+9", "channel": "sms"},
        ]})
        by_role = {n["recipient_role"]: n for n in sent["notifications"]}
        assert by_role["driver"]["destination"] == "+212600000077" and by_role["driver"]["status"] == "delivered"
        assert by_role["ops_agent"]["recipient_user_id"] == "usr_agent_12"
        assert by_role["director"]["status"] == "failed"
        assert by_role["ghost_role"]["status"] == "failed"
        assert (sent["delivered"], sent["failed"]) == (2, 2)

        detail = await call(client, "get_alert", {"alert_id": alert_id})
        assert len(detail["notifications"]) == 4
        resolved = await call(client, "resolve_alert", {"alert_id": alert_id, "reason": "écart résorbé"})
        assert resolved["status"] == "resolved"
        types = [e["type"] for e in (await call(client, "list_agent_actions", {"mission_id": "msn_1"}))["entries"]]
        assert {"alert_emitted", "notification_sent", "alert_resolved"} <= set(types)


async def test_notify_rejects_invalid_recipient(tms, store):
    async with open_client(tms, store) as client:
        created = await call(client, "create_alert", {"rule_id": "r", "rule_version": 1, "severity": "low", "title": "t"})
        msg = await call_error(client, "notify_recipients", {"alert_id": created["alert"]["id"], "recipients": [
            {"role": "ops_agent", "user_id": "usr_agent_12", "management_level": "M+1", "channel": "in_app"}]})
        assert "exactement" in msg


async def test_call_driver_guardrails(tms, store):
    async with open_client(tms, store) as client:
        refused = await call_error(client, "call_driver", {"mission_id": "msn_1", "reason": "Point de situation"})
        assert "refusé" in refused
        await call(client, "create_alert", {"rule_id": "r", "rule_version": 1, "severity": "high", "title": "t", "mission_id": "msn_1"})
        first = await call(client, "call_driver", {"mission_id": "msn_1", "reason": "Point de situation"})
        assert first["placed"] and first["call"]["phone"] == "+212600000077"
        second = await call(client, "call_driver", {"mission_id": "msn_1", "reason": "Point de situation"})
        assert not second["placed"]
        tms.advance(20)
        assert (await call(client, "call_driver", {"mission_id": "msn_1", "reason": "Nouveau point"}))["placed"]


async def test_call_driver_without_phone(tms, store):
    async with open_client(tms, store) as client:
        await call(client, "create_alert", {"rule_id": "r", "rule_version": 1, "severity": "critical", "title": "t", "mission_id": "msn_2"})
        assert "téléphone" in await call_error(client, "call_driver", {"mission_id": "msn_2", "reason": "Point de situation"})


async def test_overview(tms, store):
    async with open_client(tms, store) as client:
        await call(client, "create_alert", {"rule_id": "r", "rule_version": 1, "severity": "medium", "title": "t", "mission_id": "msn_1"})
        overview = await call(client, "get_operations_overview")
        assert overview["active_missions"] == 2 and overview["open_alerts_by_severity"] == {"medium": 1}
        by_id = {m["mission_id"]: m for m in overview["missions"]}
        assert overview["missions"][0]["mission_id"] == "msn_1"  # missions avec alertes ouvertes en tête
        assert by_id["msn_1"]["open_alerts"] == ["r"] and by_id["msn_1"]["offset_km"] == 6.4
        assert by_id["msn_1"]["delivery_status"] == "avance de 5 min"
        assert by_id["msn_2"]["vehicle_status"] == "stopped"
