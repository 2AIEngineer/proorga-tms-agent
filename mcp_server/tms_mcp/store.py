"""Base de l'agent (Document 3) : alertes, notifications, journal d'audit, appels chauffeur.

Ces données sont produites par l'agent et ne sont jamais écrites dans le TMS.
PostgreSQL, dans un schéma dédié (`agent` par défaut). La création d'alerte (déduplication +
cooldown + insertion) s'exécute dans une transaction sous verrou consultatif par clé : elle reste
atomique même avec plusieurs instances du serveur MCP.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from tms_mcp.db import open_pool

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id                TEXT PRIMARY KEY,
    rule_id           TEXT NOT NULL,
    rule_version      INTEGER NOT NULL,
    mission_id        TEXT,
    event_id          TEXT,
    severity          TEXT NOT NULL,
    category          TEXT,
    title             TEXT NOT NULL,
    message           TEXT,
    observed_data     JSONB NOT NULL DEFAULT '{}',
    deduplication_key TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        TIMESTAMPTZ NOT NULL,
    resolved_at       TIMESTAMPTZ,
    resolution_reason TEXT,
    analysis          JSONB
);
CREATE INDEX IF NOT EXISTS ix_alerts_key ON alerts (deduplication_key, created_at);
CREATE INDEX IF NOT EXISTS ix_alerts_mission ON alerts (mission_id, status);

CREATE TABLE IF NOT EXISTS notifications (
    id                TEXT PRIMARY KEY,
    alert_id          TEXT NOT NULL REFERENCES alerts (id),
    recipient_user_id TEXT,
    recipient_name    TEXT,
    recipient_role    TEXT,
    management_level  TEXT,
    channel           TEXT NOT NULL,
    destination       TEXT,
    message           TEXT,
    status            TEXT NOT NULL,
    error             TEXT,
    sent_at           TIMESTAMPTZ NOT NULL,
    delivered_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_notifications_alert ON notifications (alert_id);

CREATE TABLE IF NOT EXISTS agent_logs (
    id         TEXT PRIMARY KEY,
    mission_id TEXT,
    alert_id   TEXT,
    type       TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_logs_mission ON agent_logs (mission_id, created_at);

CREATE TABLE IF NOT EXISTS driver_calls (
    id         TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    alert_id   TEXT,
    driver_id  TEXT,
    driver_name TEXT,
    phone      TEXT,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_calls_mission ON driver_calls (mission_id, started_at);
"""

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _from_iso(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v.replace("Z", "+00:00"))


@dataclass
class AlertCreation:
    alert: dict[str, Any]
    created: bool
    reason: str | None = None


