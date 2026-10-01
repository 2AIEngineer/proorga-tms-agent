"""Schémas de sortie des tools (outputSchema MCP).

Les objets du contrat pivot (Document 1) acceptent des champs supplémentaires (`extra="allow"`) :
un connecteur vers un autre TMS peut enrichir les objets sans casser le contrat.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- Contrat pivot du TMS -------------------------------------------------------------


class Point(_Open):
    lat: float
    lon: float


class Place(Point):
    label: str | None = None


class MissionEvent(_Open):
    id: str
    mission_id: str
    type: str
    occurred_at: str
    position: Point | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None


class Mission(_Open):
    id: str
    reference: str | None = None
    vehicle_id: str | None = None
    driver_id: str | None = None
    origin: Place | None = None
    destination: Place | None = None
    planned_route_id: str | None = None
    status: str
    planned_pickup_at: str | None = None
    planned_delivery_at: str | None = None
    actual_pickup_at: str | None = None
    actual_delivery_at: str | None = None
    current_eta: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MissionDetail(Mission):
    events: list[MissionEvent] = Field(default_factory=list)


class Position(_Open):
    lat: float
    lon: float
    heading: float | None = None
    speed_kmh: float | None = None
    recorded_at: str | None = None


class Vehicle(_Open):
    id: str
    plate: str | None = None
    fleet_id: str | None = None
    current_position: Position | None = None
    status: str | None = None
    last_seen_at: str | None = None
    current_mission_id: str | None = None


class Deviation(_Open):
    mission_id: str
    current_offset_km: float | None = None
    current_offset_since: str | None = None
    duration_minutes: float | None = None
    severity: str | None = None
    vehicle_position: Point | None = None
    computed_at: str | None = None


class Eta(_Open):
    mission_id: str
    planned_delivery_at: str | None = None
    current_eta: str | None = None
    delay_minutes: float | None = None
    remaining_distance_km: float | None = None
    computed_at: str | None = None


class User(_Open):
    id: str
    name: str | None = None
    role: str | None = None
    management_level: str | None = None
    phone: str | None = None
    email: str | None = None


class Driver(User):
    status: str | None = None
    current_mission_id: str | None = None


# --- Enveloppes des tools TMS ---------------------------------------------------------


class TmsTime(BaseModel):
    time: str = Field(description="Heure courante du TMS (ISO 8601 UTC), référence pour `now` et `updated_since`.")
    simulation: dict[str, Any] | None = None


class MissionList(BaseModel):
    tms_time: str | None = Field(None, description="Heure du TMS au moment de la lecture (à réutiliser comme `updated_since`).")
    count: int
    missions: list[Mission]


class EventList(BaseModel):
    mission_id: str
    count: int
    events: list[MissionEvent]


class RouteOut(BaseModel):
    id: str | None = None
    mission_id: str
    distance_km: float | None = None
    duration_min: float | None = None
    waypoint_count: int
    start: Point | None = None
    end: Point | None = None
    polyline: str | None = None
    waypoints: list[dict[str, Any]] | None = None


class VehiclePosition(Position):
    vehicle_id: str


class PositionHistory(BaseModel):
    vehicle_id: str
    count: int
    positions: list[dict[str, Any]]


class UserList(BaseModel):
    count: int
    users: list[User]


class MissionSnapshot(BaseModel):
    """Tout ce que le moteur de règles consomme pour une mission, lu en une fois."""

    tms_time: str
    mission: MissionDetail
    vehicle: Vehicle | None = None
    deviation: Deviation | None = None
    eta: Eta | None = None
    driver: Driver | None = None
    errors: dict[str, str] = Field(
        default_factory=dict, description="Ressources illisibles (la mission reste exploitable partiellement)."
    )


# --- Base de l'agent ------------------------------------------------------------------


class Alert(_Open):
    id: str
    rule_id: str
    rule_version: int
    mission_id: str | None = None
    event_id: str | None = None
    severity: str
    category: str | None = None
    title: str
    message: str | None = None
    observed_data: dict[str, Any] | None = None
    deduplication_key: str | None = None
    status: str
    created_at: str
    resolved_at: str | None = None
    resolution_reason: str | None = None
    analysis: dict[str, Any] | None = None


class Notification(_Open):
    id: str
    alert_id: str
    recipient_user_id: str | None = None
    recipient_name: str | None = None
    recipient_role: str | None = None
    management_level: str | None = None
    channel: str
    destination: str | None = None
    message: str | None = None
    status: str
    error: str | None = None
    sent_at: str
    delivered_at: str | None = None


class AlertCreated(BaseModel):
    created: bool = Field(description="False si la déduplication ou le cooldown ont empêché l'émission.")
    reason: str | None = Field(None, description="Motif de non-émission.")
    alert: Alert = Field(description="Alerte créée, ou alerte existante qui a bloqué l'émission.")


class AlertList(BaseModel):
    count: int
    alerts: list[Alert]


class AlertDetail(BaseModel):
    alert: Alert
    notifications: list[Notification]


class NotificationList(BaseModel):
    alert_id: str
    count: int
    delivered: int
    failed: int
    notifications: list[Notification]


class AgentLogEntry(_Open):
    id: str
    mission_id: str | None = None
    alert_id: str | None = None
    type: str
    payload: dict[str, Any] | None = None
    created_at: str


class AgentLogList(BaseModel):
    count: int
    entries: list[AgentLogEntry]


class DriverCall(_Open):
    id: str
    mission_id: str
    alert_id: str | None = None
    driver_id: str | None = None
    driver_name: str | None = None
    phone: str | None = None
    reason: str
    status: str
    started_at: str
    ended_at: str | None = None


class DriverCallResult(BaseModel):
    placed: bool = Field(description="False si un appel récent existe déjà (renvoyé dans `call`).")
    note: str
    call: DriverCall


class MissionIndicator(BaseModel):
    """Indicateurs d'une mission en cours, déjà interprétés (pas de calcul à refaire)."""

    mission_id: str
    reference: str | None = None
    route: str | None = None
    vehicle_status: str | None = None
    speed_kmh: float | None = None
    delay_minutes: float | None = Field(None, description="Positif = retard, négatif = avance.")
    delivery_status: str | None = Field(None, description="« retard de X min », « avance de X min » ou « à l'heure ».")
    offset_km: float | None = Field(None, description="Écart à l'itinéraire (null avant le départ).")
    offset_minutes: float | None = None
    open_alerts: list[str] = Field(default_factory=list, description="Règles des alertes ouvertes sur la mission.")
    unavailable: list[str] = Field(default_factory=list, description="Indicateurs illisibles dans le TMS.")


class OverviewOut(BaseModel):
    tms_time: str
    active_missions: int
    open_alerts: int
    open_alerts_by_severity: dict[str, int]
    missions: list[MissionIndicator] = Field(default_factory=list)
    recent_alerts: list[Alert]
