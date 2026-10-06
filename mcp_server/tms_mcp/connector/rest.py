"""Connecteur REST vers le TMS du POC (mapping direct : l'API expose déjà le contrat pivot).

- Lecture seule : seule la méthode GET est utilisée.
- Réessais avec backoff exponentiel sur erreurs réseau, 429 et 5xx.
- Pagination par curseur suivie automatiquement.
- Erreurs RFC 7807 traduites en `TmsError` lisibles.
- Petit cache TTL pour les données de référence (utilisateurs d'un rôle, itinéraires).
"""

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from tms_mcp.connector.base import TmsError, TmsNotFound, TmsUnavailable

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


class _TtlCache:
    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        return None

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)


class RestTmsConnector:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 15.0,
        max_retries: int = 3,
        max_pages: int = 20,
        cache_ttl_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.max_pages = max_pages
        self._cache = _TtlCache(cache_ttl_s)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers={"Accept": "application/json", "User-Agent": "tms-mcp-server/0.1"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- HTTP -------------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cleaned_params = {
            k: v for k, v in (params or {}).items() if v not in (None, [], "")
        }
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.get(path, params=cleaned_params)
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code not in _RETRYABLE_STATUS:
                    raise self._error(resp)
                last_exc = self._error(resp)
            if attempt < self.max_retries:
                delay = min(0.25 * 2**attempt, 4.0) + random.uniform(0, 0.2)
                log.warning(
                    "TMS GET %s échoué (%s), nouvelle tentative dans %.2fs",
                    path,
                    last_exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise TmsUnavailable(
            f"TMS injoignable pour GET {path} : {last_exc}"
        ) from last_exc

    @staticmethod
    def _error(resp: httpx.Response) -> TmsError:
        try:
            body = resp.json()
        except ValueError:
            body = {"title": resp.text[:200]}
        title = body.get("title") or resp.reason_phrase
        detail = body.get("detail")
        message = f"TMS {resp.status_code} {title}" + (f" : {detail}" if detail else "")
        cls = TmsNotFound if resp.status_code == 404 else TmsError
        return cls(message, status=resp.status_code, detail=body)

    async def _get_all(
        self, path: str, params: dict[str, Any] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Suit la pagination par curseur (`pagination.cursor`)."""
        params = {**(params or {}), "limit": limit}
        items: list[dict[str, Any]] = []
        for _ in range(self.max_pages):
            page = await self._get(path, params)
            items.extend(page.get("data", []))
            pagination = page.get("pagination") or {}
            if not pagination.get("has_more") or not pagination.get("cursor"):
                return items
            params["cursor"] = pagination["cursor"]
        log.warning("Pagination tronquée sur %s après %d pages", path, self.max_pages)
        return items

    # --- Meta -------------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self._get("/health")

    async def now(self) -> datetime:
        """Heure courante du TMS (accélérée en simulation) : référence pour toutes les durées."""
        dt = parse_datetime((await self.health()).get("time"))
        if dt is None:
            raise TmsError("le TMS n'a pas renvoyé d'heure courante valide")
        return dt

    # --- Missions ---------------------------------------------------------------------

    async def list_missions(
        self, *, status=None, vehicle_id=None, driver_id=None, updated_since=None
    ) -> list[dict[str, Any]]:
        return await self._get_all(
            "/missions",
            {
                "status": status,
                "vehicle_id": vehicle_id,
                "driver_id": driver_id,
                "updated_since": _iso(updated_since),
            },
        )

    async def get_mission(self, mission_id: str) -> dict[str, Any]:
        return await self._get(f"/missions/{mission_id}")

    async def list_mission_events(
        self, mission_id: str, *, since=None, types=None
    ) -> list[dict[str, Any]]:
        return await self._get_all(
            f"/missions/{mission_id}/events", {"since": _iso(since), "type": types}
        )

    async def get_mission_route(self, mission_id: str) -> dict[str, Any]:
        key = f"route:{mission_id}"
        if (cached := self._cache.get(key)) is not None:
            return cached
        route = await self._get(f"/missions/{mission_id}/route")
        self._cache.put(key, route)
        return route

    async def get_mission_deviation(self, mission_id: str) -> dict[str, Any]:
        return await self._get(f"/missions/{mission_id}/deviation")

    async def get_mission_eta(self, mission_id: str) -> dict[str, Any]:
        return await self._get(f"/missions/{mission_id}/eta")

    # --- Véhicules --------------------------------------------------------------------

    async def list_vehicles(
        self, *, status=None, fleet_id=None
    ) -> list[dict[str, Any]]:
        return await self._get_all(
            "/vehicles", {"status": status, "fleet_id": fleet_id}
        )

    async def get_vehicle(self, vehicle_id: str) -> dict[str, Any]:
        return await self._get(f"/vehicles/{vehicle_id}")

    async def get_vehicle_position(self, vehicle_id: str) -> dict[str, Any]:
        return await self._get(f"/vehicles/{vehicle_id}/position")

    async def list_vehicle_positions(
        self, vehicle_id: str, *, since=None, until=None, mission_id=None
    ) -> list[dict[str, Any]]:
        return await self._get_all(
            f"/vehicles/{vehicle_id}/positions",
            {"since": _iso(since), "until": _iso(until), "mission_id": mission_id},
            limit=2000,
        )

    # --- Utilisateurs -----------------------------------------------------------------

    async def get_driver(self, driver_id: str) -> dict[str, Any]:
        return await self._get(f"/drivers/{driver_id}")

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return await self._get(f"/users/{user_id}")

    async def list_users(
        self, *, role=None, management_level=None
    ) -> list[dict[str, Any]]:
        return await self._get_all(
            "/users", {"role": role, "management_level": management_level}
        )

    async def list_role_users(self, role: str) -> list[dict[str, Any]]:
        key = f"role:{role}"
        if (cached := self._cache.get(key)) is not None:
            return cached
        users = await self._get_all(f"/roles/{role}/users")
        self._cache.put(key, users)
        return users