class AgentStore:
    def __init__(self, conninfo: str, schema: str = "agent"):
        self.schema = schema
        self._pool = open_pool(conninfo, schema, SCHEMA)

    @classmethod
    def from_settings(cls, settings) -> "AgentStore":
        return cls(settings.database_url, settings.agent_db_schema)

    def close(self) -> None:
        self._pool.close()

    # --- Helpers ----------------------------------------------------------------------

    @staticmethod
    def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        """Horodatages renvoyés en ISO 8601 UTC (`...Z`), comme le contrat des tools."""
        if row is None:
            return None
        return {k: iso(v) if isinstance(v, datetime) else v for k, v in row.items()}

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._pool.connection() as conn:
            conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return [self._row(r) for r in conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            return self._row(conn.execute(sql, params).fetchone())

    @staticmethod
    def _where(filters: dict[str, Any]) -> tuple[str, tuple]:
        clauses, params = [], []
        for col, value in filters.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                clauses.append(f"{col} = ANY(%s)")
                params.append(list(value))
            else:
                clauses.append(f"{col} = %s")
                params.append(value)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

    def _insert(self, table: str, fields: dict[str, Any]) -> dict[str, Any]:
        cols = ", ".join(fields)
        return self._one(
            f"INSERT INTO {table} ({cols}) VALUES ({', '.join(['%s'] * len(fields))}) RETURNING *",
            tuple(fields.values()),
        )

    # --- Alertes ----------------------------------------------------------------------

    def create_alert(
        self,
        *,
        now: datetime,
        rule_id: str,
        rule_version: int,
        severity: str,
        title: str,
        mission_id: str | None = None,
        event_id: str | None = None,
        category: str | None = None,
        message: str | None = None,
        observed_data: dict[str, Any] | None = None,
        deduplication_key: str | None = None,
        cooldown_minutes: float = 0,
    ) -> AlertCreation:
        """Crée l'alerte sauf si la déduplication ou le cooldown l'interdisent (opération atomique).

        - une alerte **ouverte** porte déjà la clé → non émise ;
        - la dernière alerte de même clé (même résolue) date de moins de `cooldown_minutes` → non émise.
        """
        with self._pool.connection() as conn, conn.transaction():
            if deduplication_key:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (deduplication_key,))
                existing = self._row(
                    conn.execute(
                        "SELECT * FROM alerts WHERE deduplication_key = %s AND status = 'open' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (deduplication_key,),
                    ).fetchone()
                )
                if existing:
                    return AlertCreation(existing, False, "alerte déjà ouverte avec la même clé de déduplication")
                last = self._row(
                    conn.execute(
                        "SELECT * FROM alerts WHERE deduplication_key = %s ORDER BY created_at DESC LIMIT 1",
                        (deduplication_key,),
                    ).fetchone()
                )
                if last and cooldown_minutes:
                    elapsed = now - _from_iso(last["created_at"])
                    # elapsed < 0 : l'horloge du TMS a été remise à zéro (nouvelle simulation) → pas de cooldown.
                    if timedelta(0) <= elapsed < timedelta(minutes=cooldown_minutes):
                        remaining = cooldown_minutes - elapsed.total_seconds() / 60
                        return AlertCreation(last, False, f"cooldown actif ({remaining:.0f} min restantes)")
            row = conn.execute(
                "INSERT INTO alerts (id, rule_id, rule_version, mission_id, event_id, severity, category, title, "
                "message, observed_data, deduplication_key, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s) RETURNING *",
                (
                    new_id("alr"),
                    rule_id,
                    rule_version,
                    mission_id,
                    event_id,
                    severity,
                    category,
                    title,
                    message,
                    Jsonb(observed_data or {}),
                    deduplication_key,
                    now,
                ),
            ).fetchone()
            return AlertCreation(self._row(row), True)

    def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM alerts WHERE id = %s", (alert_id,))

    def list_alerts(
        self,
        *,
        mission_id: str | None = None,
        severity: str | list[str] | None = None,
        status: str | None = None,
        deduplication_key: str | None = None,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            {
                "mission_id": mission_id,
                "severity": severity,
                "status": status,
                "deduplication_key": deduplication_key,
                "rule_id": rule_id,
            }
        )
        return self._all(f"SELECT * FROM alerts{where} ORDER BY created_at DESC LIMIT %s", (*params, limit))

    def resolve_alert(self, alert_id: str, *, now: datetime, reason: str) -> dict[str, Any] | None:
        self._execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = %s, resolution_reason = %s "
            "WHERE id = %s AND status = 'open'",
            (now, reason, alert_id),
        )
        return self.get_alert(alert_id)

    def annotate_alert(self, alert_id: str, analysis: dict[str, Any]) -> dict[str, Any] | None:
        return self._one("UPDATE alerts SET analysis = %s WHERE id = %s RETURNING *", (Jsonb(analysis), alert_id))

    def alert_stats(self) -> dict[str, Any]:
        rows = self._all("SELECT status, severity, COUNT(*) AS n FROM alerts GROUP BY status, severity")
        stats: dict[str, Any] = {"open": 0, "resolved": 0, "by_severity_open": {}}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + r["n"]
            if r["status"] == "open":
                stats["by_severity_open"][r["severity"]] = r["n"]
        return stats

    # --- Notifications ----------------------------------------------------------------

    def add_notification(self, **fields: Any) -> dict[str, Any]:
        return self._insert("notifications", {"id": new_id("ntf"), **fields})

    def list_notifications(self, alert_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notifications WHERE alert_id = %s ORDER BY sent_at, id", (alert_id,))

    # --- Journal ----------------------------------------------------------------------

    def add_log(
        self,
        *,
        now: datetime,
        type: str,
        mission_id: str | None = None,
        alert_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._insert(
            "agent_logs",
            {"id": new_id("log"), "mission_id": mission_id, "alert_id": alert_id, "type": type,
             "payload": Jsonb(payload or {}), "created_at": now},
        )

    def list_logs(
        self, *, mission_id: str | None = None, type: str | None = None, alert_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where, params = self._where({"mission_id": mission_id, "type": type, "alert_id": alert_id})
        return self._all(f"SELECT * FROM agent_logs{where} ORDER BY created_at DESC, id LIMIT %s", (*params, limit))

    # --- Appels chauffeur -------------------------------------------------------------

    def add_driver_call(self, **fields: Any) -> dict[str, Any]:
        return self._insert("driver_calls", {"id": new_id("call"), **fields})

    def last_driver_call(self, mission_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM driver_calls WHERE mission_id = %s ORDER BY started_at DESC LIMIT 1", (mission_id,)
        )

    def list_driver_calls(self, mission_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = self._where({"mission_id": mission_id})
        return self._all(f"SELECT * FROM driver_calls{where} ORDER BY started_at DESC LIMIT %s", (*params, limit))

    def reset(self) -> None:
        self._execute("TRUNCATE notifications, driver_calls, agent_logs, alerts")
