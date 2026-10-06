import itertools
import uuid
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest
from psycopg import sql
from tms_agent.config import AGENT_DIR, AgentSettings
from tms_agent.graph.monitor import MonitoringAgent
from tms_agent.llm import LlmError, ModelTurn
from tms_agent.mcp_gateway import McpGateway
from tms_agent.rules_engine import RuleBook
from tms_mcp.config import Settings as ServerSettings
from tms_mcp.server import create_server
from tms_mcp.store import AgentStore
from tms_mcp.testing import FakeTmsConnector, make_mission


@pytest.fixture(scope="session")
def database() -> dict[str, Any]:
    """PostgreSQL de `agent/.env` (POSTGRES_*). Chaque test travaille dans des schémas jetables."""
    fields = AgentSettings().model_dump(include={"postgres_db", "postgres_user", "postgres_password",
                                                 "postgres_host", "postgres_port"})
    url = AgentSettings(**fields).database_url
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL indisponible : {exc}")
    return fields


@pytest.fixture
def schemas(database):
    """Fabrique de noms de schémas de test, supprimés en fin de test."""
    created: list[str] = []

    def make(prefix: str) -> str:
        created.append(f"test_{prefix}_{uuid.uuid4().hex[:8]}")
        return created[-1]

    yield make
    with psycopg.connect(AgentSettings(**database).database_url, autocommit=True) as conn:
        for name in created:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def settings(database, schemas) -> AgentSettings:
    return AgentSettings(
        _env_file=None,
        **database,
        state_db_schema=schemas("monitor"),
        assistant_db_schema=schemas("assistant"),
        rules_dir=AGENT_DIR / "domain" / "rules",
        llm_max_turns=6,
        investigate_min_severity="medium",
        background_investigations=False,
        llm_provider="local",
    )


@pytest.fixture
def tms() -> FakeTmsConnector:
    fake = FakeTmsConnector()
    fake.add_mission(make_mission("msn_1"))
    return fake


@pytest.fixture
def store(settings, schemas):
    s = AgentStore(settings.database_url, schemas("agent"))
    yield s
    s.close()


_ids = itertools.count()


def tool_use(name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": inp}


class ScriptedLLM:
    """Remplace le LLM : chaque tour est un callable(messages) -> liste de blocs, ou une exception."""

    provider = "scripted"
    model = "scripted"
    recoverable = False

    def __init__(self, *turns, context_window=None):
        self.context_window = context_window
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[str]] = []
        self.forced: list[str | None] = []
        self.disabled_reason = None

    @property
    def available(self) -> bool:
        return True

    async def probe(self) -> bool:
        return True

    async def complete(self, *, system, messages, tools, effort, force_tool=None) -> ModelTurn:
        self.forced.append(force_tool)
        self.requests.append([dict(m) for m in messages])
        self.tools_seen.append([t["name"] for t in tools])
        if not self.turns:
            raise LlmError("script épuisé")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        blocks = turn(messages) if callable(turn) else turn
        uses = [b for b in blocks if b["type"] == "tool_use"]
        text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        return ModelTurn(content=blocks, stop_reason="tool_use" if uses else "end_turn", text=text, tool_uses=uses,
                         usage={"input_tokens": 10, "output_tokens": 5})


class OfflineLLM:
    provider = "none"
    model = "none"
    available = False
    recoverable = False
    context_window = None
    disabled_reason = "test hors ligne"

    async def probe(self) -> bool:
        return False


@asynccontextmanager
async def running_agent(settings, tms, store, llm=None, events=None):
    server = create_server(ServerSettings(), connector=tms, store=store)
    gateway = McpGateway(server)
    sink = (lambda kind, data: events.append((kind, data))) if events is not None else None
    agent = MonitoringAgent(settings, gateway, llm=llm or OfflineLLM(), rulebook=RuleBook(settings.rules_dir),
                            on_event=sink)
    try:
        await agent.setup()
        yield agent
    finally:
        await agent.shutdown()
        await gateway.close()
