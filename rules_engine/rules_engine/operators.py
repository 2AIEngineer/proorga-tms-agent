"""Opérateurs de condition (comparaison, appartenance, temporel, géospatial).

Convention : si le champ est absent du contexte (ou nul), la condition est fausse.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rules_engine.paths import MISSING, resolve


class OperatorValueError(ValueError):
    pass


@dataclass(frozen=True)
class EvalContext:
    data: dict[str, Any]
    now: datetime


@dataclass(frozen=True)
class Operator:
    name: str
    test: Callable[[Any, Any, EvalContext], bool]
    validate: Callable[[Any], None]


# --- Conversions ----------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def to_datetime(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def to_point(v: Any) -> tuple[float, float] | None:
    """Accepte {lat, lon} ou [lat, lon]."""
    if isinstance(v, dict) and _is_number(v.get("lat")) and _is_number(v.get("lon")):
        return float(v["lat"]), float(v["lon"])
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_number(x) for x in v):
        return float(v[0]), float(v[1])
    return None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def minutes_since(v: Any, now: datetime) -> float | None:
    dt = to_datetime(v)
    return None if dt is None else (now - dt).total_seconds() / 60


# --- Validateurs de `value` -------------------------------------------------------------


def _any(_: Any) -> None:
    pass


def _number(v: Any) -> None:
    if not _is_number(v):
        raise OperatorValueError(f"`value` doit être un nombre (reçu {v!r})")


def _range(v: Any) -> None:
    if not (isinstance(v, list) and len(v) == 2 and all(_is_number(x) for x in v) and v[0] <= v[1]):
        raise OperatorValueError(f"`value` doit être [min, max] (reçu {v!r})")


def _list(v: Any) -> None:
    if not isinstance(v, list) or not v:
        raise OperatorValueError(f"`value` doit être une liste non vide (reçu {v!r})")


def _distance(v: Any) -> None:
    if not (isinstance(v, dict) and set(v) == {"from", "km"} and _is_number(v["km"])):
        raise OperatorValueError("`value` doit être {from: <point ou champ>, km: <nombre>}")
    if not (isinstance(v["from"], str) or to_point(v["from"])):
        raise OperatorValueError("`from` doit être un point {lat, lon} ou un chemin de champ (ex. mission.destination)")


def _polygon(v: Any) -> None:
    if not (isinstance(v, list) and len(v) >= 3 and all(to_point(p) for p in v)):
        raise OperatorValueError("`value` doit être une liste d'au moins 3 points {lat, lon} ou [lat, lon]")


# --- Tests --------------------------------------------------------------------------------


def _equals(a: Any, b: Any, _: EvalContext) -> bool:
    if _is_number(a) and _is_number(b):
        return math.isclose(a, b)
    return a == b


def _cmp(fn: Callable[[float, float], bool]):
    return lambda a, b, _: _is_number(a) and fn(a, b)


def _between(a: Any, b: Any, _: EvalContext) -> bool:
    return _is_number(a) and b[0] <= a <= b[1]


def _older_than(a: Any, b: Any, ctx: EvalContext) -> bool:
    m = minutes_since(a, ctx.now)
    return m is not None and m > b


def _newer_than(a: Any, b: Any, ctx: EvalContext) -> bool:
    m = minutes_since(a, ctx.now)
    return m is not None and 0 <= m < b


def _duration_gt(a: Any, b: Any, ctx: EvalContext) -> bool:
    """Le champ est soit une durée en minutes, soit l'instant de début de la situation."""
    minutes = a if _is_number(a) else minutes_since(a, ctx.now)
    return minutes is not None and minutes > b


def _distance_gt(a: Any, b: Any, ctx: EvalContext) -> bool:
    p = to_point(a)
    ref = b["from"]
    q = to_point(resolve(ctx.data, ref)) if isinstance(ref, str) else to_point(ref)
    return p is not None and q is not None and haversine_km(p, q) > b["km"]


def _inside(point: tuple[float, float], polygon: list) -> bool:
    """Ray casting (lat = y, lon = x)."""
    pts = [to_point(p) for p in polygon]
    y, x = point
    inside = False
    for (y1, x1), (y2, x2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _inside_polygon(a: Any, b: Any, _: EvalContext) -> bool:
    p = to_point(a)
    return p is not None and _inside(p, b)


def _outside_polygon(a: Any, b: Any, _: EvalContext) -> bool:
    p = to_point(a)
    return p is not None and not _inside(p, b)


OPERATORS: dict[str, Operator] = {
    op.name: op
    for op in [
        Operator("equals", _equals, _any),
        Operator("not_equals", lambda a, b, c: not _equals(a, b, c), _any),
        Operator("greater_than", _cmp(lambda a, b: a > b), _number),
        Operator("greater_or_equal", _cmp(lambda a, b: a >= b), _number),
        Operator("less_than", _cmp(lambda a, b: a < b), _number),
        Operator("less_or_equal", _cmp(lambda a, b: a <= b), _number),
        Operator("between", _between, _range),
        Operator("in", lambda a, b, _: a in b, _list),
        Operator("not_in", lambda a, b, _: a not in b, _list),
        Operator("older_than_minutes", _older_than, _number),
        Operator("newer_than_minutes", _newer_than, _number),
        Operator("duration_greater_than_minutes", _duration_gt, _number),
        Operator("distance_greater_than_km", _distance_gt, _distance),
        Operator("inside_polygon", _inside_polygon, _polygon),
        Operator("outside_polygon", _outside_polygon, _polygon),
    ]
}


def apply(operator: str, actual: Any, expected: Any, ctx: EvalContext) -> bool:
    if actual is MISSING or actual is None:
        return False
    return OPERATORS[operator].test(actual, expected, ctx)
