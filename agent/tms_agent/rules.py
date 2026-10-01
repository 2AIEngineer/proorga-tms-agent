"""Accès au moteur de règles déclaratif (projet `rules_engine`).

- Rechargement à chaud : si un fichier de `rules/` change, les règles sont rechargées au cycle
  suivant ; une règle invalide est signalée et l'ancien jeu de règles reste actif.
- Conversion d'un instantané MCP (`get_mission_snapshot`) en contexte d'évaluation.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from rules_engine.operators import to_datetime

from rules_engine import RuleEngine, RuleLoadError, build_context, load_rules

log = logging.getLogger(__name__)


class RuleBook:
    def __init__(self, rules_dir: str | Path):
        self.rules_dir = Path(rules_dir)
        self._fingerprint: tuple | None = None
        self.engine: RuleEngine = RuleEngine([])
        self.last_error: str | None = None
        self.reload(force=True)

    def _current_fingerprint(self) -> tuple:
        files = sorted([*self.rules_dir.glob("*.yaml"), *self.rules_dir.glob("*.yml")])
        return tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)

    def reload(self, force: bool = False) -> bool:
        """Recharge si les fichiers ont changé. Renvoie True si un nouveau jeu de règles est actif."""
        fingerprint = self._current_fingerprint()
        if not force and fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        try:
            engine = RuleEngine(load_rules(self.rules_dir))
        except (RuleLoadError, ValueError) as exc:
            self.last_error = str(exc)
            if force and not self.engine.rules:
                raise
            log.error("Règles invalides, l'ancien jeu reste actif :\n%s", exc)
            return False
        self.engine, self.last_error = engine, None
        log.info(
            "%d règle(s) active(s) chargée(s) depuis %s",
            len(engine.enabled_rules),
            self.rules_dir,
        )
        return True

    @property
    def state_rule_ids(self) -> set[str]:
        return {r.id for r in self.engine.enabled_rules if not r.event_scoped}

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "version": r.version,
                "enabled": r.enabled,
                "severity": r.severity,
                "category": r.category,
                "scope": "event" if r.event_scoped else "state",
                "description": " ".join(r.description.split()),
                "conditions": [
                    f"{leaf.field} {leaf.operator} {leaf.value!r}" for leaf in r.leaves
                ],
                "action": r.then.action,
                "notify": [n.model_dump(exclude_none=True) for n in r.then.notify],
            }
            for r in self.engine.rules
        ]


def snapshot_context(snapshot: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
    """Contexte d'évaluation + heure TMS à partir de la sortie de `get_mission_snapshot`."""
    now = to_datetime(snapshot["tms_time"])
    context = build_context(
        now=now,
        mission=snapshot["mission"],
        vehicle=snapshot.get("vehicle"),
        deviation=snapshot.get("deviation"),
        eta=snapshot.get("eta"),
        driver=snapshot.get("driver"),
    )
    return context, now


def _num(v: Any, digits: int = 1) -> str:
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if isinstance(v, (int, float)) else "?"


def mission_facts(snapshot: dict[str, Any]) -> list[str]:
    """Faits clés d'une mission, calculés de façon déterministe et formulés sans ambiguïté.

    Fournis au LLM avec l'alerte : un petit modèle n'a pas à recalculer un retard ni à interpréter
    des champs proches (cap / vitesse, retard négatif = avance).
    """
    context, _ = snapshot_context(snapshot)
    m, v, d, e = context["mission"], context.get("vehicle") or {}, context.get("deviation") or {}, context.get("eta") or {}
    origin = (m.get("origin") or {}).get("label", "?")
    destination = (m.get("destination") or {}).get("label", "?")
    facts = [
        f"Heure TMS : {snapshot['tms_time']}.",
        f"Mission {m.get('reference')} ({m['id']}) : statut {m.get('status')}, {origin} → {destination}.",
    ]
    if m.get("last_event_type"):
        facts.append(f"Dernier événement : {m['last_event_type']} il y a {_num(m.get('last_event_age_minutes'))} min "
                     f"({m.get('event_count')} événement(s) au total).")
    if v:
        position = v.get("current_position") or {}
        facts.append(
            f"Véhicule {v.get('plate')} : statut {v.get('status')}, vitesse {_num(v.get('speed_kmh'))} km/h, "
            f"cap {_num(position.get('heading'), 0)}° (direction de la route, pas une vitesse), "
            f"dernière position reçue il y a {_num(v.get('last_seen_age_minutes'))} min."
        )
    if d:
        if d.get("current_offset_km") is None:
            facts.append("Écart à l'itinéraire : non calculé (véhicule pas encore parti du chargement).")
        else:
            since = f" depuis {_num(d.get('duration_minutes'))} min" if d.get("duration_minutes") else ""
            facts.append(f"Écart à l'itinéraire : {_num(d['current_offset_km'], 2)} km{since} (sévérité TMS : {d.get('severity')}).")
    if e and isinstance(e.get("delay_minutes"), (int, float)):
        delay = e["delay_minutes"]
        trend = f"RETARD de {_num(delay)} min" if delay > 0 else (f"AVANCE de {_num(-delay)} min" if delay < 0 else "à l'heure")
        facts.append(f"Livraison prévue {e.get('planned_delivery_at')}, ETA {e.get('current_eta')} : {trend}"
                     f"{f', {_num(e.get('remaining_distance_km'))} km restants' if e.get('remaining_distance_km') is not None else ''}.")
    driver = snapshot.get("driver") or {}
    if driver:
        facts.append(f"Chauffeur {driver.get('name')} ({driver.get('id')}), téléphone {'connu' if driver.get('phone') else 'absent'}.")
    if snapshot.get("errors"):
        facts.append(f"Données manquantes : {', '.join(snapshot['errors'])}.")
    return facts
