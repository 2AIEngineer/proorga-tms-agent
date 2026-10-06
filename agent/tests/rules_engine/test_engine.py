from datetime import UTC, datetime, timedelta
from pathlib import Path

from tms_agent.rules_engine import (
    InMemoryAlertHistory,
    RuleEngine,
    apply_deduplication,
    build_context,
    parse_rules,
)
from tms_agent.rules_engine.scenarios import run_scenarios
from tms_agent.rules_engine.templating import render

ROOT = Path(__file__).parent.parent.parent
NOW = datetime(2025, 11, 4, 9, 42, tzinfo=UTC)

MISSION = {
    "id": "msn_8f3a",
    "reference": "MIS-2025-00412",
    "status": "in_progress",
    "origin": {"label": "Entrepôt Casablanca", "lat": 33.5731, "lon": -7.5898},
    "destination": {"label": "Client Rabat", "lat": 34.0209, "lon": -6.8416},
    "planned_delivery_at": "2025-11-04T12:30:00Z",
    "events": [
        {"id": "evt_1", "type": "arrived_pickup", "occurred_at": "2025-11-04T08:12:00Z", "position": None, "payload": {}},
        {"id": "evt_2", "type": "departed_pickup", "occurred_at": "2025-11-04T09:00:00Z", "position": None, "payload": {}},
    ],
}
VEHICLE = {"id": "veh_201", "plate": "AB-123-CD", "status": "moving",
           "current_position": {"lat": 33.61, "lon": -7.52, "speed_kmh": 64}, "last_seen_at": "2025-11-04T09:41:30Z"}
DEVIATION = {"current_offset_km": 6.4, "current_offset_since": "2025-11-04T09:15:00Z", "duration_minutes": 27,
             "severity": "medium", "vehicle_position": {"lat": 33.61, "lon": -7.52}, "computed_at": "2025-11-04T09:42:00Z"}


def engine() -> RuleEngine:
    return RuleEngine.from_path(ROOT / "domain" / "rules")


def context(**overrides):
    return build_context(now=NOW, mission=MISSION, vehicle=VEHICLE, deviation=DEVIATION,
                         eta={"planned_delivery_at": "2025-11-04T12:30:00Z", "current_eta": "2025-11-04T12:47:00Z"},
                         **overrides)


def test_context_derived_fields():
    ctx = context()
    assert ctx["mission"]["last_event_type"] == "departed_pickup"
    assert ctx["mission"]["last_event_age_minutes"] == 42
    assert ctx["mission"]["event_count"] == 2 and "events" not in ctx["mission"]
    assert ctx["eta"]["delay_minutes"] == 17
    assert ctx["vehicle"]["speed_kmh"] == 64 and ctx["vehicle"]["last_seen_age_minutes"] == 0.5


def test_document3_alert_format():
    [match] = engine().evaluate(context(), NOW)
    assert match.rule_id == "deviation_persistent" and match.rule_version == 2
    assert match.message == "Le véhicule AB-123-CD de la mission MIS-2025-00412 s'est écarté de 6.4 km depuis 27 minutes."
    assert match.deduplication_key == "deviation:msn_8f3a"
    assert match.observed_data["deviation.vehicle_position"] == {"lat": 33.61, "lon": -7.52}
    assert [(n["role"], n["management_level"], n["channel"]) for n in match.notify] == [
        ("driver", "M+0", "call"), ("ops_agent", "M+1", "in_app"), ("ops_supervisor", "M+2", "sms"),
    ]
    assert all(t.result for t in match.trace)


def test_explain_traces_every_condition_without_short_circuit():
    evaluations = {e.rule_id: e for e in engine().explain(context(), NOW)}
    eta = evaluations["eta_delay"]
    assert not eta.triggered and len(eta.trace) == 2
    assert "critical_incident" not in evaluations  # règle d'événement, pas d'événement dans le contexte


def test_event_rules_are_evaluated_per_new_event():
    events = [
        {"id": "e1", "type": "driver_message", "occurred_at": "2025-11-04T09:40:00Z", "payload": {"severity": "critical", "text": "Accident"}},
        {"id": "e2", "type": "driver_message", "occurred_at": "2025-11-04T09:41:00Z", "payload": {"severity": "critical", "text": "Second"}},
    ]
    matches = engine().evaluate_mission(context(), NOW, events)
    critical = [m for m in matches if m.rule_id == "critical_incident"]
    assert [m.event_id for m in critical] == ["e1", "e2"]
    assert [m.deduplication_key for m in critical] == ["critical:e1", "critical:e2"]
    assert critical[0].message == "Mission MIS-2025-00412 (AB-123-CD) : Accident"
    assert len(critical[0].notify) == 4


def test_logic_operators_and_disabled_rules():
    rules = parse_rules("""
id: complex
version: 1
severity: low
category: test
description: any / not
when:
  all:
    - any:
        - { field: vehicle.status, operator: equals, value: stopped }
        - { field: vehicle.speed_kmh, operator: greater_than, value: 60 }
    - not: { field: mission.status, operator: in, value: [completed, cancelled] }
then: { action: log_only }
---
id: disabled
version: 1
enabled: false
severity: low
category: test
description: désactivée
when: { field: mission.status, operator: equals, value: in_progress }
then: { action: log_only }
""")
    matches = RuleEngine(rules).evaluate(context(), NOW)
    assert [m.rule_id for m in matches] == ["complex"]
    assert matches[0].title is None and matches[0].deduplication_key is None


def test_template_rendering():
    data = {"a": {"x": 6.0, "y": 6.43, "z": None}}
    assert render("{{ a.x }} / {{a.y}} / {{ a.z }} / {{ a.missing }}", data) == "6 / 6.4 / ? / ?"


def test_deduplication_and_cooldown():
    history = InMemoryAlertHistory()
    eng = engine()
    [match] = eng.evaluate(context(), NOW)

    [d] = apply_deduplication([match], history, NOW)
    assert d.emit
    history.record(match.deduplication_key, NOW)

    [d] = apply_deduplication([match], history, NOW + timedelta(minutes=5))
    assert not d.emit and d.reason == "alerte déjà ouverte"

    history.resolve(match.deduplication_key)
    [d] = apply_deduplication([match], history, NOW + timedelta(minutes=10))
    assert not d.emit and d.reason.startswith("cooldown actif (20 min")

    [d] = apply_deduplication([match], history, NOW + timedelta(minutes=31))
    assert d.emit

    decisions = apply_deduplication([match, match], InMemoryAlertHistory(), NOW)
    assert [x.emit for x in decisions] == [True, False]


def test_business_scenarios_all_pass():
    results = run_scenarios(engine(), ROOT / "domain" / "scenarios")
    assert len(results) >= 20
    failed = [(r.file, r.name, r.expected, r.actual, r.error) for r in results if not r.passed]
    assert failed == []
