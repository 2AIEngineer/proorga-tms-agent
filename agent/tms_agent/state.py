"""État persistant de la boucle de surveillance (propre au processus agent).

- Événements de mission déjà évalués : un redémarrage ne ré-évalue pas les règles d'événement
  sur des événements traités, et un événement dont le traitement a échoué sera repris.
- Petites valeurs clé/valeur (heure TMS du dernier cycle, compteur de cycles).

PostgreSQL, schéma `monitor` par défaut. Les alertes, notifications et le journal ne sont pas
ici : ils sont dans la base de l'agent, derrière le serveur MCP.
"""

from collections.abc import Iterable

from tms_agent.db import open_pool

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    mission_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (mission_id, event_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class AgentStateStore:
    def __init__(self, conninfo: str, schema: str = "monitor"):
        self.schema = schema
        self._pool = open_pool(conninfo, schema, SCHEMA, max_size=2)

    @classmethod
    def from_settings(cls, settings) -> "AgentStateStore":
        return cls(settings.database_url, settings.state_db_schema)

    def processed_event_ids(self, mission_id: str) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT event_id FROM processed_events WHERE mission_id = %s",
                (mission_id,),
            )
            return {r["event_id"] for r in rows}

    def mark_processed(
        self, mission_id: str, event_ids: Iterable[str], at: str
    ) -> None:
        rows = [(mission_id, e, at) for e in event_ids]
        if not rows:
            return
        with self._pool.connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO processed_events (mission_id, event_id, processed_at) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                rows,
            )

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def reset(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("TRUNCATE processed_events, kv")

    def close(self) -> None:
        self._pool.close()
