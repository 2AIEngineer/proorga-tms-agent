"""Boîte à outils exposée au LLM.

- Tools MCP **découverts dynamiquement** (`list_tools`) : nom, titre, description sémantique et
  schéma d'entrée viennent du serveur. Brancher un autre TMS ne demande aucun code ici.
- Tools locaux de l'agent : explication des règles métier, remise de l'évaluation finale.
- Chaque mode (enquête, assistant) filtre les tools autorisés : l'émission d'alertes et les
  notifications restent pilotées par les règles, jamais improvisées par le LLM.
- Profil **compact** pour les petits modèles locaux (fenêtre de 8k tokens) : sous-ensemble de
  tools par mode, descriptions réduites à leur première phrase, JSON des résultats épuré.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.types import Tool
from pydantic import BaseModel, Field, ValidationError, field_validator

from rules_engine import enrich_event
from tms_agent.llm import INVALID_ARGUMENTS
from tms_agent.mcp_gateway import McpGateway, McpToolError, McpUnavailable
from tms_agent.rules import RuleBook, snapshot_context

log = logging.getLogger(__name__)

# Tools réservés à la boucle déterministe : une alerte naît d'une règle, ses destinataires aussi.
RULE_DRIVEN_TOOLS = {"create_alert", "notify_recipients"}

# Sous-ensembles du profil compact (les tools absents du serveur MCP sont simplement ignorés).
COMPACT_INVESTIGATION_TOOLS = {
    "get_mission_snapshot",
    "get_mission_events",
    "get_vehicle_positions",
    "explain_rules_for_mission",
    "list_alerts",
    "list_agent_actions",
    "call_driver",
    "submit_assessment",
}
COMPACT_ASSISTANT_TOOLS = {
    "get_operations_overview",
    "get_active_missions",
    "get_mission_snapshot",
    "get_mission_events",
    "list_alerts",
    "get_alert",
    "explain_rules_for_mission",
    "list_business_rules",
    "list_agent_actions",
    "resolve_alert",
    "call_driver",
}


@dataclass
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    strict: bool = False


def prune(value: Any) -> Any:
    """Retire récursivement les valeurs nulles et vides (allège le contexte des petits modèles)."""
    if isinstance(value, dict):
        out = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def _strip_titles(schema: Any) -> Any:
    """Retire les `title` générés de JSON Schema, sans toucher aux propriétés nommées `title`."""
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_titles(value)
    return out


def _first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    match = re.search(r"^(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


class Toolbox:
    def __init__(
        self,
        gateway: McpGateway,
        mcp_tools: list[Tool],
        local_tools: list[LocalTool] = (),
        *,
        deny: set[str] = frozenset(),
        allow: set[str] | None = None,
        compact: bool = False,
        result_max_chars: int = 24000,
    ):
        self.gateway = gateway
        self.compact = compact
        self.result_max_chars = result_max_chars

        def keep(name: str) -> bool:
            return name not in deny and (allow is None or name in allow)

        self._mcp = {t.name: t for t in mcp_tools if keep(t.name)}
        self._local = {t.name: t for t in local_tools if keep(t.name)}

    @property
    def names(self) -> list[str]:
        return sorted({*self._mcp, *self._local})

    def definitions(self) -> list[dict[str, Any]]:
        """Définitions `{name, description, input_schema}` triées par nom (préfixe stable pour le cache)."""
        defs = []
        for name in self.names:
            if name in self._local:
                tool = self._local[name]
                description, schema = tool.description, tool.input_schema
                d = {
                    "name": name,
                    "description": _first_sentence(description)
                    if self.compact
                    else description,
                    "input_schema": schema,
                }
                if tool.strict and not self.compact:
                    d["strict"] = True
            else:
                tool = self._mcp[name]
                description = (tool.description or "").strip()
                if self.compact:
                    d = {
                        "name": name,
                        "description": _first_sentence(description),
                        "input_schema": _strip_titles(tool.input_schema),
                    }
                else:
                    if tool.title:
                        description = f"{tool.title}. {description}"
                    meta = tool.meta or {}
                    if meta.get("backend"):
                        description += (
                            f"\n[backend: {meta['backend']}, catégorie: {meta.get('category')}, "
                            f"lecture seule: {meta.get('read_only')}]"
                        )
                    d = {
                        "name": name,
                        "description": description,
                        "input_schema": tool.input_schema,
                    }
            defs.append(d)
        return defs

    async def execute(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Exécute un tool ; renvoie (contenu texte, is_error). Ne lève jamais : l'erreur est renvoyée au LLM."""
        if INVALID_ARGUMENTS in arguments:
            return (
                f"Arguments illisibles pour {name} (JSON invalide) : {arguments[INVALID_ARGUMENTS][:300]}. "
                "Relancer l'appel avec un objet JSON valide."
            ), True
        try:
            if name in self._local:
                result = await self._local[name].handler(arguments)
            elif name in self._mcp:
                result = await self.gateway.call(name, arguments)
            else:
                return (
                    f"Tool inconnu ou non autorisé dans ce mode : {name}. Tools disponibles : {', '.join(self.names)}",
                    True,
                )
        except McpToolError as exc:
            return exc.message, True
        except McpUnavailable as exc:
            return (
                f"Serveur MCP indisponible : {exc}. Réessayer plus tard ou conclure avec les données déjà obtenues.",
                True,
            )
        except ToolInputError as exc:
            return str(exc), True
        except Exception as exc:
            log.exception("Tool %s en échec", name)
            return f"Échec du tool {name} : {type(exc).__name__}: {exc}", True
        if isinstance(result, str):
            text = result
        elif self.compact:
            text = json.dumps(
                prune(result), ensure_ascii=False, default=str, separators=(",", ":")
            )
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > self.result_max_chars:
            text = (
                text[: self.result_max_chars]
                + f"\n… [résultat tronqué : {len(text)} caractères ; affiner les filtres]"
            )
        return text, False


