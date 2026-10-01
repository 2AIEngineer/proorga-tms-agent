"""Moteur d'exécution : évalue des règles déclaratives sur un contexte figé.

Le moteur est pur et déterministe : il ne lit ni n'écrit rien. Il reçoit le contexte
(construit par l'orchestrateur à partir du TMS) et l'heure courante, et renvoie les
règles déclenchées avec les données observées et les destinataires à notifier.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rules_engine.context import enrich_event
from rules_engine.models import All, AnyOf, Condition, Leaf, Not, Rule
from rules_engine.operators import EvalContext, apply
from rules_engine.paths import MISSING, resolve
from rules_engine.templating import render


@dataclass
class ConditionTrace:
    field: str
    operator: str
    expected: Any
    actual: Any
    result: bool


@dataclass
class RuleEvaluation:
    rule_id: str
    rule_version: int
    triggered: bool
    trace: list[ConditionTrace]
    event_id: str | None = None


@dataclass
class RuleMatch:
    """Règle déclenchée : tout ce qu'il faut pour émettre, notifier et auditer."""

    rule_id: str
    rule_version: int
    severity: str
    category: str
    action: str
    mission_id: str | None
    event_id: str | None
    title: str | None
    message: str | None
    deduplication_key: str | None
    cooldown_minutes: int
    notify: list[dict[str, Any]]
    observed_data: dict[str, Any]
    trace: list[ConditionTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _jsonable(v: Any) -> Any:
    return None if v is MISSING else v


class RuleEngine:
    def __init__(self, rules: list[Rule]):
        ids = [r.id for r in rules]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"identifiants de règle en double : {', '.join(sorted(duplicates))}")
        self.rules = list(rules)

    @classmethod
    def from_path(cls, path: str | Path) -> "RuleEngine":
        from rules_engine.loader import load_rules

        return cls(load_rules(path))

    @property
    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    # --- Évaluation -------------------------------------------------------------------

    def _eval(self, cond: Condition, ctx: EvalContext, trace: list[ConditionTrace]) -> bool:
        # Pas de court-circuit : la trace contient toutes les conditions (auditabilité).
        if isinstance(cond, Leaf):
            actual = resolve(ctx.data, cond.field)
            result = apply(cond.operator, actual, cond.value, ctx)
            trace.append(ConditionTrace(cond.field, cond.operator, cond.value, _jsonable(actual), result))
            return result
        if isinstance(cond, All):
            return all([self._eval(c, ctx, trace) for c in cond.all])
        if isinstance(cond, AnyOf):
            return any([self._eval(c, ctx, trace) for c in cond.any])
        if isinstance(cond, Not):
            return not self._eval(cond.not_, ctx, trace)
        raise TypeError(f"condition inconnue : {cond!r}")

    def _evaluate_rule(self, rule: Rule, context: dict[str, Any], now: datetime) -> RuleEvaluation:
        trace: list[ConditionTrace] = []
        triggered = self._eval(rule.when, EvalContext(context, now), trace)
        event_id = context.get("event", {}).get("id") if rule.event_scoped else None
        return RuleEvaluation(rule.id, rule.version, triggered, trace, event_id)

    def _to_match(self, rule: Rule, evaluation: RuleEvaluation, context: dict[str, Any]) -> RuleMatch:
        observed = {t.field: t.actual for t in evaluation.trace}
        observed.update({path: _jsonable(resolve(context, path)) for path in rule.observe})
        data = {**context, "rule": {"id": rule.id, "version": rule.version, "severity": rule.severity}}
        alert = rule.then.alert
        title = render(alert.title, data) if alert else None
        message = None
        if alert:
            message = render(alert.message, data) if alert.message else f"{title} — mission {render('{{ mission.reference }}', data)}"
        notify = [
            {
                **r.model_dump(exclude_none=True, exclude={"message"}),
                "message": render(r.message, data) if r.message else message,
            }
            for r in rule.then.notify
        ]
        return RuleMatch(
            rule_id=rule.id,
            rule_version=rule.version,
            severity=rule.severity,
            category=rule.category,
            action=rule.then.action,
            mission_id=_jsonable(resolve(context, "mission.id")),
            event_id=evaluation.event_id,
            title=title,
            message=message,
            deduplication_key=render(alert.deduplication_key, data) if alert else None,
            cooldown_minutes=alert.cooldown_minutes if alert else 0,
            notify=notify,
            observed_data=observed,
            trace=evaluation.trace,
        )

    def explain(self, context: dict[str, Any], now: datetime) -> list[RuleEvaluation]:
        """Évalue toutes les règles actives applicables au contexte, déclenchées ou non."""
        has_event = "event" in context
        return [self._evaluate_rule(r, context, now) for r in self.enabled_rules if r.event_scoped == has_event]

    def evaluate(self, context: dict[str, Any], now: datetime) -> list[RuleMatch]:
        """Règles déclenchées pour un contexte.

        Sans clé `event`, seules les règles portant sur l'état (mission, véhicule, écart, ETA)
        sont évaluées ; avec `event`, seules celles qui portent sur un événement.
        """
        by_id = {r.id: r for r in self.rules}
        return [self._to_match(by_id[e.rule_id], e, context) for e in self.explain(context, now) if e.triggered]

    def evaluate_mission(
        self, context: dict[str, Any], now: datetime, new_events: list[dict[str, Any]] = ()
    ) -> list[RuleMatch]:
        """Cycle complet pour une mission : règles d'état une fois, règles d'événement pour chaque nouvel événement."""
        matches = self.evaluate(context, now)
        for evt in new_events:
            event_ctx = {**context, "event": enrich_event(evt, context["mission"], now)}
            matches += self.evaluate(event_ctx, now)
        return matches
