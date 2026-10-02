import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest
from mcp import Client
from psycopg import sql
from tms_mcp.config import Settings
from tms_mcp.server import create_server
from tms_mcp.store import AgentStore
from tms_mcp.testing import FakeTmsConnector, make_mission


@pytest.fixture
def tms():
    fake = FakeTmsConnector()
    fake.add_mission(make_mission("msn_1"), offset_km=6.4, offset_minutes=27)
    fake.add_mission(make_mission("msn_2", driver_id="drv_78", vehicle_id="veh_2"), vehicle_status="stopped")
    return fake


@pytest.fixture(scope="session")
def database_url() -> str:
    """PostgreSQL de `.env` (POSTGRES_*). Chaque test travaille dans un schéma jetable."""
    url = Settings().database_url
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL indisponible : {exc}")
    return url


@pytest.fixture
def store(database_url):
    schema = f"test_agent_{uuid.uuid4().hex[:8]}"
    s = AgentStore(database_url, schema)
    yield s
    s.close()
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@asynccontextmanager
async def open_client(tms, store):
    """Client MCP en mémoire. Ouvert dans le test lui-même : une fixture async ferait entrer et sortir
    les scopes anyio du client dans deux tâches différentes."""
    server = create_server(Settings(), connector=tms, store=store)
    async with Client(server) as c:
        yield c


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def call_error(client, name, args=None) -> str:
    result = await client.call_tool(name, args or {})
    assert result.is_error, result.structured_content
    return result.content[0].text
