"""Ligne de commande : valider, évaluer et tester des règles.

    rules-engine validate rules/
    rules-engine eval rules/ snapshot.json [--now 2025-11-04T09:42:00Z] [--explain]
    rules-engine test rules/ scenarios/
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from rules_engine.context import build_context, enrich_event
from rules_engine.engine import RuleEngine
from rules_engine.loader import RuleLoadError, load_rules
from rules_engine.operators import to_datetime
from rules_engine.scenarios import run_scenarios, run_snapshot


def _load_engine(path: str) -> RuleEngine:
    try:
        return RuleEngine(load_rules(path))
    except (RuleLoadError, ValueError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)


def cmd_validate(args) -> int:
    engine = _load_engine(args.rules)
    for r in engine.rules:
        state = "active" if r.enabled else "désactivée"
        scope = "événement" if r.event_scoped else "état"
        print(f"✓ {r.id} v{r.version}  [{r.severity}/{r.category}]  {state}, portée {scope}, action {r.then.action}")
    print(f"{len(engine.rules)} règle(s) valide(s).")
    return 0


def cmd_eval(args) -> int:
    engine = _load_engine(args.rules)
    snapshot = yaml.safe_load(Path(args.snapshot).read_text(encoding="utf-8"))
    now = args.now or snapshot.get("now") or datetime.now(UTC).isoformat()
    if args.explain:
        now_dt = to_datetime(now)
        ctx = build_context(now=now_dt, **{k: snapshot.get(k) for k in ("vehicle", "deviation", "eta", "events", "driver")},
                            mission=snapshot["mission"])
        contexts = [ctx] + [{**ctx, "event": enrich_event(e, ctx["mission"], now_dt)} for e in snapshot.get("new_events", [])]
        for c in contexts:
            for ev in engine.explain(c, now_dt):
                mark = "DÉCLENCHÉE" if ev.triggered else "non déclenchée"
                print(f"{ev.rule_id} v{ev.rule_version}{f' (événement {ev.event_id})' if ev.event_id else ''} : {mark}")
                for t in ev.trace:
                    print(f"    {'✓' if t.result else '✗'} {t.field} {t.operator} {t.expected!r}  (valeur : {t.actual!r})")
        return 0
    matches = run_snapshot(engine, snapshot, now)
    print(json.dumps([m.to_dict() for m in matches], ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_test(args) -> int:
    engine = _load_engine(args.rules)
    results = run_scenarios(engine, args.scenarios)
    current = None
    for r in results:
        if r.file != current:
            current = r.file
            print(f"\n{r.file}")
        print(f"  {'✓' if r.passed else '✗'} {r.name}")
        if not r.passed:
            if r.error:
                print(f"      erreur : {r.error}")
            else:
                print(f"      attendu : {r.expected or '(aucune)'}")
                print(f"      obtenu  : {r.actual or '(aucune)'}")
    failed = sum(not r.passed for r in results)
    print(f"\n{len(results) - failed}/{len(results)} cas réussis.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rules-engine", description="Moteur de règles métier (YAML).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="Valide les fichiers de règles.")
    p.add_argument("rules", help="Fichier ou dossier de règles YAML.")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("eval", help="Évalue les règles sur un instantané (JSON ou YAML).")
    p.add_argument("rules")
    p.add_argument("snapshot", help="Fichier {mission, vehicle, deviation, eta, events, new_events, now?}.")
    p.add_argument("--now", help="Heure d'évaluation ISO 8601 (défaut : `now` du fichier, sinon maintenant).")
    p.add_argument("--explain", action="store_true", help="Détaille chaque condition de chaque règle.")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("test", help="Exécute les scénarios sur données figées.")
    p.add_argument("rules")
    p.add_argument("scenarios", help="Fichier ou dossier de scénarios YAML.")
    p.set_defaults(func=cmd_test)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
