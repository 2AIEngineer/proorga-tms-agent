"""TMS en mémoire implémentant le contrat pivot (tests et démonstrations hors ligne)."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from tms_mcp.connector.base import TmsNotFound, TmsUnavailable
from tms_mcp.connector.rest import parse_datetime

T0 = datetime(2025, 11, 4, 8, 0, tzinfo=UTC)


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


DEFAULT_USERS = [
    {"id": "drv_77", "name": "Karim E.", "role": "driver", "management_level": "M+0", "phone": "+212600000077", "email": None},
    {"id": "drv_78", "name": "Hamid O.", "role": "driver", "management_level": "M+0", "phone": None, "email": None},
    {"id": "usr_agent_12", "name": "Salma R.", "role": "ops_agent", "management_level": "M+1", "phone": "+212600000012", "email": "salma@example.com"},
    {"id": "usr_sup_04", "name": "Yassine B.", "role": "ops_supervisor", "management_level": "M+2", "phone": "+212600000004", "email": "yassine@example.com"},
    {"id": "usr_mgr_02", "name": "Nadia K.", "role": "ops_manager", "management_level": "M+3", "phone": "+212600000002", "email": "nadia@example.com"},
    {"id": "usr_dir_01", "name": "Omar F.", "role": "director", "management_level": "M+4", "phone": "+212600000001", "email": None},
]


def make_mission(mission_id: str = "msn_1", *, minutes: float = 120, driver_id: str = "drv_77", vehicle_id: str = "veh_1",
                 status: str = "in_progress") -> dict[str, Any]:
    return {
        "id": mission_id,
        "reference": f"MIS-{mission_id}",
        "vehicle_id": vehicle_id,
        "driver_id": driver_id,
        "origin": {"label": "Casablanca", "lat": 33.5731, "lon": -7.5898},
        "destination": {"label": "Rabat", "lat": 34.0209, "lon": -6.8416},
        "planned_route_id": f"rte_{mission_id}",
        "status": status,
        "planned_pickup_at": ts(0),
        "planned_delivery_at": ts(270),
        "actual_pickup_at": ts(12),
        "actual_delivery_at": None,
        "current_eta": ts(265),
        "created_at": ts(-60),
        "updated_at": ts(minutes),
        "events": [
            {"id": f"{mission_id}_e1", "mission_id": mission_id, "type": "arrived_pickup", "occurred_at": ts(12),
             "position": {"lat": 33.5733, "lon": -7.5901}, "payload": {}, "source": "driver_app"},
            {"id": f"{mission_id}_e2", "mission_id": mission_id, "type": "departed_pickup", "occurred_at": ts(55),
             "position": {"lat": 33.5735, "lon": -7.5899}, "payload": {}, "source": "driver_app"},
        ],
    }


class FakeTmsConnector:
    """État modifiable par les tests : `missions`, `vehicles`, `deviations`, `etas`, `now_dt`."""

    def __init__(self) -> None:
        self.now_dt = T0 + timedelta(minutes=120)
        self.users = {u["id"]: dict(u) for u in DEFAULT_USERS}
        self.missions: dict[str, dict[str, Any]] = {}
        self.vehicles: dict[str, dict[str, Any]] = {}
        self.deviations: dict[str, dict[str, Any]] = {}
        self.etas: dict[str, dict[str, Any]] = {}
        self.fail: set[str] = set()  # noms de méthodes qui lèvent TmsUnavailable
        self.calls: list[str] = []

    # --- Construction de scénarios ---------------------------------------------------

    def add_mission(self, mission: dict[str, Any], *, vehicle_status: str = "moving", offset_km: float | None = 0.2,
                    offset_minutes: float = 0, delay_minutes: float = -5) -> dict[str, Any]:
        mid, vid = mission["id"], mission["vehicle_id"]
        self.missions[mid] = mission
        self.vehicles[vid] = {
            "id": vid, "plate": f"PL-{vid}", "fleet_id": "flt_1", "status": vehicle_status,
            "current_position": {"lat": 33.75, "lon": -7.25, "heading": 45, "speed_kmh": 0 if vehicle_status == "stopped" else 78,
                                 "recorded_at": self.now_iso()},
            "last_seen_at": self.now_iso(), "current_mission_id": mid,
        }
        self.set_deviation(mid, offset_km, offset_minutes)
        self.etas[mid] = {"mission_id": mid, "planned_delivery_at": mission["planned_delivery_at"], "current_eta": None,
                          "delay_minutes": delay_minutes, "remaining_distance_km": 54.2, "computed_at": self.now_iso()}
        return mission

    def set_deviation(self, mission_id: str, offset_km: float | None, minutes: float) -> None:
        since = (self.now_dt - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z") if minutes else None
        self.deviations[mission_id] = {
            "mission_id": mission_id, "current_offset_km": offset_km, "current_offset_since": since,
            "duration_minutes": minutes, "severity": "medium" if (offset_km or 0) > 5 else "low",
            "vehicle_position": {"lat": 33.61, "lon": -7.52}, "computed_at": self.now_iso(),
        }

    def add_event(self, mission_id: str, event: dict[str, Any]) -> None:
        self.missions[mission_id]["events"].append({"mission_id": mission_id, "source": "driver_app", "payload": {}, **event})

    def advance(self, minutes: float) -> None:
        self.now_dt += timedelta(minutes=minutes)

    def now_iso(self) -> str:
        return self.now_dt.isoformat().replace("+00:00", "Z")

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise TmsUnavailable(f"{name} : TMS injoignable (simulé)")

    # --- Contrat ---------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        self._check("health")
        return {"status": "ok", "version": "fake", "time": self.now_iso(), "simulation": {"running": True, "speed": 1}}

    async def now(self) -> datetime:
        self._check("now")
        return self.now_dt

    async def list_missions(self, *, status=None, vehicle_id=None, driver_id=None, updated_since=None) -> list[dict[str, Any]]:
        self._check("list_missions")
        out = []
        for m in self.missions.values():
            if status and m["status"] not in status:
                continue
            if vehicle_id and m["vehicle_id"] != vehicle_id or driver_id and m["driver_id"] != driver_id:
                continue
            if updated_since and parse_datetime(m["updated_at"]) <= updated_since:
                continue
            out.append({k: v for k, v in m.items() if k != "events"})
        return copy.deepcopy(out)

    def _mission(self, mission_id: str) -> dict[str, Any]:
        if mission_id not in self.missions:
            raise TmsNotFound(f"TMS 404 Mission introuvable : {mission_id!r}", status=404)
        return self.missions[mission_id]

    async def get_mission(self, mission_id: str) -> dict[str, Any]:
        self._check("get_mission")
        m = copy.deepcopy(self._mission(mission_id))
        m["events"].sort(key=lambda e: e["occurred_at"])
        return m

    async def list_mission_events(self, mission_id: str, *, since=None, types=None) -> list[dict[str, Any]]:
        self._check("list_mission_events")
        events = sorted(self._mission(mission_id)["events"], key=lambda e: e["occurred_at"])
        return copy.deepcopy([e for e in events if (not since or parse_datetime(e["occurred_at"]) > since)
                              and (not types or e["type"] in types)])

    async def get_mission_route(self, mission_id: str) -> dict[str, Any]:
        self._check("get_mission_route")
        m = self._mission(mission_id)
        return {"id": m["planned_route_id"], "mission_id": mission_id, "polyline": "abc", "distance_km": 87.0, "duration_min": 75.0,
                "waypoints": [{"lat": m["origin"]["lat"], "lon": m["origin"]["lon"], "sequence": 0},
                              {"lat": m["destination"]["lat"], "lon": m["destination"]["lon"], "sequence": 1}]}

    async def get_mission_deviation(self, mission_id: str) -> dict[str, Any]:
        self._check("get_mission_deviation")
        self._mission(mission_id)
        return copy.deepcopy(self.deviations[mission_id])

    async def get_mission_eta(self, mission_id: str) -> dict[str, Any]:
        self._check("get_mission_eta")
        self._mission(mission_id)
        return copy.deepcopy(self.etas[mission_id])

    async def list_vehicles(self, *, status=None, fleet_id=None) -> list[dict[str, Any]]:
        self._check("list_vehicles")
        return [copy.deepcopy(v) for v in self.vehicles.values() if not status or v["status"] == status]

    async def get_vehicle(self, vehicle_id: str) -> dict[str, Any]:
        self._check("get_vehicle")
        if vehicle_id not in self.vehicles:
            raise TmsNotFound(f"TMS 404 Véhicule introuvable : {vehicle_id!r}", status=404)
        return copy.deepcopy(self.vehicles[vehicle_id])

    async def get_vehicle_position(self, vehicle_id: str) -> dict[str, Any]:
        vehicle = await self.get_vehicle(vehicle_id)
        return {"vehicle_id": vehicle_id, **vehicle["current_position"]}

    async def list_vehicle_positions(self, vehicle_id: str, *, since=None, until=None, mission_id=None) -> list[dict[str, Any]]:
        vehicle = await self.get_vehicle(vehicle_id)
        return [{**vehicle["current_position"], "mission_id": vehicle["current_mission_id"]}]

    async def get_driver(self, driver_id: str) -> dict[str, Any]:
        self._check("get_driver")
        user = self.users.get(driver_id)
        if not user or user["role"] != "driver":
            raise TmsNotFound(f"TMS 404 Chauffeur introuvable : {driver_id!r}", status=404)
        current = next((m["id"] for m in self.missions.values() if m["driver_id"] == driver_id and m["status"] == "in_progress"), None)
        return {**user, "status": "on_mission" if current else "available", "current_mission_id": current}

    async def get_user(self, user_id: str) -> dict[str, Any]:
        self._check("get_user")
        if user_id not in self.users:
            raise TmsNotFound(f"TMS 404 Utilisateur introuvable : {user_id!r}", status=404)
        return dict(self.users[user_id])

    async def list_users(self, *, role=None, management_level=None) -> list[dict[str, Any]]:
        self._check("list_users")
        return [dict(u) for u in self.users.values()
                if (not role or u["role"] == role) and (not management_level or u["management_level"] == management_level)]

    async def list_role_users(self, role: str) -> list[dict[str, Any]]:
        return await self.list_users(role=role)

    async def aclose(self) -> None:
        pass
