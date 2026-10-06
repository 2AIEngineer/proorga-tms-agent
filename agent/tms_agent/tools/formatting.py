"""Mise en forme pour le LLM : résultats épurés, schémas et descriptions allégés (profil compact)."""

import re
from typing import Any


def prune(value: Any) -> Any:
    """Retire récursivement les valeurs nulles et vides (allège le contexte des petits modèles)."""
    if isinstance(value, dict):
        out = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def strip_titles(schema: Any) -> Any:
    """Retire les `title` générés de JSON Schema, sans toucher aux propriétés nommées `title`."""
    if isinstance(schema, list):
        return [strip_titles(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = strip_titles(value)
    return out


def first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    match = re.search(r"^(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"
