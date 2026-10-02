"""Chargement et validation des fichiers de règles YAML."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from tms_agent.rules_engine.models import Rule


class RuleLoadError(Exception):
    """Erreur lisible par l'équipe métier : fichier, règle, emplacement et cause."""


def _format_errors(source: str, rule_id: str | None, exc: ValidationError) -> str:
    lines = [f"{source}{f' (règle {rule_id})' if rule_id else ''} : règle invalide"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if not str(p).startswith(("function-", "tagged-union")))
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc or '(racine)'} : {msg}")
    return "\n".join(lines)


def parse_rules(text: str, source: str = "<texte>") -> list[Rule]:
    """Un fichier peut contenir une règle, une liste de règles, ou plusieurs documents YAML."""
    try:
        documents = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        raise RuleLoadError(f"{source} : YAML invalide\n  {exc}") from exc
    raw_rules = [r for d in documents for r in (d if isinstance(d, list) else [d])]
    rules = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise RuleLoadError(f"{source} : une règle doit être un objet YAML")
        try:
            rules.append(Rule.model_validate(raw))
        except ValidationError as exc:
            raise RuleLoadError(_format_errors(source, raw.get("id"), exc)) from exc
    return rules


def load_rules(path: str | Path) -> list[Rule]:
    """Charge un fichier ou tous les fichiers *.yaml / *.yml d'un dossier (ordre alphabétique)."""
    path = Path(path)
    files = sorted([*path.glob("*.yaml"), *path.glob("*.yml")]) if path.is_dir() else [path]
    if not files:
        raise RuleLoadError(f"aucun fichier de règles dans {path}")
    rules: list[Rule] = []
    seen: dict[str, Path] = {}
    for file in files:
        for rule in parse_rules(file.read_text(encoding="utf-8"), str(file)):
            if rule.id in seen:
                raise RuleLoadError(f"{file} : la règle {rule.id!r} est déjà définie dans {seen[rule.id]}")
            seen[rule.id] = file
            rules.append(rule)
    return rules
