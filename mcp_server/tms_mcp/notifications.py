"""Envoi des notifications d'une alerte.

1. Résolution des destinataires en **lisant** le TMS : `role: driver` → chauffeur de la mission
   (`GET /drivers/{id}`), autre rôle → `GET /roles/{role}/users`, `user_id` → `GET /users/{id}`.
2. Envoi par canal (`in_app`, `sms`, `email`, `call`) via un `ChannelSender`.
   Dans le POC, les envois sont simulés ; brancher un vrai fournisseur (SMS, SMTP, téléphonie)
   revient à fournir un autre `ChannelSender`.
3. Enregistrement de chaque notification dans la base de l'agent.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol

from tms_mcp.connector import TmsConnector, TmsError
from tms_mcp.store import AgentStore, iso

log = logging.getLogger(__name__)

CHANNELS = ("in_app", "sms", "email", "call")


@dataclass
class Delivery:
    status: str  # delivered | failed
    delivered_at: datetime | None = None
    error: str | None = None


class ChannelSender(Protocol):
    async def send(self, *, channel: str, destination: str, message: str, sent_at: datetime) -> Delivery: ...


class SimulatedSender:
    """Envoi simulé : délai de livraison réaliste par canal, aucune communication réelle."""

    LATENCY_S: ClassVar[dict[str, int]] = {"in_app": 1, "sms": 2, "email": 5, "call": 4}

    async def send(self, *, channel: str, destination: str, message: str, sent_at: datetime) -> Delivery:
        log.info("[SIMULATION] %s → %s : %s", channel, destination, message[:120])
        return Delivery("delivered", sent_at + timedelta(seconds=self.LATENCY_S.get(channel, 1)))


def _destination(user: dict[str, Any], channel: str) -> str | None:
    if channel in ("sms", "call"):
        return user.get("phone")
    if channel == "email":
        return user.get("email")
    return user.get("id")  # in_app : identifiant de l'utilisateur


class NotificationService:
    def __init__(self, connector: TmsConnector, store: AgentStore, sender: ChannelSender | None = None):
        self.connector = connector
        self.store = store
        self.sender = sender or SimulatedSender()

    async def resolve_recipient(self, recipient: dict[str, Any], mission: dict[str, Any] | None) -> list[dict[str, Any]]:
        if recipient.get("user_id"):
            return [await self.connector.get_user(recipient["user_id"])]
        role = recipient.get("role")
        if role == "driver":
            if not mission or not mission.get("driver_id"):
                raise TmsError("rôle `driver` impossible à résoudre : alerte sans mission ou mission sans chauffeur")
            return [await self.connector.get_driver(mission["driver_id"])]
        return await self.connector.list_role_users(role)

    async def notify(
        self,
        *,
        alert: dict[str, Any],
        recipients: list[dict[str, Any]],
        message: str | None,
        now: datetime,
    ) -> list[dict[str, Any]]:
        mission = None
        if alert.get("mission_id"):
            try:
                mission = await self.connector.get_mission(alert["mission_id"])
            except TmsError as exc:
                log.warning("Mission %s illisible pour la résolution des destinataires : %s", alert["mission_id"], exc)

        created: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for recipient in recipients:
            channel = recipient.get("channel")
            role = recipient.get("role")
            level = recipient.get("management_level")
            text = recipient.get("message") or message or alert.get("message") or alert["title"]
            base = {
                "alert_id": alert["id"],
                "recipient_role": role,
                "management_level": level,
                "channel": channel,
                "message": text,
                "sent_at": iso(now),
            }
            if channel not in CHANNELS:
                created.append(self.store.add_notification(**base, status="failed", error=f"canal inconnu {channel!r}"))
                continue
            try:
                users = await self.resolve_recipient(recipient, mission)
            except TmsError as exc:
                created.append(
                    self.store.add_notification(**base, recipient_user_id=recipient.get("user_id"), status="failed",
                                                error=f"résolution du destinataire impossible : {exc}")
                )
                continue
            if not users:
                created.append(
                    self.store.add_notification(**base, status="failed", error=f"aucun utilisateur pour le rôle {role!r}")
                )
                continue
            for user in users:
                key = (user["id"], channel)
                if key in seen:
                    continue
                seen.add(key)
                fields = {
                    **base,
                    "recipient_user_id": user["id"],
                    "recipient_name": user.get("name"),
                    "recipient_role": role or user.get("role"),
                    "management_level": level or user.get("management_level"),
                }
                destination = _destination(user, channel)
                if not destination:
                    created.append(
                        self.store.add_notification(**fields, status="failed",
                                                    error=f"aucune coordonnée pour le canal {channel}")
                    )
                    continue
                try:
                    delivery = await self.sender.send(channel=channel, destination=destination, message=text, sent_at=now)
                except Exception as exc:  # noqa: BLE001 — un canal défaillant ne bloque pas les autres
                    delivery = Delivery("failed", error=f"{type(exc).__name__}: {exc}")
                created.append(
                    self.store.add_notification(
                        **fields,
                        destination=destination,
                        status=delivery.status,
                        error=delivery.error,
                        delivered_at=iso(delivery.delivered_at) if delivery.delivered_at else None,
                    )
                )
        return created
