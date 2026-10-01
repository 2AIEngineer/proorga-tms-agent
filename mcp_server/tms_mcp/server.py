"""Serveur MCP de l'orchestrateur (Document 2).

Deux familles de tools :

- **A. Tools TMS** (observation, lecture seule) : adossés au connecteur TMS (contrat pivot).
- **B. Tools de l'orchestrateur** (action, journalisation, audit) : adossés à la base de l'agent.

Aucun tool n'écrit dans le TMS. Les descriptions expliquent l'intention métier et le moment
d'usage : c'est ce qui permet à un agent LLM de comprendre un TMS qu'il n'a jamais vu.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tms_mcp import __version__
from tms_mcp.config import Settings, get_settings
from tms_mcp.connector import RestTmsConnector, TmsConnector, TmsError, TmsNotFound, TmsUnavailable, parse_datetime
from tms_mcp.models import (
    AgentLogEntry,
    AgentLogList,
    Alert,
    AlertCreated,
    AlertDetail,
    AlertList,
    Deviation,
    Driver,
    DriverCallResult,
    Eta,
    EventList,
    MissionDetail,
    MissionIndicator,
    MissionList,
    MissionSnapshot,
    NotificationList,
    OverviewOut,
    PositionHistory,
    RouteOut,
    TmsTime,
    UserList,
    Vehicle,
    VehiclePosition,
)
from tms_mcp.notifications import NotificationService
from tms_mcp.store import AgentStore, iso

log = logging.getLogger(__name__)
T = TypeVar("T")

MissionStatus = Literal["planned", "in_progress", "completed", "cancelled"]
EventType = Literal[
    "arrived_pickup",
    "loading_started",
    "loading_completed",
    "departed_pickup",
    "arrived_delivery",
    "unloading_started",
    "unloading_completed",
    "driver_message",
    "deviation_detected",
]
Severity = Literal["low", "medium", "high", "critical"]
Channel = Literal["in_app", "sms", "email", "call"]

INSTRUCTIONS = """\
Serveur MCP de surveillance des missions de transport.

Famille A — TMS (lecture seule, backend `tms`) : missions, événements, véhicules, positions,
itinéraires, écarts, ETA, chauffeurs et utilisateurs. Le TMS n'est jamais modifié.
Famille B — orchestrateur (backend `agent`) : alertes, notifications, appels chauffeur et
journal d'audit, stockés dans la base de l'agent.

Boucle de surveillance type : `get_active_missions` → `get_mission_snapshot` pour chaque mission
→ évaluation des règles métier → `create_alert` (déduplication/cooldown intégrés) →
`notify_recipients` → `log_agent_action`.
Pour enquêter sur une situation : `get_mission_snapshot`, `get_mission_events`,
`get_vehicle_positions`, `list_alerts`, `get_alert`.
Toutes les durées sont calculées avec l'heure du TMS (`get_tms_time`), qui peut être accélérée.
"""


def _meta(category: str, backend: str, *, domain: str = "transport", latency: str = "fast", frequency: str = "medium",
          read_only: bool = True, cost: str | None = None) -> dict[str, Any]:
    meta = {
        "domain": domain,
        "category": category,
        "backend": backend,
        "read_only": read_only,
        "latency_hint": latency,
        "call_frequency_hint": frequency,
    }
    if cost:
        meta["cost_hint"] = cost
    return meta


_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
_READ_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_WRITE_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_WRITE_IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
_ACTION_EXTERNAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


class Recipient(BaseModel):
    """Destinataire d'une notification, tel que décrit dans le bloc `notify` d'une règle."""

    model_config = ConfigDict(extra="forbid")

    role: str | None = Field(None, description="Rôle à résoudre via le TMS (driver, ops_agent, ops_supervisor, ops_manager, director...). `driver` = chauffeur de la mission.")
    user_id: str | None = Field(None, description="Identifiant d'un utilisateur précis du TMS (à la place de `role`).")
    management_level: str = Field(description="Niveau de management : M+0, M+1, M+2, M+3, M+4.")
    channel: Channel = Field(description="Canal : in_app, sms, email ou call.")
    message: str | None = Field(None, description="Message spécifique à ce destinataire (sinon message commun).")

    @model_validator(mode="after")
    def _role_xor_user(self):
        if (self.role is None) == (self.user_id is None):
            raise ValueError("préciser exactement l'un des champs `role` ou `user_id`")
        return self


class Services:
    """Dépendances partagées par les tools."""

    def __init__(self, settings: Settings, connector: TmsConnector, store: AgentStore):
        self.settings = settings
        self.connector = connector
        self.store = store
        self.notifications = NotificationService(connector, store)

    async def now(self) -> datetime:
        """Heure du TMS ; repli sur l'horloge système si le TMS ne répond pas (écritures locales)."""
        try:
            return await self.connector.now()
        except TmsError as exc:
            log.warning("Heure du TMS indisponible (%s) : horloge système utilisée", exc)
            return datetime.now(UTC).replace(microsecond=0)


async def _tms(awaitable: Awaitable[T]) -> T:
    """Traduit les erreurs du connecteur en erreurs de tool lisibles par le LLM."""
    try:
        return await awaitable
    except TmsNotFound as exc:
        raise ToolError(f"Ressource introuvable dans le TMS : {exc}") from exc
    except TmsUnavailable as exc:
        raise ToolError(f"TMS temporairement indisponible, réessayer plus tard : {exc}") from exc
    except TmsError as exc:
        raise ToolError(str(exc)) from exc


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    dt = parse_datetime(value)
    if dt is None:
        raise ToolError(f"`{name}` doit être une date ISO 8601 (reçu {value!r})")
    return dt


