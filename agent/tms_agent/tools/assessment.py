"""Évaluation finale d'une enquête : schémas, validation tolérante et tool `submit_assessment`."""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from tms_agent.tools.base import LocalTool, ToolInputError

SUBMIT_ASSESSMENT = "submit_assessment"

ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Synthèse en 1 à 3 phrases, lisible par un opérateur.",
        },
        "situation": {
            "type": "string",
            "description": "Ce qui se passe concrètement, chiffres à l'appui.",
        },
        "probable_cause": {
            "type": "string",
            "description": "Cause la plus probable, ou « indéterminée » avec ce qui manque.",
        },
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "customer_impact": {
            "type": "string",
            "description": "Impact sur la livraison et le client.",
        },
        "recommended_actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actions concrètes, par ordre de priorité, avec le rôle responsable.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Faits observés qui fondent l'analyse (valeurs, horodatages).",
        },
        "driver_called": {
            "type": "boolean",
            "description": "Vrai si call_driver a été utilisé pendant l'enquête.",
        },
        "false_positive_suspected": {
            "type": "boolean",
            "description": "Vrai si les données suggèrent une fausse alerte.",
        },
        "confidence": {
            "type": "number",
            "description": "Confiance dans l'analyse, entre 0 et 1.",
        },
    },
    "required": [
        "summary",
        "situation",
        "probable_cause",
        "risk_level",
        "customer_impact",
        "recommended_actions",
        "evidence",
        "driver_called",
        "false_positive_suspected",
        "confidence",
    ],
    "additionalProperties": False,
}


_RISK_SYNONYMS = {
    "faible": "low",
    "bas": "low",
    "basse": "low",
    "moyen": "medium",
    "moyenne": "medium",
    "modéré": "medium",
    "modere": "medium",
    "élevé": "high",
    "eleve": "high",
    "élevée": "high",
    "haut": "high",
    "haute": "high",
    "critique": "critical",
    "severe": "critical",
    "sévère": "critical",
}


class Assessment(BaseModel):
    """Évaluation d'enquête. Validation tolérante : un petit modèle local peut omettre des champs
    secondaires ou écrire « élevé » ; seuls `summary` et `risk_level` sont indispensables."""

    summary: str = Field(min_length=5)
    risk_level: Literal["low", "medium", "high", "critical"]
    situation: str = ""
    probable_cause: str = "indéterminée"
    customer_impact: str = ""
    recommended_actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    driver_called: bool = False
    false_positive_suspected: bool = False
    confidence: float | None = None

    @field_validator("risk_level", mode="before")
    @classmethod
    def _risk(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower()
            return _RISK_SYNONYMS.get(v, v)
        return v

    @field_validator("recommended_actions", "evidence", mode="before")
    @classmethod
    def _as_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [
                line.strip(" -•*\t") for line in v.splitlines() if line.strip(" -•*\t")
            ]
        return [str(x) for x in v] if isinstance(v, list) else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().rstrip("%")
            try:
                v = float(v.replace(",", "."))
            except ValueError:
                return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = v / 100 if v > 1 else v
            return max(0.0, min(1.0, float(v)))
        return None


def validate_assessment(data: dict[str, Any]) -> Assessment:
    try:
        return Assessment.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'objet'} : {e['msg']}"
            for e in exc.errors()
        )
        raise ToolInputError(
            f"Évaluation invalide ({problems}). Corriger et rappeler submit_assessment."
        ) from exc


COMPACT_ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Synthèse en 1 à 3 phrases."},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "probable_cause": {"type": "string"},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Faits chiffrés observés.",
        },
        "driver_called": {"type": "boolean"},
        "confidence": {"type": "number", "description": "Entre 0 et 1."},
    },
    "required": ["summary", "risk_level", "recommended_actions"],
}


def submit_assessment_tool(compact: bool = False) -> LocalTool:
    async def handler(arguments: dict[str, Any]) -> Any:
        validate_assessment(arguments)
        return "Évaluation enregistrée. Fin de l'enquête."

    return LocalTool(
        name=SUBMIT_ASSESSMENT,
        description=(
            "Remet l'évaluation finale de l'enquête (synthèse, cause probable, risque, actions recommandées, "
            "preuves). Appeler ce tool une seule fois, en dernier, lorsque l'enquête est terminée."
        ),
        input_schema=COMPACT_ASSESSMENT_SCHEMA if compact else ASSESSMENT_SCHEMA,
        handler=handler,
        strict=not compact,
    )
