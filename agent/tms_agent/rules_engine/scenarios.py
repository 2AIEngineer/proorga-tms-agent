"""Exécution de règles sur des données figées (scénarios YAML lisibles par l'équipe métier).

Format d'un fichier de scénarios :

    name: Dérive d'itinéraire
    base:                      # instantané commun (optionnel), fusionné dans chaque cas
      mission: {...}
      vehicle: {...}
    cases:
      - name: Écart de 6,4 km depuis 27 min
        now: "2025-11-04T09:42:00Z"
        snapshot:              # surcharge de `base` (fusion profonde)
          deviation: {current_offset_km: 6.4, ...}
        new_events: [...]      # événements à évaluer avec les règles portant sur `event.*`
        expect:
          triggered: [deviation_persistent]   # liste exacte des règles déclenchées
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tms_agent.rules_engine.context import build_context
from tms_agent.rules_engine.engine import RuleEngine, RuleMatch
from tms_agent.rules_engine.operators import to_datetime


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(base.get(key), value) if key in base else value
        return merged
    return override


def run_snapshot(engine: RuleEngine, snapshot: dict[str, Any], now: str | Any) -> list[RuleMatch]:
    """Construit le contexte depuis un instantané TMS et évalue toutes les règles."""
    now_dt = to_datetime(now)
    if now_dt is None:
        raise ValueError(f"`now` invalide : {now!r}")
    context = build_context(
        now=now_dt,
        mission=snapshot["mission"],
        vehicle=snapshot.get("vehicle"),
        deviation=snapshot.get("deviation"),
        eta=snapshot.get("eta"),
        events=snapshot.get("events"),
        driver=snapshot.get("driver"),
    )
    return engine.evaluate_mission(context, now_dt, snapshot.get("new_events", []))


@dataclass
class CaseResult:
    file: str
    name: str
    passed: bool
    expected: list[str]
    actual: list[str]
    matches: list[RuleMatch] = field(default_factory=list)
    error: str | None = None


def run_scenario_file(engine: RuleEngine, path: Path) -> list[CaseResult]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = doc.get("base", {})
    results = []
    for i, case in enumerate(doc.get("cases", []), 1):
        name = case.get("name", f"cas {i}")
        expected = sorted(case.get("expect", {}).get("triggered", []))
        try:
            snapshot = deep_merge(base, case.get("snapshot", {}))
            if "new_events" in case:
                snapshot["new_events"] = case["new_events"]
            matches = run_snapshot(engine, snapshot, case["now"])
        except Exception as exc:  # noqa: BLE001 — rapporté comme échec du cas
            results.append(CaseResult(str(path), name, False, expected, [], error=f"{type(exc).__name__}: {exc}"))
            continue
        actual = sorted(m.rule_id for m in matches)
        results.append(CaseResult(str(path), name, actual == expected, expected, actual, matches))
    return results


def run_scenarios(engine: RuleEngine, path: str | Path) -> list[CaseResult]:
    path = Path(path)
    files = sorted([*path.glob("*.yaml"), *path.glob("*.yml")]) if path.is_dir() else [path]
    return [r for f in files for r in run_scenario_file(engine, f)]