def create_server(
    settings: Settings | None = None,
    *,
    connector: TmsConnector | None = None,
    store: AgentStore | None = None,
) -> MCPServer:
    settings = settings or get_settings()
    owns_connector = connector is None
    connector = connector or RestTmsConnector(
        settings.tms_api_url,
        timeout_s=settings.tms_timeout_s,
        max_retries=settings.tms_max_retries,
        max_pages=settings.tms_max_pages,
        cache_ttl_s=settings.reference_cache_ttl_s,
    )
    store = store or AgentStore(settings.agent_db_path)
    svc = Services(settings, connector, store)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        try:
            yield {"services": svc}
        finally:
            if owns_connector:
                await connector.aclose()

    mcp = MCPServer(
        "tms-orchestrator",
        title="Orchestrateur de suivi des missions",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )
    mcp.services = svc  # accès direct (tests, CLI)

    # =================================================================================
    # A. Tools TMS — observation, lecture seule
    # =================================================================================

    @mcp.tool(
        title="Heure courante du TMS",
        annotations=_READ,
        meta=_meta("meta", "tms", frequency="high"),
    )
    async def get_tms_time() -> TmsTime:
        """Retourne l'heure courante du TMS et l'état de sa simulation. Le TMS peut tourner en temps
        accéléré : utiliser cette heure (et non l'horloge système) comme référence `now` pour calculer
        des âges et des durées, et comme valeur de `updated_since` au cycle de surveillance suivant."""
        health = await _tms(connector.health())
        return TmsTime(time=health["time"], simulation=health.get("simulation"))

    @mcp.tool(
        title="Lister les missions en cours",
        annotations=_READ,
        meta=_meta("observation", "tms", frequency="high"),
    )
    async def get_active_missions(
        vehicle_id: Annotated[str | None, Field(description="Filtre optionnel sur un véhicule précis.")] = None,
        updated_since: Annotated[str | None, Field(description="Ne retourne que les missions mises à jour après cette date ISO 8601 (polling incrémental).")] = None,
    ) -> MissionList:
        """Retourne toutes les missions actuellement en cours d'exécution (statut `in_progress`), avec
        origine, destination, véhicule, chauffeur, horaires planifiés et ETA courante. C'est le point
        d'entrée recommandé pour toute boucle de surveillance : obtenir la vue d'ensemble des transports
        à surveiller avant une analyse plus fine. Attention : avec `updated_since`, une mission bloquée
        (qui ne reçoit plus de mise à jour) n'apparaît plus ; pour détecter les immobilisations, lister
        sans ce filtre. Ne pas utiliser pour les missions planifiées ou terminées (voir `list_missions`)."""
        since = _parse_dt(updated_since, "updated_since")
        missions, now = await asyncio.gather(
            _tms(connector.list_missions(status=["in_progress"], vehicle_id=vehicle_id, updated_since=since)),
            svc.now(),
        )
        return MissionList(tms_time=iso(now), count=len(missions), missions=missions)

    @mcp.tool(
        title="Lister les missions (tous statuts)",
        annotations=_READ,
        meta=_meta("observation", "tms"),
    )
    async def list_missions(
        status: Annotated[list[MissionStatus] | None, Field(description="Statut(s) à inclure : planned, in_progress, completed, cancelled.")] = None,
        updated_since: Annotated[str | None, Field(description="Missions modifiées après cette date ISO 8601.")] = None,
        vehicle_id: Annotated[str | None, Field(description="Filtre sur un véhicule.")] = None,
        driver_id: Annotated[str | None, Field(description="Filtre sur un chauffeur.")] = None,
    ) -> MissionList:
        """Retourne les missions filtrées par statut, véhicule, chauffeur ou date de mise à jour. Utiliser
        ce tool pour retrouver des missions planifiées, terminées ou annulées — par exemple pour savoir
        si une mission surveillée vient de se terminer (et clore ses alertes), ou pour l'historique d'un
        chauffeur. Pour la surveillance courante, préférer `get_active_missions`."""
        since = _parse_dt(updated_since, "updated_since")
        missions, now = await asyncio.gather(
            _tms(connector.list_missions(status=status, vehicle_id=vehicle_id, driver_id=driver_id, updated_since=since)),
            svc.now(),
        )
        return MissionList(tms_time=iso(now), count=len(missions), missions=missions)

    @mcp.tool(
        title="Détail d'une mission",
        annotations=_READ,
        meta=_meta("observation", "tms"),
    )
    async def get_mission_details(
        mission_id: Annotated[str, Field(description="Identifiant de la mission (ex. msn_...).")],
    ) -> MissionDetail:
        """Retourne les détails complets d'une mission : référence, origine, destination, véhicule,
        chauffeur, horaires planifiés et réels, ETA recalculée, statut et chronologie complète des
        événements. Utiliser ce tool lorsqu'on a besoin du contexte complet d'une mission identifiée,
        par exemple pour évaluer un retard ou expliquer une alerte."""
        return await _tms(connector.get_mission(mission_id))

    @mcp.tool(
        title="Chronologie des événements d'une mission",
        annotations=_READ,
        meta=_meta("observation", "tms", frequency="high"),
    )
    async def get_mission_events(
        mission_id: Annotated[str, Field(description="Identifiant de la mission.")],
        since: Annotated[str | None, Field(description="Ne retourne que les événements survenus après cette date ISO 8601.")] = None,
        type: Annotated[list[EventType] | None, Field(description="Filtre sur un ou plusieurs types d'événement.")] = None,
    ) -> EventList:
        """Retourne la chronologie des événements d'une mission, triés par date : arrivée au point de
        chargement, chargement commencé/terminé, départ, arrivée à la livraison, déchargement, messages
        du chauffeur (`driver_message`, avec `payload.severity` et `payload.text`) et dérives détectées
        par le TMS. Utiliser ce tool pour reconstituer l'historique d'une mission, repérer les nouveaux
        événements depuis le dernier cycle (`since`), ou détecter une immobilisation anormale ou une
        absence de mise à jour."""
        events = await _tms(
            connector.list_mission_events(mission_id, since=_parse_dt(since, "since"), types=type)
        )
        return EventList(mission_id=mission_id, count=len(events), events=events)

    @mcp.tool(
        title="Détail d'un véhicule",
        annotations=_READ,
        meta=_meta("observation", "tms", domain="fleet"),
    )
    async def get_vehicle_details(
        vehicle_id: Annotated[str, Field(description="Identifiant du véhicule (ex. veh_...).")],
    ) -> Vehicle:
        """Retourne la fiche d'un véhicule : immatriculation, flotte, statut (`idle`, `moving`, `stopped`,
        `offline`), dernière position, date de dernière émission et mission en cours. Utiliser ce tool
        pour savoir si un camion roule, est arrêté ou ne transmet plus — information clé pour distinguer
        une immobilisation d'une simple absence de mise à jour."""
        return await _tms(connector.get_vehicle(vehicle_id))

    @mcp.tool(
        title="Position GPS d'un véhicule",
        annotations=_READ,
        meta=_meta("observation", "tms", domain="fleet", frequency="high"),
    )
    async def get_vehicle_position(
        vehicle_id: Annotated[str, Field(description="Identifiant du véhicule.")],
    ) -> VehiclePosition:
        """Retourne la dernière position GPS connue d'un véhicule, avec cap, vitesse et horodatage.
        Utiliser ce tool pour localiser un camion en temps réel, par exemple avant de le comparer à
        l'itinéraire planifié ou pour vérifier une arrivée."""
        return await _tms(connector.get_vehicle_position(vehicle_id))

    @mcp.tool(
        title="Historique des positions d'un véhicule",
        annotations=_READ,
        meta=_meta("observation", "tms", domain="fleet", latency="medium", frequency="low"),
    )
    async def get_vehicle_positions(
        vehicle_id: Annotated[str, Field(description="Identifiant du véhicule.")],
        since: Annotated[str | None, Field(description="Positions enregistrées après cette date ISO 8601.")] = None,
        until: Annotated[str | None, Field(description="Positions enregistrées avant cette date ISO 8601.")] = None,
        mission_id: Annotated[str | None, Field(description="Limiter aux positions d'une mission.")] = None,
        last: Annotated[int, Field(ge=1, le=500, description="Nombre maximal de positions renvoyées (les plus récentes).")] = 30,
    ) -> PositionHistory:
        """Retourne l'historique des positions GPS d'un véhicule (lat, lon, cap, vitesse, horodatage),
        limité aux `last` plus récentes. Utiliser ce tool pour enquêter : depuis quand le camion est-il
        à l'arrêt, quel trajet a-t-il suivi pendant une dérive, à quelle vitesse roulait-il."""
        positions = await _tms(
            connector.list_vehicle_positions(
                vehicle_id, since=_parse_dt(since, "since"), until=_parse_dt(until, "until"), mission_id=mission_id
            )
        )
        positions = positions[-last:]
        return PositionHistory(vehicle_id=vehicle_id, count=len(positions), positions=positions)

    @mcp.tool(
        title="Itinéraire planifié d'une mission",
        annotations=_READ,
        meta=_meta("observation", "tms", frequency="low"),
    )
    async def get_mission_route(
        mission_id: Annotated[str, Field(description="Identifiant de la mission.")],
        include_geometry: Annotated[bool, Field(description="Inclure la polyline et tous les waypoints (volumineux).")] = False,
    ) -> RouteOut:
        """Retourne l'itinéraire planifié d'une mission : distance, durée prévue, points de départ et
        d'arrivée, et sur demande la géométrie complète (waypoints, polyline encodée Google). Utiliser ce
        tool pour disposer de la référence géographique à laquelle comparer la position du véhicule.
        Pour détecter une dérive, préférer `get_mission_deviation`, déjà calculé par le TMS."""
        route = await _tms(connector.get_mission_route(mission_id))
        waypoints = sorted(route.get("waypoints") or [], key=lambda w: w.get("sequence", 0))
        return RouteOut(
            id=route.get("id"),
            mission_id=route.get("mission_id", mission_id),
            distance_km=route.get("distance_km"),
            duration_min=route.get("duration_min"),
            waypoint_count=len(waypoints),
            start=waypoints[0] if waypoints else None,
            end=waypoints[-1] if waypoints else None,
            polyline=route.get("polyline") if include_geometry else None,
            waypoints=waypoints if include_geometry else None,
        )

    @mcp.tool(
        title="Écart à l'itinéraire",
        annotations=_READ,
        meta=_meta("observation", "tms", frequency="high"),
    )
    async def get_mission_deviation(
        mission_id: Annotated[str, Field(description="Identifiant de la mission.")],
    ) -> Deviation:
        """Retourne l'écart courant entre la position du véhicule et l'itinéraire planifié (km), depuis
        quand cet écart persiste, sa durée en minutes et sa sévérité (low/medium/high), calculés par le
        TMS. `current_offset_km` est null tant que le véhicule n'a pas quitté le point de chargement.
        Utiliser ce tool en priorité pour détecter une dérive, plutôt que de recalculer l'écart à partir
        de `get_vehicle_position` et `get_mission_route`."""
        return await _tms(connector.get_mission_deviation(mission_id))

    @mcp.tool(
        title="ETA recalculée",
        annotations=_READ,
        meta=_meta("observation", "tms", frequency="high"),
    )
    async def get_mission_eta(
        mission_id: Annotated[str, Field(description="Identifiant de la mission.")],
    ) -> Eta:
        """Retourne l'ETA recalculée d'une mission à partir de la position actuelle, l'heure de livraison
        planifiée, le retard en minutes (négatif = avance) et la distance restante. Utiliser ce tool pour
        évaluer un retard par rapport au planning de livraison."""
        return await _tms(connector.get_mission_eta(mission_id))

    @mcp.tool(
        title="Fiche chauffeur",
        annotations=_READ,
        meta=_meta("observation", "tms", domain="fleet", frequency="low"),
    )
    async def get_driver_details(
        driver_id: Annotated[str, Field(description="Identifiant du chauffeur (ex. drv_...).")],
    ) -> Driver:
        """Retourne la fiche d'un chauffeur depuis le TMS : nom, téléphone, e-mail, statut (`available` /
        `on_mission`) et mission en cours. Utiliser ce tool pour connaître le contact du chauffeur d'une
        mission avant une escalade, ou pour contextualiser une alerte."""
        return await _tms(connector.get_driver(driver_id))

    @mcp.tool(
        title="Lister les utilisateurs",
        annotations=_READ,
        meta=_meta("observation", "tms", domain="alerting", frequency="low"),
    )
    async def list_users(
        role: Annotated[str | None, Field(description="Rôle : driver, ops_agent, ops_supervisor, ops_manager, director...")] = None,
        management_level: Annotated[str | None, Field(description="Niveau : M+0 à M+4.")] = None,
    ) -> UserList:
        """Retourne les utilisateurs du TMS (nom, rôle, niveau de management, téléphone, e-mail), filtrés
        par rôle ou niveau. Utiliser ce tool pour savoir qui occupe un rôle de la chaîne d'escalade
        (agent de suivi M+1, superviseur M+2, manager M+3, direction M+4) avant de notifier ou d'expliquer
        qui a été prévenu."""
        if role and not management_level:
            users = await _tms(connector.list_role_users(role))
        else:
            users = await _tms(connector.list_users(role=role, management_level=management_level))
        return UserList(count=len(users), users=users)

    @mcp.tool(
        title="Instantané complet d'une mission",
        annotations=_READ,
        meta=_meta("observation", "tms", latency="medium", frequency="high"),
    )
    async def get_mission_snapshot(
        mission_id: Annotated[str, Field(description="Identifiant de la mission.")],
    ) -> MissionSnapshot:
        """Retourne en un seul appel tout le contexte de surveillance d'une mission : détail et événements,
        véhicule (statut, position, vitesse), écart à l'itinéraire, ETA, fiche chauffeur et heure du TMS.
        C'est l'entrée attendue par le moteur de règles. Utiliser ce tool à chaque cycle pour chaque
        mission en cours, ou pour démarrer une enquête, plutôt que d'enchaîner cinq appels. Si une
        ressource secondaire est illisible, elle est signalée dans `errors` et le reste est renvoyé."""
        mission = await _tms(connector.get_mission(mission_id))
        tasks: dict[str, Awaitable[Any]] = {
            "deviation": connector.get_mission_deviation(mission_id),
            "eta": connector.get_mission_eta(mission_id),
            "tms_time": connector.now(),
        }
        if mission.get("vehicle_id"):
            tasks["vehicle"] = connector.get_vehicle(mission["vehicle_id"])
        if mission.get("driver_id"):
            tasks["driver"] = connector.get_driver(mission["driver_id"])
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        data: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, result in zip(tasks, results):
            if isinstance(result, Exception):
                errors[key] = f"{type(result).__name__}: {result}"
            else:
                data[key] = result
        tms_time = data.pop("tms_time", None) or datetime.now(UTC)
        return MissionSnapshot(tms_time=iso(tms_time), mission=mission, errors=errors, **data)

    # =================================================================================
    # B. Tools de l'orchestrateur — base de l'agent
    # =================================================================================

    @mcp.tool(
        title="Émettre une alerte",
        annotations=_WRITE_IDEMPOTENT,
        meta=_meta("action", "agent", domain="alerting", read_only=False),
    )
    async def create_alert(
        rule_id: Annotated[str, Field(description="Identifiant de la règle métier déclenchée.")],
        rule_version: Annotated[int, Field(ge=1, description="Version de la règle (traçabilité).")],
        severity: Annotated[Severity, Field(description="Sévérité : low, medium, high, critical.")],
        title: Annotated[str, Field(description="Titre court de l'alerte.")],
        mission_id: Annotated[str | None, Field(description="Mission du TMS concernée.")] = None,
        event_id: Annotated[str | None, Field(description="Événement déclencheur (règles d'événement).")] = None,
        category: Annotated[str | None, Field(description="Catégorie : route_deviation, delay, immobilization, data_gap, safety...")] = None,
        message: Annotated[str | None, Field(description="Message détaillé, lisible par un humain.")] = None,
        observed_data: Annotated[dict[str, Any] | None, Field(description="Données lues dans le TMS ayant déclenché la règle (pourquoi l'agent alerte).")] = None,
        deduplication_key: Annotated[str | None, Field(description="Clé de déduplication de la règle (ex. deviation:msn_123).")] = None,
        cooldown_minutes: Annotated[float, Field(ge=0, description="Délai minimal (temps TMS) avant ré-émission d'une alerte de même clé.")] = 0,
    ) -> AlertCreated:
        """Enregistre une alerte dans la base de l'agent pour une règle métier déclenchée, en appliquant
        atomiquement la déduplication (pas de nouvelle alerte si une alerte ouverte porte la même clé)
        et le cooldown (pas de ré-émission avant `cooldown_minutes`, même si la précédente est résolue).
        Utiliser ce tool uniquement pour matérialiser une règle déclenchée par le moteur de règles, puis
        appeler `notify_recipients` si `created` est vrai. Le TMS n'est jamais modifié."""
        now = await svc.now()
        result = store.create_alert(
            now=now,
            rule_id=rule_id,
            rule_version=rule_version,
            severity=severity,
            title=title,
            mission_id=mission_id,
            event_id=event_id,
            category=category,
            message=message,
            observed_data=observed_data,
            deduplication_key=deduplication_key,
            cooldown_minutes=cooldown_minutes,
        )
        if result.created:
            store.add_log(
                now=now,
                type="alert_emitted",
                mission_id=mission_id,
                alert_id=result.alert["id"],
                payload={"rule_id": rule_id, "rule_version": rule_version, "severity": severity, "title": title},
            )
        return AlertCreated(created=result.created, reason=result.reason, alert=result.alert)

    @mcp.tool(
        title="Lister les alertes",
        annotations=_READ_LOCAL,
        meta=_meta("observation", "agent", domain="alerting", frequency="high"),
    )
    async def list_alerts(
        mission_id: Annotated[str | None, Field(description="Filtre sur une mission.")] = None,
        severity: Annotated[list[Severity] | None, Field(description="Filtre sur une ou plusieurs sévérités.")] = None,
        status: Annotated[Literal["open", "resolved"] | None, Field(description="open ou resolved.")] = None,
        deduplication_key: Annotated[str | None, Field(description="Filtre sur une clé de déduplication.")] = None,
        rule_id: Annotated[str | None, Field(description="Filtre sur une règle.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Nombre maximal d'alertes (les plus récentes).")] = 50,
    ) -> AlertList:
        """Retourne les alertes émises par l'agent, stockées dans sa base (les plus récentes d'abord),
        avec données observées, statut et éventuelle analyse. Utiliser ce tool pour éviter de dupliquer
        une alerte déjà émise, vérifier l'état d'escalade d'une mission, ou faire le point sur les
        situations ouvertes."""
        alerts = store.list_alerts(
            mission_id=mission_id, severity=severity, status=status,
            deduplication_key=deduplication_key, rule_id=rule_id, limit=limit,
        )
        return AlertList(count=len(alerts), alerts=alerts)

    @mcp.tool(
        title="Détail d'une alerte",
        annotations=_READ_LOCAL,
        meta=_meta("observation", "agent", domain="alerting"),
    )
    async def get_alert(
        alert_id: Annotated[str, Field(description="Identifiant de l'alerte (ex. alr_...).")],
    ) -> AlertDetail:
        """Retourne une alerte complète (règle, données observées, message, analyse) avec toutes ses
        notifications. Utiliser ce tool pour expliquer pourquoi l'agent a alerté et qui a été prévenu."""
        alert = store.get_alert(alert_id)
        if alert is None:
            raise ToolError(f"Alerte {alert_id!r} introuvable")
        return AlertDetail(alert=alert, notifications=store.list_notifications(alert_id))

    @mcp.tool(
        title="Clore une alerte",
        annotations=_WRITE_IDEMPOTENT,
        meta=_meta("action", "agent", domain="alerting", read_only=False, frequency="low"),
    )
    async def resolve_alert(
        alert_id: Annotated[str, Field(description="Identifiant de l'alerte à clore.")],
        reason: Annotated[str, Field(description="Motif de clôture (ex. « écart résorbé », « mission terminée »).")],
    ) -> Alert:
        """Passe une alerte au statut `resolved` dans la base de l'agent, avec un motif. Utiliser ce tool
        quand la situation qui a déclenché l'alerte a disparu (le véhicule a rejoint l'itinéraire, la
        mission est terminée...). Une alerte résolue reste soumise au cooldown de sa règle."""
        now = await svc.now()
        if store.get_alert(alert_id) is None:
            raise ToolError(f"Alerte {alert_id!r} introuvable")
        alert = store.resolve_alert(alert_id, now=now, reason=reason)
        store.add_log(now=now, type="alert_resolved", mission_id=alert.get("mission_id"), alert_id=alert_id,
                      payload={"reason": reason})
        return alert

    @mcp.tool(
        title="Joindre une analyse à une alerte",
        annotations=_WRITE_IDEMPOTENT,
        meta=_meta("action", "agent", domain="alerting", read_only=False, frequency="low"),
    )
    async def annotate_alert(
        alert_id: Annotated[str, Field(description="Identifiant de l'alerte.")],
        analysis: Annotated[dict[str, Any], Field(description="Analyse structurée : summary, probable_cause, risk_level, recommended_actions, confidence...")],
    ) -> Alert:
        """Attache à une alerte l'analyse de situation produite par l'agent (diagnostic, cause probable,
        risque, actions recommandées). Utiliser ce tool après une enquête pour que les opérateurs
        disposent d'un contexte exploitable en plus des données brutes."""
        now = await svc.now()
        if store.get_alert(alert_id) is None:
            raise ToolError(f"Alerte {alert_id!r} introuvable")
        alert = store.annotate_alert(alert_id, {**analysis, "analyzed_at": iso(now)})
        store.add_log(now=now, type="alert_analyzed", mission_id=alert.get("mission_id"), alert_id=alert_id,
                      payload={"summary": analysis.get("summary")})
        return alert

    @mcp.tool(
        title="Notifier les destinataires d'une alerte",
        annotations=_ACTION_EXTERNAL,
        meta=_meta("action", "agent", domain="alerting", read_only=False, latency="medium", cost="sms/appel facturables"),
    )
    async def notify_recipients(
        alert_id: Annotated[str, Field(description="Alerte à notifier.")],
        recipients: Annotated[list[Recipient], Field(min_length=1, description="Destinataires : [{role | user_id, management_level, channel, message?}].")],
        message: Annotated[str | None, Field(description="Message commun (sinon message de l'alerte).")] = None,
    ) -> NotificationList:
        """Envoie une notification à une liste de destinataires pour une alerte donnée. Chaque
        destinataire est identifié par son rôle ou son identifiant utilisateur, avec un niveau de
        management (M+0, M+1, M+2...) et un canal (in_app, sms, email, call). Le tool résout les rôles
        en utilisateurs concrets en lisant le TMS (le rôle `driver` désigne le chauffeur de la mission),
        envoie, puis enregistre chaque notification et son statut de livraison dans la base de l'agent.
        Utiliser ce tool juste après `create_alert`, avec la liste de destinataires prescrite par la règle."""
        alert = store.get_alert(alert_id)
        if alert is None:
            raise ToolError(f"Alerte {alert_id!r} introuvable")
        now = await svc.now()
        notifications = await svc.notifications.notify(
            alert=alert, recipients=[r.model_dump() for r in recipients], message=message, now=now
        )
        delivered = sum(n["status"] == "delivered" for n in notifications)
        store.add_log(
            now=now, type="notification_sent", mission_id=alert.get("mission_id"), alert_id=alert_id,
            payload={"delivered": delivered, "failed": len(notifications) - delivered,
                     "recipients": [f"{n.get('recipient_role')}:{n.get('recipient_user_id')}/{n['channel']}" for n in notifications]},
        )
        return NotificationList(alert_id=alert_id, count=len(notifications), delivered=delivered,
                                failed=len(notifications) - delivered, notifications=notifications)

    @mcp.tool(
        title="Notifications d'une alerte",
        annotations=_READ_LOCAL,
        meta=_meta("observation", "agent", domain="alerting", frequency="low"),
    )
    async def list_alert_notifications(
        alert_id: Annotated[str, Field(description="Identifiant de l'alerte.")],
    ) -> NotificationList:
        """Retourne la liste des notifications émises pour une alerte : destinataire, rôle, niveau de
        management, canal, coordonnée utilisée, statut et horodatage de livraison. Utiliser ce tool pour
        vérifier qui a été notifié, quand et comment — reporting, audit, ou contrôle d'une escalade."""
        notifications = store.list_notifications(alert_id)
        delivered = sum(n["status"] == "delivered" for n in notifications)
        return NotificationList(alert_id=alert_id, count=len(notifications), delivered=delivered,
                                failed=len(notifications) - delivered, notifications=notifications)

    @mcp.tool(
        title="Appeler le chauffeur (simulé)",
        annotations=_ACTION_EXTERNAL,
        meta=_meta("action", "agent", domain="fleet", read_only=False, latency="slow", frequency="low", cost="appel téléphonique"),
    )
    async def call_driver(
        mission_id: Annotated[str, Field(description="Mission dont le chauffeur doit être appelé.")],
        reason: Annotated[str, Field(min_length=5, description="Motif de l'appel, formulé pour le chauffeur.")],
        alert_id: Annotated[str | None, Field(description="Alerte justifiant l'appel (recommandé).")] = None,
    ) -> DriverCallResult:
        """Simule un appel téléphonique vers le chauffeur d'une mission pour gérer une situation. Le
        numéro est lu dans le TMS (fiche chauffeur). Dans le POC, l'appel est journalisé dans la base de
        l'agent et ne déclenche pas de communication réelle. Utiliser ce tool uniquement lorsqu'une règle
        métier de sévérité élevée (high ou critical) s'est déclenchée sur la mission et que l'escalade
        téléphonique est justifiée : le tool refuse l'appel sans alerte ouverte de ce niveau, et ne
        rappelle pas le même chauffeur pour la même mission à moins de 15 minutes d'intervalle."""
        now = await svc.now()
        if alert_id:
            alert = store.get_alert(alert_id)
            if alert is None or alert.get("mission_id") != mission_id:
                raise ToolError(f"Alerte {alert_id!r} introuvable pour la mission {mission_id}")
            justifying = [alert] if alert["status"] == "open" and alert["severity"] in ("high", "critical") else []
        else:
            justifying = store.list_alerts(mission_id=mission_id, status="open", severity=["high", "critical"], limit=1)
        if not justifying:
            raise ToolError(
                "Appel refusé : aucune alerte ouverte de sévérité high ou critical ne justifie une escalade "
                f"téléphonique pour la mission {mission_id}. Utiliser `notify_recipients` ou `log_agent_action`."
            )
        last = store.last_driver_call(mission_id)
        if last:
            elapsed = now - datetime.fromisoformat(last["started_at"].replace("Z", "+00:00"))
            if timedelta(0) <= elapsed < timedelta(minutes=settings.driver_call_min_interval_min):
                return DriverCallResult(placed=False, call=last,
                                        note=f"Chauffeur déjà appelé il y a {elapsed.total_seconds() / 60:.0f} min ; pas de nouvel appel.")
        mission = await _tms(connector.get_mission(mission_id))
        driver = await _tms(connector.get_driver(mission["driver_id"]))
        if not driver.get("phone"):
            raise ToolError(f"Le chauffeur {driver['id']} n'a pas de numéro de téléphone dans le TMS")
        call = store.add_driver_call(
            mission_id=mission_id, alert_id=justifying[0]["id"], driver_id=driver["id"],
            driver_name=driver.get("name"), phone=driver["phone"], reason=reason,
            status="completed_simulated", started_at=iso(now), ended_at=iso(now + timedelta(minutes=2)),
        )
        store.add_log(now=now, type="driver_called", mission_id=mission_id, alert_id=justifying[0]["id"],
                      payload={"call_id": call["id"], "driver_id": driver["id"], "phone": driver["phone"], "reason": reason})
        log.info("[SIMULATION] appel %s (%s) : %s", driver.get("name"), driver["phone"], reason)
        return DriverCallResult(placed=True, call=call, note="Appel simulé effectué et journalisé.")

    @mcp.tool(
        title="Journaliser une action de l'agent",
        annotations=_WRITE_LOCAL,
        meta=_meta("action", "agent", domain="alerting", read_only=False),
    )
    async def log_agent_action(
        type: Annotated[str, Field(description="Type d'action : alert_emitted, driver_called, notification_sent, decision, observation, report...")],
        mission_id: Annotated[str | None, Field(description="Mission concernée.")] = None,
        payload: Annotated[dict[str, Any] | None, Field(description="Détails libres de l'action (décision, motif, données).")] = None,
        alert_id: Annotated[str | None, Field(description="Alerte concernée.")] = None,
    ) -> AgentLogEntry:
        """Enregistre une action ou une décision de l'agent dans son journal d'audit, rattachée à une
        mission et/ou une alerte (par exemple une alerte générée, un appel chauffeur, une décision de ne
        pas escalader). Utiliser ce tool pour garder une trace des décisions de l'agent. Le journal est
        stocké dans la base de l'agent : le TMS n'est jamais modifié."""
        return store.add_log(now=await svc.now(), type=type, mission_id=mission_id, alert_id=alert_id, payload=payload)

    @mcp.tool(
        title="Consulter le journal de l'agent",
        annotations=_READ_LOCAL,
        meta=_meta("observation", "agent", domain="alerting", frequency="low"),
    )
    async def list_agent_actions(
        mission_id: Annotated[str | None, Field(description="Filtre sur une mission.")] = None,
        type: Annotated[str | None, Field(description="Filtre sur un type d'action.")] = None,
        alert_id: Annotated[str | None, Field(description="Filtre sur une alerte.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Nombre maximal d'entrées (les plus récentes).")] = 50,
    ) -> AgentLogList:
        """Retourne le journal d'audit de l'agent (alertes émises, notifications, appels, décisions,
        analyses), les plus récentes d'abord. Utiliser ce tool pour savoir ce que l'agent a déjà fait
        sur une mission avant d'agir à nouveau, ou pour produire un rapport d'activité."""
        entries = store.list_logs(mission_id=mission_id, type=type, alert_id=alert_id, limit=limit)
        return AgentLogList(count=len(entries), entries=entries)

    @mcp.tool(
        title="Vue d'ensemble de l'exploitation",
        annotations=_READ,
        meta=_meta("observation", "agent", domain="alerting", latency="medium"),
    )
    async def get_operations_overview() -> OverviewOut:
        """Retourne un tableau de bord synthétique : heure du TMS, alertes ouvertes par sévérité et, pour
        chaque mission en cours, ses indicateurs déjà interprétés (retard ou avance à la livraison, écart à
        l'itinéraire, statut et vitesse du véhicule, alertes ouvertes). Utiliser ce tool en premier pour
        répondre à « quelle est la situation ? », repérer les missions à risque ou démarrer un rapport."""
        missions, now = await asyncio.gather(_tms(connector.list_missions(status=["in_progress"])), svc.now())
        open_alerts = store.list_alerts(status="open", limit=500)
        indicators = await asyncio.gather(*(_indicator(m, open_alerts) for m in missions))
        stats = store.alert_stats()
        return OverviewOut(
            tms_time=iso(now),
            active_missions=len(missions),
            open_alerts=stats.get("open", 0),
            open_alerts_by_severity=stats["by_severity_open"],
            missions=sorted(indicators, key=lambda i: (-len(i.open_alerts), -(i.delay_minutes or 0))),
            recent_alerts=open_alerts[:10],
        )

    async def _indicator(mission: dict[str, Any], open_alerts: list[dict[str, Any]]) -> MissionIndicator:
        mid = mission["id"]
        calls = {"eta": connector.get_mission_eta(mid), "deviation": connector.get_mission_deviation(mid)}
        if mission.get("vehicle_id"):
            calls["vehicle"] = connector.get_vehicle(mission["vehicle_id"])
        results = dict(zip(calls, await asyncio.gather(*calls.values(), return_exceptions=True)))
        unavailable = [k for k, v in results.items() if isinstance(v, Exception)]
        eta = results.get("eta") if "eta" not in unavailable else {}
        deviation = results.get("deviation") if "deviation" not in unavailable else {}
        vehicle = results.get("vehicle") if "vehicle" not in unavailable else {}
        delay = (eta or {}).get("delay_minutes")
        if isinstance(delay, (int, float)):
            delivery = f"retard de {delay:.0f} min" if delay >= 1 else (f"avance de {-delay:.0f} min" if delay <= -1 else "à l'heure")
        else:
            delivery = None
        origin, destination = (mission.get("origin") or {}).get("label"), (mission.get("destination") or {}).get("label")
        return MissionIndicator(
            mission_id=mid,
            reference=mission.get("reference"),
            route=f"{origin} → {destination}" if origin and destination else None,
            vehicle_status=(vehicle or {}).get("status"),
            speed_kmh=((vehicle or {}).get("current_position") or {}).get("speed_kmh"),
            delay_minutes=delay,
            delivery_status=delivery,
            offset_km=(deviation or {}).get("current_offset_km"),
            offset_minutes=(deviation or {}).get("duration_minutes"),
            open_alerts=[a["rule_id"] for a in open_alerts if a.get("mission_id") == mid],
            unavailable=unavailable,
        )

    # =================================================================================
    # Ressources et prompts
    # =================================================================================

    @mcp.resource("agent://alerts/open", name="open_alerts", title="Alertes ouvertes", mime_type="application/json")
    def open_alerts_resource() -> str:
        """Alertes ouvertes de l'agent (JSON)."""
        return json.dumps(store.list_alerts(status="open", limit=200), ensure_ascii=False, default=str)

    @mcp.resource("tms://contract", name="tms_contract", title="Contrat pivot du TMS", mime_type="text/markdown")
    def contract_resource() -> str:
        """Résumé du contrat pivot exposé par les tools TMS."""
        return (
            "# Contrat pivot du TMS\n"
            "- Mission : id, reference, vehicle_id, driver_id, origin/destination {label, lat, lon}, status "
            "(planned | in_progress | completed | cancelled), planned_pickup_at, planned_delivery_at, "
            "actual_pickup_at, actual_delivery_at, current_eta, updated_at, events[].\n"
            "- MissionEvent : id, type (arrived_pickup, loading_started, loading_completed, departed_pickup, "
            "arrived_delivery, unloading_started, unloading_completed, driver_message, deviation_detected), "
            "occurred_at, position, payload, source (driver_app | tms).\n"
            "- Vehicle : id, plate, status (idle | moving | stopped | offline), current_position, last_seen_at.\n"
            "- Deviation : current_offset_km, current_offset_since, duration_minutes, severity, vehicle_position.\n"
            "- Eta : planned_delivery_at, current_eta, delay_minutes, remaining_distance_km.\n"
            "- User : id, name, role, management_level (M+0..M+4), phone, email.\n"
            "Horodatages ISO 8601 UTC ; heure de référence : get_tms_time.\n"
        )

    @mcp.prompt(title="Cycle de surveillance")
    def surveillance_cycle() -> str:
        """Instructions pour exécuter un cycle de surveillance complet."""
        return (
            "Exécute un cycle de surveillance : 1) get_active_missions ; 2) pour chaque mission, "
            "get_mission_snapshot ; 3) identifie les situations anormales (dérive, retard, immobilisation, "
            "absence de mise à jour, incident chauffeur) ; 4) vérifie list_alerts avant toute escalade ; "
            "5) résume la situation par mission avec le niveau de risque."
        )

    @mcp.prompt(title="Enquête sur une alerte")
    def investigate_alert(alert_id: str) -> str:
        """Instructions pour enquêter sur une alerte précise."""
        return (
            f"Enquête sur l'alerte {alert_id} : lis-la avec get_alert, puis le contexte de la mission "
            "(get_mission_snapshot, get_mission_events, get_vehicle_positions). Établis la cause probable, "
            "le niveau de risque et les actions recommandées, en citant les données observées."
        )

    return mcp
