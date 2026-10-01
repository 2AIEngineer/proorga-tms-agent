"""État persistant de la boucle de surveillance (propre au processus agent).

- Événements de mission déjà évalués : un redémarrage ne ré-évalue pas les règles d'événement
  sur des événements traités, et un événement dont le traitement a échoué sera repris.
- Petites valeurs clé/valeur (heure TMS du dernier cycle, compteur de cycles).

Les alertes, notifications et le journal ne sont pas ici : ils sont dans la base de l'agent,
derrière le serveur MCP.
"""

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    mission_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (mission_id, event_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class AgentStateStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def processed_event_ids(self, mission_id: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id FROM processed_events WHERE mission_id = ?",
                (mission_id,),
            )
            return {r[0] for r in rows}

    def mark_processed(
        self, mission_id: str, event_ids: Iterable[str], at: str
    ) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO processed_events (mission_id, event_id, processed_at) VALUES (?, ?, ?)",
                [(mission_id, e, at) for e in event_ids],
            )

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM processed_events")
            self._conn.execute("DELETE FROM kv")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
