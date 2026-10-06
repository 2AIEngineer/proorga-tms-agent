"""Instantané de mission (sortie du tool MCP `get_mission_snapshot`) : contexte d'évaluation des
règles et faits clés calculés de façon déterministe pour le LLM.
"""

from datetime import datetime
from typing import Any

from tms_agent.rules_engine.context import build_context
from tms_agent.rules_engine.operators import to_datetime


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
    return (
        f"{v:.{digits}f}".rstrip("0").rstrip(".")
        if isinstance(v, (int, float))
        else "?"
    )


def mission_facts(snapshot: dict[str, Any]) -> list[str]:
    """Faits clés d'une mission, calculés de façon déterministe et formulés sans ambiguïté.

    Fournis au LLM avec l'alerte : un petit modèle n'a pas à recalculer un retard ni à interpréter
    des champs proches (cap / vitesse, retard négatif = avance).
    """
    context, _ = snapshot_context(snapshot)
    m, v, d, e = (
        context["mission"],
        context.get("vehicle") or {},
        context.get("deviation") or {},
        context.get("eta") or {},
    )
    origin = (m.get("origin") or {}).get("label", "?")
    destination = (m.get("destination") or {}).get("label", "?")
    facts = [
        f"Heure TMS : {snapshot['tms_time']}.",
        f"Mission {m.get('reference')} ({m['id']}) : statut {m.get('status')}, {origin} → {destination}.",
    ]
    if m.get("last_event_type"):
        facts.append(
            f"Dernier événement : {m['last_event_type']} il y a {_num(m.get('last_event_age_minutes'))} min "
            f"({m.get('event_count')} événement(s) au total)."
        )
    if v:
        position = v.get("current_position") or {}
        facts.append(
            f"Véhicule {v.get('plate')} : statut {v.get('status')}, vitesse {_num(v.get('speed_kmh'))} km/h, "
            f"cap {_num(position.get('heading'), 0)}° (direction de la route, pas une vitesse), "
            f"dernière position reçue il y a {_num(v.get('last_seen_age_minutes'))} min."
        )
    if d:
        if d.get("current_offset_km") is None:
            facts.append(
                "Écart à l'itinéraire : non calculé (véhicule pas encore parti du chargement)."
            )
        else:
            since = (
                f" depuis {_num(d.get('duration_minutes'))} min"
                if d.get("duration_minutes")
                else ""
            )
            facts.append(
                f"Écart à l'itinéraire : {_num(d['current_offset_km'], 2)} km{since} (sévérité TMS : {d.get('severity')})."
            )
    if e and isinstance(e.get("delay_minutes"), (int, float)):
        delay = e["delay_minutes"]
        trend = (
            f"RETARD de {_num(delay)} min"
            if delay > 0
            else (f"AVANCE de {_num(-delay)} min" if delay < 0 else "à l'heure")
        )
        remaining = e.get("remaining_distance_km")
        remaining_text = f", {_num(remaining)} km restants" if remaining is not None else ""
        facts.append(
            f"Livraison prévue {e.get('planned_delivery_at')}, ETA {e.get('current_eta')} : {trend}"
            f"{remaining_text}."
        )
    driver = snapshot.get("driver") or {}
    if driver:
        facts.append(
            f"Chauffeur {driver.get('name')} ({driver.get('id')}), téléphone {'connu' if driver.get('phone') else 'absent'}."
        )
    if snapshot.get("errors"):
        facts.append(f"Données manquantes : {', '.join(snapshot['errors'])}.")
    return facts
