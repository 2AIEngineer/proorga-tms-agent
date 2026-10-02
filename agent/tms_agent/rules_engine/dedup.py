"""Déduplication et cooldown des alertes (Document 3).

- `deduplication_key` : si une alerte ouverte porte déjà cette clé, la nouvelle n'est pas émise.
- `cooldown_minutes` : délai minimal avant de ré-émettre une alerte de même clé, même fermée.

L'historique des alertes appartient à l'orchestrateur (sa base) : il implémente `AlertHistory`.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from tms_agent.rules_engine.engine import RuleMatch


class AlertHistory(Protocol):
    def has_open_alert(self, deduplication_key: str) -> bool: ...

    def last_emitted_at(self, deduplication_key: str) -> datetime | None: ...


@dataclass
class Decision:
    match: RuleMatch
    emit: bool
    reason: str | None = None  # motif de suppression


def apply_deduplication(matches: list[RuleMatch], history: AlertHistory, now: datetime) -> list[Decision]:
    decisions: list[Decision] = []
    emitted_now: set[str] = set()
    for m in matches:
        key = m.deduplication_key
        if m.action != "emit_alert" or key is None:
            decisions.append(Decision(m, True))
            continue
        if key in emitted_now:
            decisions.append(Decision(m, False, "doublon dans le même cycle"))
            continue
        if history.has_open_alert(key):
            decisions.append(Decision(m, False, "alerte déjà ouverte"))
            continue
        last = history.last_emitted_at(key)
        if last is not None and m.cooldown_minutes and now - last < timedelta(minutes=m.cooldown_minutes):
            remaining = m.cooldown_minutes - (now - last).total_seconds() / 60
            decisions.append(Decision(m, False, f"cooldown actif ({remaining:.0f} min restantes)"))
            continue
        emitted_now.add(key)
        decisions.append(Decision(m, True))
    return decisions


class InMemoryAlertHistory:
    """Implémentation en mémoire (tests, démonstrations)."""

    def __init__(self) -> None:
        self._open: set[str] = set()
        self._last: dict[str, datetime] = {}

    def record(self, deduplication_key: str, at: datetime) -> None:
        self._open.add(deduplication_key)
        self._last[deduplication_key] = at

    def resolve(self, deduplication_key: str) -> None:
        self._open.discard(deduplication_key)

    def has_open_alert(self, deduplication_key: str) -> bool:
        return deduplication_key in self._open

    def last_emitted_at(self, deduplication_key: str) -> datetime | None:
        return self._last.get(deduplication_key)
