"""Format des règles (Document 3 — Format du moteur de règles)."""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_validator

from rules_engine.operators import OPERATORS

# Racines de champs disponibles dans le contexte d'évaluation (voir context.py).
CONTEXT_ROOTS = ("mission", "vehicle", "deviation", "eta", "driver", "event")

Severity = Literal["low", "medium", "high", "critical"]
Channel = Literal["in_app", "sms", "email", "call"]
Action = Literal["emit_alert", "log_only", "create_report"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Conditions ----------------------------------------------------------------------


class Leaf(_Strict):
    """Condition élémentaire : `field` `operator` `value`."""

    field: str
    operator: str
    value: Any = None

    @field_validator("field")
    @classmethod
    def _known_root(cls, v: str) -> str:
        root = v.split(".", 1)[0]
        if root not in CONTEXT_ROOTS or "." not in v:
            raise ValueError(f"champ {v!r} invalide : doit commencer par {', '.join(r + '.' for r in CONTEXT_ROOTS)}")
        return v

    @model_validator(mode="after")
    def _known_operator(self):
        op = OPERATORS.get(self.operator)
        if op is None:
            raise ValueError(f"opérateur inconnu {self.operator!r} (disponibles : {', '.join(sorted(OPERATORS))})")
        op.validate(self.value)
        return self


class All(_Strict):
    all: Annotated[list["Condition"], Field(min_length=1)]


class AnyOf(_Strict):
    any: Annotated[list["Condition"], Field(min_length=1)]


class Not(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    not_: "Condition" = Field(alias="not")


def _condition_kind(v: Any) -> str:
    if isinstance(v, dict):
        for key in ("all", "any", "not"):
            if key in v:
                return key
        return "leaf"
    return {All: "all", AnyOf: "any", Not: "not"}.get(type(v), "leaf")


Condition = Annotated[
    Union[
        Annotated[All, Tag("all")],
        Annotated[AnyOf, Tag("any")],
        Annotated[Not, Tag("not")],
        Annotated[Leaf, Tag("leaf")],
    ],
    Discriminator(_condition_kind),
]

All.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def iter_leaves(cond: Condition):
    if isinstance(cond, Leaf):
        yield cond
    elif isinstance(cond, All):
        for c in cond.all:
            yield from iter_leaves(c)
    elif isinstance(cond, AnyOf):
        for c in cond.any:
            yield from iter_leaves(c)
    elif isinstance(cond, Not):
        yield from iter_leaves(cond.not_)


# --- Action -------------------------------------------------------------------------


class Recipient(_Strict):
    role: str | None = None
    user_id: str | None = None
    management_level: str
    channel: Channel
    message: str | None = None

    @model_validator(mode="after")
    def _role_xor_user(self):
        if (self.role is None) == (self.user_id is None):
            raise ValueError("préciser exactement l'un des champs `role` ou `user_id`")
        return self


class AlertSpec(_Strict):
    title: str
    message: str | None = Field(None, description="Gabarit {{ champ }} ; défaut : titre + référence de mission.")
    deduplication_key: str = "{{ rule.id }}:{{ mission.id }}"
    cooldown_minutes: Annotated[int, Field(ge=0)] = 0


class Then(_Strict):
    action: Action
    alert: AlertSpec | None = None
    notify: list[Recipient] = Field(default_factory=list)

    @model_validator(mode="after")
    def _alert_required(self):
        if self.action == "emit_alert" and self.alert is None:
            raise ValueError("l'action emit_alert nécessite un bloc `alert`")
        return self


# --- Règle --------------------------------------------------------------------------


class Rule(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: Annotated[int, Field(ge=1)]
    enabled: bool = True
    severity: Severity
    category: str
    description: str
    author: str | None = None
    when: Condition
    then: Then
    observe: list[str] = Field(
        default_factory=list,
        description="Champs supplémentaires à joindre aux données observées (ex. vehicle.current_position).",
    )

    @property
    def leaves(self) -> list[Leaf]:
        return list(iter_leaves(self.when))

    @property
    def event_scoped(self) -> bool:
        """Une règle qui lit `event.*` est évaluée pour chaque nouvel événement."""
        return any(leaf.field.startswith("event.") for leaf in self.leaves)
