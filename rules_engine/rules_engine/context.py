"""Construction du contexte d'évaluation à partir des ressources du TMS (contrat pivot, Document 1).

Les entrées sont les objets JSON tels que renvoyés par l'API du TMS (via les tools MCP) :
mission, vehicle, deviation, eta, events, driver. Le contexte ajoute des champs dérivés
(âges, durées, distances) calculés par rapport à `now` — l'heure du TMS.
"""

from datetime import datetime
from typing import Any

from rules_engine.operators import haversine_km, minutes_since, to_datetime, to_point

# Point cible d'un événement d'arrivée.
_TARGET_OF_EVENT = {"arrived_pickup": "origin", "arrived_delivery": "destination"}


def _round(v: float | None, digits: int = 1) -> float | None:
    return None if v is None else round(v, digits)


def build_context(
    *,
    now: datetime,
    mission: dict[str, Any],
    vehicle: dict[str, Any] | None = None,
    deviation: dict[str, Any] | None = None,
    eta: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    driver: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = sorted(events if events is not None else mission.get("events", []), key=lambda e: e["occurred_at"])
    last = events[-1] if events else None

    ctx_mission = {k: v for k, v in mission.items() if k != "events"}
    ctx_mission.update(
        event_count=len(events),
        last_event_type=last["type"] if last else None,
        last_event_at=last["occurred_at"] if last else None,
        last_event_age_minutes=_round(minutes_since(last["occurred_at"], now)) if last else None,
    )

    ctx: dict[str, Any] = {"mission": ctx_mission}

    if vehicle is not None:
        position = vehicle.get("current_position") or {}
        ctx["vehicle"] = {
            **vehicle,
            "speed_kmh": position.get("speed_kmh"),
            "last_seen_age_minutes": _round(minutes_since(vehicle.get("last_seen_at"), now)),
        }

    if deviation is not None:
        duration = deviation.get("duration_minutes")
        if duration is None and deviation.get("current_offset_since"):
            duration = minutes_since(deviation["current_offset_since"], to_datetime(deviation.get("computed_at")) or now)
        ctx["deviation"] = {**deviation, "duration_minutes": _round(duration)}

    if eta is not None:
        delay = eta.get("delay_minutes")
        if delay is None and eta.get("current_eta") and eta.get("planned_delivery_at"):
            delay = (to_datetime(eta["current_eta"]) - to_datetime(eta["planned_delivery_at"])).total_seconds() / 60
        ctx["eta"] = {**eta, "delay_minutes": _round(delay)}

    if driver is not None:
        ctx["driver"] = dict(driver)

    return ctx


def enrich_event(event: dict[str, Any], mission: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Ajoute à un événement les champs dérivés utiles aux règles (distance à la cible, âge)."""
    distance = None
    target_key = _TARGET_OF_EVENT.get(event.get("type"))
    position = to_point(event.get("position"))
    target = to_point(mission.get(target_key)) if target_key else None
    if position and target:
        distance = round(haversine_km(position, target), 2)
    return {
        **event,
        "distance_to_target_km": distance,
        "age_minutes": _round(minutes_since(event.get("occurred_at"), now)),
    }
