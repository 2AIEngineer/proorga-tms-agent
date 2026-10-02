"""Connexion PostgreSQL : pool de connexions limité à un schéma, création idempotente du schéma."""

import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class DatabaseUnavailable(RuntimeError):
    """PostgreSQL injoignable ou identifiants refusés."""


def open_pool(conninfo: str, schema: str, ddl: str, *, max_size: int = 4, timeout_s: float = 10.0) -> ConnectionPool:
    """Crée le schéma et ses tables si besoin, puis ouvre un pool dont le `search_path` est ce schéma.

    La création est sérialisée par un verrou consultatif : plusieurs processus peuvent démarrer en même temps.
    """
    if not _IDENTIFIER.match(schema):
        raise ValueError(f"nom de schéma invalide : {schema!r}")
    try:
        with psycopg.connect(conninfo, connect_timeout=int(timeout_s)) as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"schema:{schema}",))
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
            conn.execute(ddl)
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailable(f"PostgreSQL injoignable : {exc}".strip()) from exc
    pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row, "options": f"-c search_path={schema}"},
        open=True,
    )
    pool.wait(timeout_s)
    return pool
