"""Briques communes : tool local de l'agent et erreur d'arguments renvoyée au modèle."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    strict: bool = False


class ToolInputError(Exception):
    """Arguments d'un tool local invalides : message renvoyé tel quel au modèle pour correction."""
