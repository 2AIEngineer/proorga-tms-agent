"""Base de l'agent (Document 3) : alertes, notifications, journal d'audit, appels chauffeur.

Ces données sont produites par l'agent et ne sont jamais écrites dans le TMS.
SQLite (WAL) ; toutes les écritures sont sérialisées par un verrou, ce qui rend
la création d'alerte (déduplication + cooldown + insertion) atomique.
"""

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
    observed_data     TEXT NOT NULL DEFAULT '{}',
    deduplication_key TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    resolution_reason TEXT,
    analysis          TEXT
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
    sent_at           TEXT NOT NULL,
    delivered_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_alert ON notifications (alert_id);

CREATE TABLE IF NOT EXISTS agent_logs (
    id         TEXT PRIMARY KEY,
    mission_id TEXT,
    alert_id   TEXT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
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
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_mission ON driver_calls (mission_id, started_at);
"""

_JSON_COLUMNS = {"observed_data", "analysis", "payload"}


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
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- Helpers ----------------------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for col in _JSON_COLUMNS & d.keys():
            d[col] = json.loads(d[col]) if d[col] else None
        return d

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self._conn.execute(sql, params).fetchone())

    @staticmethod
    def _where(filters: dict[str, Any]) -> tuple[str, tuple]:
        clauses, params = [], []
        for col, value in filters.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                clauses.append(f"{col} IN ({','.join('?' * len(value))})")
                params.extend(value)
            else:
                clauses.append(f"{col} = ?")
                params.append(value)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

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
        with self._lock:
            if deduplication_key:
                existing = self._row(
                    self._conn.execute(
                        "SELECT * FROM alerts WHERE deduplication_key = ? AND status = 'open' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (deduplication_key,),
                    ).fetchone()
                )
                if existing:
                    return AlertCreation(existing, False, "alerte déjà ouverte avec la même clé de déduplication")
                last = self._row(
                    self._conn.execute(
                        "SELECT * FROM alerts WHERE deduplication_key = ? ORDER BY created_at DESC LIMIT 1",
                        (deduplication_key,),
                    ).fetchone()
                )
                if last and cooldown_minutes:
                    elapsed = now - _from_iso(last["created_at"])
                    # elapsed < 0 : l'horloge du TMS a été remise à zéro (nouvelle simulation) → pas de cooldown.
                    if timedelta(0) <= elapsed < timedelta(minutes=cooldown_minutes):
                        remaining = cooldown_minutes - elapsed.total_seconds() / 60
                        return AlertCreation(last, False, f"cooldown actif ({remaining:.0f} min restantes)")
            alert_id = new_id("alr")
            self._conn.execute(
                "INSERT INTO alerts (id, rule_id, rule_version, mission_id, event_id, severity, category, title, "
                "message, observed_data, deduplication_key, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    alert_id,
                    rule_id,
                    rule_version,
                    mission_id,
                    event_id,
                    severity,
                    category,
                    title,
                    message,
                    json.dumps(observed_data or {}, ensure_ascii=False, default=str),
                    deduplication_key,
                    iso(now),
                ),
            )
            return AlertCreation(self.get_alert(alert_id), True)

    def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM alerts WHERE id = ?", (alert_id,))

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
        return self._all(f"SELECT * FROM alerts{where} ORDER BY created_at DESC LIMIT ?", (*params, limit))

    def resolve_alert(self, alert_id: str, *, now: datetime, reason: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET status = 'resolved', resolved_at = ?, resolution_reason = ? "
                "WHERE id = ? AND status = 'open'",
                (iso(now), reason, alert_id),
            )
            return self.get_alert(alert_id)

    def annotate_alert(self, alert_id: str, analysis: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET analysis = ? WHERE id = ?",
                (json.dumps(analysis, ensure_ascii=False, default=str), alert_id),
            )
            return self.get_alert(alert_id)

    def alert_stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, severity, COUNT(*) AS n FROM alerts GROUP BY status, severity"
            ).fetchall()
        stats: dict[str, Any] = {"open": 0, "resolved": 0, "by_severity_open": {}}
        for r in rows:
            stats[r["status"]] = stats.get(r["status"], 0) + r["n"]
            if r["status"] == "open":
                stats["by_severity_open"][r["severity"]] = r["n"]
        return stats

    # --- Notifications ----------------------------------------------------------------

    def add_notification(self, **fields: Any) -> dict[str, Any]:
        notif_id = new_id("ntf")
        cols = ["id", *fields.keys()]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO notifications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (notif_id, *fields.values()),
            )
            return self._one("SELECT * FROM notifications WHERE id = ?", (notif_id,))

    def list_notifications(self, alert_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM notifications WHERE alert_id = ? ORDER BY sent_at, id", (alert_id,))

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
        log_id = new_id("log")
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_logs (id, mission_id, alert_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (log_id, mission_id, alert_id, type, json.dumps(payload or {}, ensure_ascii=False, default=str), iso(now)),
            )
            return self._one("SELECT * FROM agent_logs WHERE id = ?", (log_id,))

    def list_logs(
        self, *, mission_id: str | None = None, type: str | None = None, alert_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where, params = self._where({"mission_id": mission_id, "type": type, "alert_id": alert_id})
        return self._all(f"SELECT * FROM agent_logs{where} ORDER BY created_at DESC, id LIMIT ?", (*params, limit))

    # --- Appels chauffeur -------------------------------------------------------------

    def add_driver_call(self, **fields: Any) -> dict[str, Any]:
        call_id = new_id("call")
        cols = ["id", *fields.keys()]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO driver_calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (call_id, *fields.values()),
            )
            return self._one("SELECT * FROM driver_calls WHERE id = ?", (call_id,))

    def last_driver_call(self, mission_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM driver_calls WHERE mission_id = ? ORDER BY started_at DESC LIMIT 1", (mission_id,)
        )

    def list_driver_calls(self, mission_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = self._where({"mission_id": mission_id})
        return self._all(f"SELECT * FROM driver_calls{where} ORDER BY started_at DESC LIMIT ?", (*params, limit))

    def reset(self) -> None:
        with self._lock:
            for table in ("notifications", "driver_calls", "agent_logs", "alerts"):
                self._conn.execute(f"DELETE FROM {table}")
