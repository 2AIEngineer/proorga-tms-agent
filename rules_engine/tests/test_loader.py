import textwrap
from pathlib import Path

import pytest

from rules_engine import RuleLoadError, load_rules, parse_rules

RULES_DIR = Path(__file__).parent.parent / "rules"

VALID = """
id: sample
version: 1
severity: low
category: delay
description: Exemple.
when:
  field: eta.delay_minutes
  operator: greater_than
  value: 10
then:
  action: log_only
"""


def _parse(text: str):
    return parse_rules(textwrap.dedent(text), "regle.yaml")


def test_poc_rules_load():
    rules = load_rules(RULES_DIR)
    assert {r.id for r in rules} == {
        "deviation_persistent", "eta_delay", "immobilization_anomaly",
        "data_gap_moving", "arrival_out_of_zone", "critical_incident",
    }
    scoped = {r.id for r in rules if r.event_scoped}
    assert scoped == {"arrival_out_of_zone", "critical_incident"}


def test_single_leaf_and_defaults():
    [rule] = _parse(VALID)
    assert rule.enabled and rule.then.notify == [] and not rule.event_scoped


def test_multiple_rules_per_file():
    text = VALID + "---\n" + VALID.replace("id: sample", "id: other")
    assert [r.id for r in _parse(text)] == ["sample", "other"]


@pytest.mark.parametrize(
    "change, message",
    [
        (("operator: greater_than", "operator: plus_grand_que"), "opérateur inconnu 'plus_grand_que'"),
        (("field: eta.delay_minutes", "field: eat.delay_minutes"), "champ 'eat.delay_minutes' invalide"),
        (("value: 10", "value: dix"), "`value` doit être un nombre"),
        (("severity: low", "severity: urgent"), "severity"),
        (("operator: greater_than", "operater: greater_than"), "operater"),  # faute de frappe
        (("action: log_only", "action: emit_alert"), "emit_alert nécessite un bloc `alert`"),
        (("id: sample", "id: Sample-Rule"), "id"),
    ],
)
def test_invalid_rules_report_readable_errors(change, message):
    with pytest.raises(RuleLoadError) as exc:
        _parse(VALID.replace(*change))
    assert str(exc.value).startswith("regle.yaml")
    assert message in str(exc.value)


def test_recipient_requires_role_or_user():
    text = VALID.replace("action: log_only", """action: emit_alert
  alert: { title: T }
  notify:
    - { role: ops_agent, user_id: usr_1, management_level: M+1, channel: sms }""")
    with pytest.raises(RuleLoadError, match="exactement l'un des champs"):
        _parse(text)


def test_duplicate_ids_across_files(tmp_path):
    (tmp_path / "a.yaml").write_text(VALID)
    (tmp_path / "b.yaml").write_text(VALID)
    with pytest.raises(RuleLoadError, match="déjà définie"):
        load_rules(tmp_path)


def test_invalid_yaml(tmp_path):
    (tmp_path / "a.yaml").write_text("id: [unclosed")
    with pytest.raises(RuleLoadError, match="YAML invalide"):
        load_rules(tmp_path)
