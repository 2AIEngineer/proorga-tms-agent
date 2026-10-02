from datetime import UTC, datetime

import pytest

from tms_agent.rules_engine.operators import EvalContext, apply
from tms_agent.rules_engine.paths import MISSING

NOW = datetime(2025, 11, 4, 10, 0, tzinfo=UTC)
CTX = EvalContext({"mission": {"destination": {"lat": 34.0209, "lon": -6.8416}}}, NOW)
SQUARE = [[33.0, -8.0], [34.0, -8.0], [34.0, -7.0], [33.0, -7.0]]


@pytest.mark.parametrize(
    "operator, actual, expected, result",
    [
        ("equals", "in_progress", "in_progress", True),
        ("equals", 5, 5.0, True),
        ("not_equals", "stopped", "moving", True),
        ("greater_than", 6.4, 5, True),
        ("greater_than", 5, 5, False),
        ("greater_than", "6", 5, False),  # pas de conversion implicite
        ("greater_than", True, 0, False),
        ("greater_or_equal", 5, 5, True),
        ("less_than", 3, 5, True),
        ("less_or_equal", 5, 5, True),
        ("between", 30, [20, 45], True),
        ("between", 50, [20, 45], False),
        ("in", "arrived_pickup", ["arrived_pickup", "arrived_delivery"], True),
        ("not_in", "driver_message", ["arrived_pickup"], True),
        ("older_than_minutes", "2025-11-04T07:30:00Z", 120, True),
        ("older_than_minutes", "2025-11-04T09:30:00Z", 120, False),
        ("newer_than_minutes", "2025-11-04T09:50:00Z", 15, True),
        ("newer_than_minutes", "2025-11-04T10:30:00Z", 15, False),  # dans le futur
        ("duration_greater_than_minutes", 27, 20, True),
        ("duration_greater_than_minutes", "2025-11-04T09:15:00Z", 20, True),
        ("distance_greater_than_km", {"lat": 34.0, "lon": -6.80}, {"from": "mission.destination", "km": 2}, True),
        ("distance_greater_than_km", {"lat": 34.0209, "lon": -6.8416}, {"from": {"lat": 34.01, "lon": -6.84}, "km": 2}, False),
        ("inside_polygon", {"lat": 33.5, "lon": -7.5}, SQUARE, True),
        ("inside_polygon", {"lat": 35.0, "lon": -7.5}, SQUARE, False),
        ("outside_polygon", [35.0, -7.5], SQUARE, True),
    ],
)
def test_operators(operator, actual, expected, result):
    assert apply(operator, actual, expected, CTX) is result


@pytest.mark.parametrize("operator", ["equals", "not_equals", "not_in", "greater_than", "older_than_minutes"])
def test_missing_or_null_field_is_false(operator):
    expected = ["x"] if operator == "not_in" else 1
    assert apply(operator, MISSING, expected, CTX) is False
    assert apply(operator, None, expected, CTX) is False
