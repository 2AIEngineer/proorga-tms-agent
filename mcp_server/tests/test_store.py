from datetime import timedelta

from tms_mcp.store import AgentStore
from tms_mcp.testing import T0

KW = {"rule_id": "r", "rule_version": 1, "severity": "high", "title": "t", "mission_id": "m", "deduplication_key": "k",
      "cooldown_minutes": 30}


def test_deduplication_while_open(store: AgentStore):
    first = store.create_alert(now=T0, **KW)
    second = store.create_alert(now=T0 + timedelta(minutes=90), **KW)
    assert first.created and not second.created
    assert second.alert["id"] == first.alert["id"]
    assert "ouverte" in second.reason


def test_cooldown_after_resolution(store: AgentStore):
    first = store.create_alert(now=T0, **KW)
    store.resolve_alert(first.alert["id"], now=T0 + timedelta(minutes=5), reason="ok")
    blocked = store.create_alert(now=T0 + timedelta(minutes=10), **KW)
    assert not blocked.created and "cooldown" in blocked.reason
    again = store.create_alert(now=T0 + timedelta(minutes=31), **KW)
    assert again.created


def test_clock_reset_does_not_block(store: AgentStore):
    first = store.create_alert(now=T0, **KW)
    store.resolve_alert(first.alert["id"], now=T0, reason="ok")
    assert store.create_alert(now=T0 - timedelta(days=1), **KW).created  # nouvelle simulation, horloge remise à zéro


def test_no_key_never_deduplicated(store: AgentStore):
    kw = {**KW, "deduplication_key": None}
    assert store.create_alert(now=T0, **kw).created
    assert store.create_alert(now=T0, **kw).created


def test_json_roundtrip_and_logs(store: AgentStore):
    alert = store.create_alert(now=T0, observed_data={"deviation.current_offset_km": 6.4}, **KW).alert
    assert alert["observed_data"] == {"deviation.current_offset_km": 6.4}
    store.annotate_alert(alert["id"], {"summary": "s"})
    assert store.get_alert(alert["id"])["analysis"] == {"summary": "s"}
    store.add_log(now=T0, type="x", mission_id="m", payload={"a": 1})
    assert store.list_logs(mission_id="m")[0]["payload"] == {"a": 1}
    assert store.alert_stats()["by_severity_open"] == {"high": 1}
