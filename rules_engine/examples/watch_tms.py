"""Démonstration de la boucle de surveillance de l'orchestrateur, sans LLM ni MCP.

Interroge l'API du TMS en lecture seule (GET), construit le contexte de chaque mission
en cours, évalue les règles, applique déduplication/cooldown et affiche les alertes.
Dans le projet orchestrateur, les lectures passeront par les tools MCP et les alertes
seront enregistrées dans la base de l'agent puis notifiées.

    uv run python examples/watch_tms.py --api http://127.0.0.1:8000 --interval 2
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from rules_engine.operators import to_datetime

from rules_engine import (
    InMemoryAlertHistory,
    RuleEngine,
    apply_deduplication,
    build_context,
)

ROOT = Path(__file__).resolve().parent.parent


def get(api: str, path: str):
    with urllib.request.urlopen(f"{api}/v1{path}", timeout=10) as resp:
        return json.load(resp)


def run_cycle(
    api: str,
    engine: RuleEngine,
    history: InMemoryAlertHistory,
    seen_events: dict[str, set[str]],
) -> None:
    now_iso = get(api, "/health")["time"]  # heure du TMS (accélérée en simulation)
    now = to_datetime(now_iso)
    for summary in get(api, "/missions?status=in_progress&limit=200")["data"]:
        mid = summary["id"]
        mission = get(api, f"/missions/{mid}")
        context = build_context(
            now=now,
            mission=mission,
            vehicle=get(api, f"/vehicles/{mission['vehicle_id']}"),
            deviation=get(api, f"/missions/{mid}/deviation"),
            eta=get(api, f"/missions/{mid}/eta"),
        )
        known = seen_events.setdefault(mid, set())
        new_events = [e for e in mission["events"] if e["id"] not in known]
        known.update(e["id"] for e in new_events)

        for decision in apply_deduplication(
            engine.evaluate_mission(context, now, new_events), history, now
        ):
            if not decision.emit:
                continue
            m = decision.match
            if m.deduplication_key:
                history.record(m.deduplication_key, now)
            recipients = ", ".join(
                f"{n.get('role') or n.get('user_id')}/{n['channel']}" for n in m.notify
            )
            print(
                f"[{now_iso}] ALERTE {m.severity.upper():8} {m.rule_id} v{m.rule_version} — {m.message}"
            )
            print(f"{'':22}→ {recipients}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--rules", default=str(ROOT / "rules"))
    parser.add_argument(
        "--interval", type=float, default=5.0, help="Secondes entre deux cycles."
    )
    parser.add_argument(
        "--cycles", type=int, default=0, help="Nombre de cycles (0 = sans fin)."
    )
    args = parser.parse_args()

    engine = RuleEngine.from_path(args.rules)
    history = InMemoryAlertHistory()
    seen_events: dict[str, set[str]] = {}
    print(f"{len(engine.enabled_rules)} règles actives — TMS {args.api}", flush=True)

    cycle = 0
    while not args.cycles or cycle < args.cycles:
        cycle += 1
        try:
            run_cycle(args.api, engine, history, seen_events)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(
                f"TMS injoignable ({exc}), nouvelle tentative dans {args.interval} s",
                flush=True,
            )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
