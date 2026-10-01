"""Contrat pivot du connecteur TMS (Document 1).

Le serveur MCP ne parle au TMS qu'à travers ce protocole. Brancher un autre TMS revient à
écrire un connecteur qui traduit ses ressources vers ces objets : les tools MCP, l'agent et
le moteur de règles ne changent pas.

Toutes les méthodes sont en **lecture seule** et renvoient des dictionnaires JSON au format pivot.
"""

from datetime import datetime
from typing import Any, Protocol

JSON = dict[str, Any]


class TmsError(Exception):
    """Erreur renvoyée par le TMS (RFC 7807) ou de transport."""

    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class TmsNotFound(TmsError):
    pass


class TmsUnavailable(TmsError):
    """TMS injoignable après plusieurs tentatives (réseau, 5xx, timeout)."""


class TmsConnector(Protocol):
    async def health(self) -> JSON: ...

    async def now(self) -> datetime: ...

    async def list_missions(
        self,
        *,
        status: list[str] | None = None,
        vehicle_id: str | None = None,
        driver_id: str | None = None,
        updated_since: datetime | None = None,
    ) -> list[JSON]: ...

    async def get_mission(self, mission_id: str) -> JSON: ...

    async def list_mission_events(
        self, mission_id: str, *, since: datetime | None = None, types: list[str] | None = None
    ) -> list[JSON]: ...

    async def get_mission_route(self, mission_id: str) -> JSON: ...

    async def get_mission_deviation(self, mission_id: str) -> JSON: ...

    async def get_mission_eta(self, mission_id: str) -> JSON: ...

    async def list_vehicles(self, *, status: str | None = None, fleet_id: str | None = None) -> list[JSON]: ...

    async def get_vehicle(self, vehicle_id: str) -> JSON: ...

    async def get_vehicle_position(self, vehicle_id: str) -> JSON: ...

    async def list_vehicle_positions(
        self,
        vehicle_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        mission_id: str | None = None,
    ) -> list[JSON]: ...

    async def get_driver(self, driver_id: str) -> JSON: ...

    async def get_user(self, user_id: str) -> JSON: ...

    async def list_users(self, *, role: str | None = None, management_level: str | None = None) -> list[JSON]: ...

    async def list_role_users(self, role: str) -> list[JSON]: ...

    async def aclose(self) -> None: ...
