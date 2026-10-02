"""Passerelle MCP : le seul chemin de l'agent vers le TMS et vers sa base.

- Connexion stdio (lance le serveur MCP comme sous-processus), HTTP, ou en mémoire (tests).
- Découverte dynamique des tools (`list_tools`) : l'agent n'a pas de liste codée en dur.
- Appels avec timeout ; reconnexion automatique si le transport tombe.
- Les résultats structurés (`structuredContent`) sont renvoyés tels quels ; une erreur de tool
  lève `McpToolError` avec le message du serveur.
"""

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Self

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import Tool

from tms_agent.config import AgentSettings

log = logging.getLogger(__name__)


class McpToolError(Exception):
    """Le tool a renvoyé une erreur (isError) : donnée absente, paramètre invalide, refus métier..."""

    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool} : {message}")
        self.tool = tool
        self.message = message


class McpUnavailable(Exception):
    """Serveur MCP injoignable (transport fermé, timeout répété)."""


def _result_text(result: Any) -> str:
    return "\n".join(
        getattr(c, "text", "")
        for c in (getattr(result, "content", None) or [])
        if getattr(c, "text", None)
    )


class McpGateway:
    def __init__(self, target: Any, *, call_timeout_s: float = 45.0):
        """`target` : URL HTTP, `StdioServerParameters`, ou instance de serveur (en mémoire)."""
        self._target = target
        self._timeout = call_timeout_s
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[Tool] | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: AgentSettings) -> "McpGateway":
        if settings.mcp_transport == "http":
            target: Any = settings.mcp_url
        else:
            target = StdioServerParameters(
                command="uv",
                args=[
                    "run",
                    "--quiet",
                    "--directory",
                    str(settings.mcp_server_dir),
                    "tms-mcp",
                    "--log-level",
                    "WARNING",
                    "serve",
                ],
                env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
            )
        return cls(target, call_timeout_s=settings.mcp_call_timeout_s)

    # --- Cycle de vie -----------------------------------------------------------------

    async def connect(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            stack = AsyncExitStack()
            try:
                self._client = await stack.enter_async_context(Client(self._target))
            except Exception as exc:
                await stack.aclose()
                raise McpUnavailable(
                    f"connexion au serveur MCP impossible : {exc}"
                ) from exc
            self._stack = stack
            self._tools = None
            info = self._client.server_info
            log.info(
                "Connecté au serveur MCP %s %s",
                getattr(info, "name", "?"),
                getattr(info, "version", ""),
            )

    async def close(self) -> None:
        async with self._lock:
            stack, self._stack, self._client = self._stack, None, None
            if stack is not None:
                try:
                    await stack.aclose()
                except Exception as exc:  # noqa: BLE001 — fermeture best effort
                    log.debug("Fermeture MCP : %s", exc)

    async def reconnect(self) -> None:
        await self.close()
        await self.connect()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def instructions(self) -> str | None:
        return self._client.instructions if self._client else None

    # --- Tools ------------------------------------------------------------------------

    async def list_tools(self, refresh: bool = False) -> list[Tool]:
        if self._tools is None or refresh:
            await self.connect()
            self._tools = list((await self._client.list_tools()).tools)
        return self._tools

    async def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Appel brut ; une seule reconnexion en cas de transport cassé."""
        for attempt in (1, 2):
            await self.connect()
            try:
                return await asyncio.wait_for(
                    self._client.call_tool(name, arguments or {}), timeout=self._timeout
                )
            except TimeoutError as exc:
                if attempt == 2:
                    raise McpUnavailable(
                        f"{name} : pas de réponse en {self._timeout:.0f}s"
                    ) from exc
                log.warning("Timeout MCP sur %s, reconnexion", name)
            except (McpToolError, McpUnavailable):
                raise
            except Exception as exc:  # transport fermé, sous-processus mort...
                if attempt == 2:
                    raise McpUnavailable(
                        f"{name} : {type(exc).__name__}: {exc}"
                    ) from exc
                log.warning("Transport MCP en erreur (%s), reconnexion", exc)
            await self.reconnect()
        raise McpUnavailable(name)  # inatteignable

    async def call(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Appel d'un tool ; renvoie le contenu structuré (dict) ou lève `McpToolError`."""
        result = await self.call_raw(name, arguments)
        if getattr(result, "is_error", False):
            raise McpToolError(name, _result_text(result) or "erreur inconnue")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        text = _result_text(result)
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}
