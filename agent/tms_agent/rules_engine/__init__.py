"""Moteur de règles métier déclaratif pour la surveillance des missions de transport."""

from tms_agent.rules_engine.context import build_context, enrich_event
from tms_agent.rules_engine.dedup import AlertHistory, Decision, InMemoryAlertHistory, apply_deduplication
from tms_agent.rules_engine.engine import ConditionTrace, RuleEngine, RuleEvaluation, RuleMatch
from tms_agent.rules_engine.loader import RuleLoadError, load_rules, parse_rules
from tms_agent.rules_engine.models import Rule
from tms_agent.rules_engine.rulebook import RuleBook
from tms_agent.rules_engine.snapshot import mission_facts, snapshot_context

__version__ = "0.1.0"

__all__ = [
    "AlertHistory",
    "ConditionTrace",
    "Decision",
    "InMemoryAlertHistory",
    "Rule",
    "RuleBook",
    "RuleEngine",
    "RuleEvaluation",
    "RuleLoadError",
    "RuleMatch",
    "apply_deduplication",
    "build_context",
    "enrich_event",
    "load_rules",
    "mission_facts",
    "parse_rules",
    "snapshot_context",
]
