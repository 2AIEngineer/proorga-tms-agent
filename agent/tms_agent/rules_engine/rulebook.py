"""Jeu de règles actif, rechargé à chaud depuis un dossier YAML (`domain/rules/`).

Si un fichier change, les règles sont rechargées au cycle suivant ; une règle invalide est
signalée et l'ancien jeu de règles reste actif.
"""

import logging
from pathlib import Path
from typing import Any

from tms_agent.rules_engine.engine import RuleEngine
from tms_agent.rules_engine.loader import RuleLoadError, load_rules

log = logging.getLogger(__name__)


class RuleBook:
    def __init__(self, rules_dir: str | Path):
        self.rules_dir = Path(rules_dir)
        self._fingerprint: tuple | None = None
        self.engine: RuleEngine = RuleEngine([])
        self.last_error: str | None = None
        self.reload(force=True)

    def _current_fingerprint(self) -> tuple:
        files = sorted([*self.rules_dir.glob("*.yaml"), *self.rules_dir.glob("*.yml")])
        return tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)

    def reload(self, force: bool = False) -> bool:
        """Recharge si les fichiers ont changé. Renvoie True si un nouveau jeu de règles est actif."""
        fingerprint = self._current_fingerprint()
        if not force and fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        try:
            engine = RuleEngine(load_rules(self.rules_dir))
        except (RuleLoadError, ValueError) as exc:
            self.last_error = str(exc)
            if force and not self.engine.rules:
                raise
            log.error("Règles invalides, l'ancien jeu reste actif :\n%s", exc)
            return False
        self.engine, self.last_error = engine, None
        log.info(
            "%d règle(s) active(s) chargée(s) depuis %s",
            len(engine.enabled_rules),
            self.rules_dir,
        )
        return True

    @property
    def state_rule_ids(self) -> set[str]:
        return {r.id for r in self.engine.enabled_rules if not r.event_scoped}

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "version": r.version,
                "enabled": r.enabled,
                "severity": r.severity,
                "category": r.category,
                "scope": "event" if r.event_scoped else "state",
                "description": " ".join(r.description.split()),
                "conditions": [
                    f"{leaf.field} {leaf.operator} {leaf.value!r}" for leaf in r.leaves
                ],
                "action": r.then.action,
                "notify": [n.model_dump(exclude_none=True) for n in r.then.notify],
            }
            for r in self.engine.rules
        ]
