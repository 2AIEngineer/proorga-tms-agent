"""Gabarits de messages : `{{ chemin.du.champ }}` (aucune logique, aucun code exécuté)."""

import re
from typing import Any

from rules_engine.paths import MISSING, resolve

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


def format_value(v: Any) -> str:
    if v is MISSING or v is None:
        return "?"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.1f}"
    return str(v)


def render(template: str, data: dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(lambda m: format_value(resolve(data, m.group(1))), template).strip()


def placeholders(template: str) -> list[str]:
    return _PLACEHOLDER.findall(template)