class ToolInputError(Exception):
    """Arguments d'un tool local invalides : message renvoyé tel quel au modèle pour correction."""


# --- Tools locaux --------------------------------------------------------------------------


def rule_tools(rulebook: RuleBook, gateway: McpGateway) -> list[LocalTool]:
    async def list_business_rules(_: dict[str, Any]) -> Any:
        return {"rules": rulebook.describe()}

    async def explain_rules_for_mission(args: dict[str, Any]) -> Any:
        snapshot = await gateway.call(
            "get_mission_snapshot", {"mission_id": args["mission_id"]}
        )
        context, now = snapshot_context(snapshot)
        engine = rulebook.engine
        evaluations = [
            {
                "rule_id": e.rule_id,
                "triggered": e.triggered,
                "conditions": [
                    {
                        "field": t.field,
                        "operator": t.operator,
                        "expected": t.expected,
                        "observed": t.actual,
                        "ok": t.result,
                    }
                    for t in e.trace
                ],
            }
            for e in engine.explain(context, now)
        ]
        event_hits = []
        for event in snapshot["mission"].get("events", []):
            ctx = {**context, "event": enrich_event(event, context["mission"], now)}
            for e in engine.explain(ctx, now):
                if e.triggered:
                    event_hits.append(
                        {
                            "rule_id": e.rule_id,
                            "event_id": event["id"],
                            "event_type": event["type"],
                        }
                    )
        return {
            "tms_time": snapshot["tms_time"],
            "derived_fields": {
                "mission.last_event_age_minutes": context["mission"].get(
                    "last_event_age_minutes"
                ),
                "mission.last_event_type": context["mission"].get("last_event_type"),
                "vehicle.status": context.get("vehicle", {}).get("status"),
                "vehicle.last_seen_age_minutes": context.get("vehicle", {}).get(
                    "last_seen_age_minutes"
                ),
                "deviation.current_offset_km": context.get("deviation", {}).get(
                    "current_offset_km"
                ),
                "deviation.duration_minutes": context.get("deviation", {}).get(
                    "duration_minutes"
                ),
                "eta.delay_minutes": context.get("eta", {}).get("delay_minutes"),
            },
            "state_rules": evaluations,
            "event_rules_triggered_on_history": event_hits,
            "snapshot_errors": snapshot.get("errors") or {},
        }

    return [
        LocalTool(
            name="list_business_rules",
            description=(
                "Retourne les règles métier actives (YAML validées par l'équipe métier) : identifiant, version, "
                "sévérité, catégorie, conditions, portée (état ou événement) et destinataires à notifier. Utiliser "
                "ce tool pour savoir ce que l'entreprise considère comme une anomalie et qui doit être prévenu."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=list_business_rules,
        ),
        LocalTool(
            name="explain_rules_for_mission",
            description=(
                "Évalue toutes les règles métier sur l'état actuel d'une mission (lu dans le TMS via MCP) et "
                "détaille chaque condition : valeur observée, seuil attendu, résultat. Donne aussi les champs "
                "dérivés (âge du dernier événement, durée d'écart, retard). Utiliser ce tool pour expliquer "
                "pourquoi une alerte a été déclenchée, ou à quel point une mission est proche d'un seuil."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mission_id": {
                        "type": "string",
                        "description": "Identifiant de la mission.",
                    }
                },
                "required": ["mission_id"],
                "additionalProperties": False,
            },
            handler=explain_rules_for_mission,
        ),
    ]


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


async def build_toolbox(
    gateway: McpGateway,
    rulebook: RuleBook,
    *,
    deny: set[str] = frozenset(),
    allow: set[str] | None = None,
    compact: bool = False,
    extra: list[LocalTool] = (),
    result_max_chars: int = 24000,
) -> Toolbox:
    mcp_tools = await gateway.list_tools()
    return Toolbox(
        gateway,
        mcp_tools,
        [*rule_tools(rulebook, gateway), *extra],
        deny=deny,
        allow=allow,
        compact=compact,
        result_max_chars=result_max_chars,
    )


def use_compact_toolset(settings: Any, context_window: int | None) -> bool:
    if settings.toolset != "auto":
        return settings.toolset == "compact"
    return context_window is not None and context_window < 32000


async def gather_limited(coros: list[Awaitable[Any]], limit: int) -> list[Any]:
    """`asyncio.gather` avec concurrence bornée ; les exceptions sont renvoyées, pas levées."""
    sem = asyncio.Semaphore(max(1, limit))

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
