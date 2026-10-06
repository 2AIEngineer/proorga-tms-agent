"""Boucle de surveillance (LangGraph) — un cycle par invocation.

    START → observe ─┬─ TMS/MCP injoignable ───────────────────────────→ report → END
                     └→ evaluate → act ─┬─ nouvelles alertes + LLM dispo → analyze → report → END
                                        └─ sinon ─────────────────────────────────→ report → END

- observe  : missions en cours + instantané complet de chacune (MCP, en parallèle, borné).
- evaluate : moteur de règles déterministe (règles d'état + règles d'événement sur les nouveaux
             événements), règles rechargées à chaud.
- act      : `create_alert` (déduplication/cooldown atomiques côté serveur) → `notify_recipients`
             selon le bloc `notify` de la règle ; `log_only` / `create_report` journalisés ;
             clôture automatique des alertes dont la condition a disparu ou dont la mission est
             terminée.
- analyze  : enquête du LLM sur les nouvelles alertes (diagnostic, cause, risque, actions),
             jointe à l'alerte (`annotate_alert`). Par défaut en arrière-plan : la détection du
             cycle suivant n'attend pas la fin des enquêtes.
- report   : bilan du cycle, état persistant (événements traités).

La détection et la notification ne dépendent jamais du LLM : sans lui (pas de clé, panne,
quota), l'agent continue d'alerter selon les règles ; seule l'analyse est sautée.
Un événement n'est marqué traité qu'une fois ses alertes enregistrées : un échec est repris au
cycle suivant.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from tms_agent.config import AgentSettings
from tms_agent.graph.investigator import Investigator
from tms_agent.llm import LlmClient, create_llm_client
from tms_agent.mcp_gateway import McpGateway, McpToolError, McpUnavailable
from tms_agent.rules_engine import RuleBook, mission_facts, snapshot_context
from tms_agent.state import AgentStateStore
from tms_agent.tools import (
    COMPACT_INVESTIGATION_TOOLS,
    RULE_DRIVEN_TOOLS,
    build_toolbox,
    submit_assessment_tool,
    use_compact_toolset,
)

log = logging.getLogger(__name__)

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


async def gather_limited(coros: list[Awaitable[Any]], limit: int) -> list[Any]:
    """`asyncio.gather` avec concurrence bornée ; les exceptions sont renvoyées, pas levées."""
    sem = asyncio.Semaphore(max(1, limit))

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


EventSink = Callable[[str, dict[str, Any]], None]


class CycleState(TypedDict, total=False):
    cycle: int
    started_at: float
    tms_time: str
    fatal: str | None
    missions: list[dict[str, Any]]
    snapshots: dict[str, dict[str, Any]]
    observe_errors: list[str]
    matches: list[dict[str, Any]]
    triggered_state: dict[str, list[str]]
    evaluated_events: dict[str, list[str]]
    emitted: list[dict[str, Any]]
    suppressed: list[dict[str, Any]]
    logged: list[dict[str, Any]]
    resolved: list[dict[str, Any]]
    act_errors: list[str]
    analyses: list[dict[str, Any]]
    investigations_queued: list[str]
    summary: dict[str, Any]


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class MonitoringAgent:
    def __init__(
        self,
        settings: AgentSettings,
        gateway: McpGateway,
        *,
        llm: LlmClient | None = None,
        state_store: AgentStateStore | None = None,
        rulebook: RuleBook | None = None,
        on_event: EventSink | None = None,
    ):
        self.settings = settings
        self.gateway = gateway
        self.llm = llm or create_llm_client(settings)
        self._owns_state_store = state_store is None
        self.state_store = state_store or AgentStateStore.from_settings(settings)
        self.rulebook = rulebook or RuleBook(settings.rules_dir)
        self.emit: EventSink = on_event or (lambda kind, data: None)
        self.investigator: Investigator | None = None
        self._investigations: dict[
            str, asyncio.Task
        ] = {}  # alert_id -> enquête en arrière-plan
        self._investigation_slots: asyncio.Semaphore | None = None
        self.graph = self._build_graph()

    async def setup(self) -> None:
        """Connexion MCP et préparation de l'enquêteur (tools découverts dynamiquement)."""
        await self.gateway.connect()
        await self._prepare_investigator()

    async def _prepare_investigator(self) -> bool:
        if not await self.llm.probe():
            self.emit(
                "llm_disabled",
                {"reason": self.llm.disabled_reason, "retry": self.llm.recoverable},
            )
            return False
        compact = use_compact_toolset(self.settings, self.llm.context_window)
        toolbox = await build_toolbox(
            self.gateway,
            self.rulebook,
            deny=RULE_DRIVEN_TOOLS | {"annotate_alert", "resolve_alert"},
            allow=COMPACT_INVESTIGATION_TOOLS if compact else None,
            compact=compact,
            extra=[submit_assessment_tool(compact)],
            result_max_chars=self.settings.tool_result_max_chars_compact
            if compact
            else self.settings.tool_result_max_chars,
        )
        self.investigator = Investigator(
            self.llm, toolbox, self.settings, on_event=self._llm_event
        )
        self.emit(
            "llm_ready",
            {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "context_window": self.llm.context_window,
                "compact": compact,
                "tools": len(toolbox.names),
            },
        )
        return True

    def _llm_event(self, kind: str, data: dict[str, Any]) -> None:
        self.emit(f"llm_{kind}", data)

    # =================================================================================
    # Nœuds
    # =================================================================================

    async def observe(self, state: CycleState) -> dict[str, Any]:
        try:
            active = await self.gateway.call("get_active_missions", {})
        except (McpToolError, McpUnavailable) as exc:
            return {"fatal": f"lecture des missions impossible : {exc}"}
        missions = active.get("missions") or []
        results = await gather_limited(
            [
                self.gateway.call("get_mission_snapshot", {"mission_id": m["id"]})
                for m in missions
            ],
            self.settings.mission_concurrency,
        )
        snapshots: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for mission, result in zip(missions, results):
            if isinstance(result, BaseException):
                errors.append(
                    f"{mission.get('reference') or mission['id']} : {_err(result)}"
                )
            else:
                snapshots[mission["id"]] = result
                if result.get("errors"):
                    errors.append(
                        f"{mission.get('reference') or mission['id']} partiel : {result['errors']}"
                    )
        tms_time = active.get("tms_time") or datetime.now(UTC).isoformat()
        self.emit(
            "observed",
            {"tms_time": tms_time, "missions": len(missions), "errors": errors},
        )
        return {
            "tms_time": tms_time,
            "missions": missions,
            "snapshots": snapshots,
            "observe_errors": errors,
        }

    async def evaluate(self, state: CycleState) -> dict[str, Any]:
        if self.rulebook.reload():
            self.emit(
                "rules_reloaded", {"count": len(self.rulebook.engine.enabled_rules)}
            )
        engine = self.rulebook.engine
        matches: list[dict[str, Any]] = []
        triggered_state: dict[str, list[str]] = {}
        evaluated_events: dict[str, list[str]] = {}
        for mission_id, snapshot in state.get("snapshots", {}).items():
            try:
                context, now = snapshot_context(snapshot)
                processed = self.state_store.processed_event_ids(mission_id)
                new_events = [
                    e
                    for e in snapshot["mission"].get("events", [])
                    if e["id"] not in processed
                ]
                found = engine.evaluate_mission(context, now, new_events)
            except Exception as exc:
                log.exception("Évaluation impossible pour %s", mission_id)
                self.emit(
                    "error", {"where": f"évaluation {mission_id}", "error": _err(exc)}
                )
                continue
            evaluated_events[mission_id] = [e["id"] for e in new_events]
            triggered_state[mission_id] = sorted(
                {m.rule_id for m in found if m.event_id is None}
            )
            for m in found:
                d = m.to_dict()
                d.pop("trace", None)
                d["mission_reference"] = snapshot["mission"].get("reference")
                matches.append(d)
        matches.sort(key=lambda d: -SEVERITY_RANK.get(d["severity"], 0))
        return {
            "matches": matches,
            "triggered_state": triggered_state,
            "evaluated_events": evaluated_events,
        }

    async def act(self, state: CycleState) -> dict[str, Any]:
        emitted, suppressed, logged, errors = [], [], [], []
        failed_event_missions: set[str] = set()

        for match in state.get("matches", []):
            try:
                if match["action"] == "emit_alert":
                    result = await self.gateway.call(
                        "create_alert",
                        {
                            "rule_id": match["rule_id"],
                            "rule_version": match["rule_version"],
                            "severity": match["severity"],
                            "category": match["category"],
                            "title": match["title"],
                            "message": match["message"],
                            "mission_id": match["mission_id"],
                            "event_id": match["event_id"],
                            "observed_data": match["observed_data"],
                            "deduplication_key": match["deduplication_key"],
                            "cooldown_minutes": match["cooldown_minutes"],
                        },
                    )
                    alert = result["alert"]
                    if not result["created"]:
                        suppressed.append(
                            {
                                "rule_id": match["rule_id"],
                                "mission_id": match["mission_id"],
                                "alert_id": alert["id"],
                                "reason": result.get("reason"),
                            }
                        )
                        continue
                    notifications: list[dict[str, Any]] = []
                    if match["notify"]:
                        recipients = [
                            {k: v for k, v in r.items() if v is not None}
                            for r in match["notify"]
                        ]
                        try:
                            notified = await self.gateway.call(
                                "notify_recipients",
                                {"alert_id": alert["id"], "recipients": recipients},
                            )
                            notifications = notified["notifications"]
                        except (McpToolError, McpUnavailable) as exc:
                            # L'alerte existe : on ne la recrée pas, l'échec est journalisé pour reprise humaine.
                            errors.append(f"notification de {alert['id']} : {exc}")
                            await self._safe_log(
                                "notification_failed",
                                match["mission_id"],
                                alert["id"],
                                {"error": str(exc)},
                            )
                    entry = {
                        "alert": alert,
                        "notifications": notifications,
                        "mission_reference": match.get("mission_reference"),
                    }
                    emitted.append(entry)
                    self.emit("alert_emitted", entry)
                else:
                    log_type = (
                        "report"
                        if match["action"] == "create_report"
                        else "rule_observation"
                    )
                    await self.gateway.call(
                        "log_agent_action",
                        {
                            "type": log_type,
                            "mission_id": match["mission_id"],
                            "payload": {
                                k: match[k]
                                for k in (
                                    "rule_id",
                                    "rule_version",
                                    "severity",
                                    "title",
                                    "message",
                                    "observed_data",
                                    "event_id",
                                )
                            },
                        },
                    )
                    logged.append(
                        {
                            "rule_id": match["rule_id"],
                            "mission_id": match["mission_id"],
                            "type": log_type,
                        }
                    )
            except (McpToolError, McpUnavailable, KeyError) as exc:
                errors.append(
                    f"{match['rule_id']} / {match['mission_id']} : {_err(exc)}"
                )
                if match.get("event_id"):
                    failed_event_missions.add(match["mission_id"])

        # Événements traités : seulement si toutes leurs alertes ont été enregistrées.
        tms_time = state.get("tms_time", "")
        for mission_id, event_ids in state.get("evaluated_events", {}).items():
            if event_ids and mission_id not in failed_event_missions:
                self.state_store.mark_processed(mission_id, event_ids, tms_time)

        resolved = (
            await self._auto_resolve(state, errors)
            if self.settings.auto_resolve
            else []
        )
        for e in errors:
            self.emit("error", {"where": "action", "error": e})
        return {
            "emitted": emitted,
            "suppressed": suppressed,
            "logged": logged,
            "resolved": resolved,
            "act_errors": errors,
        }

    async def _auto_resolve(
        self, state: CycleState, errors: list[str]
    ) -> list[dict[str, Any]]:
        try:
            open_alerts = (
                await self.gateway.call("list_alerts", {"status": "open", "limit": 500})
            )["alerts"]
        except (McpToolError, McpUnavailable) as exc:
            errors.append(f"lecture des alertes ouvertes : {exc}")
            return []
        snapshots = state.get("snapshots", {})
        active_ids = {m["id"] for m in state.get("missions", [])}
        triggered = state.get("triggered_state", {})
        state_rules = self.rulebook.state_rule_ids
        mission_status: dict[str, str | None] = {}
        resolved = []
        for alert in open_alerts:
            mission_id = alert.get("mission_id")
            reason = None
            if mission_id in snapshots:
                snapshot = snapshots[mission_id]
                if (
                    alert.get("event_id") is None
                    and alert["rule_id"] in state_rules
                    and not snapshot.get("errors")
                    and alert["rule_id"] not in triggered.get(mission_id, [])
                ):
                    reason = f"condition levée : la règle {alert['rule_id']} n'est plus vérifiée"
            elif mission_id and mission_id not in active_ids:
                if mission_id not in mission_status:
                    try:
                        mission_status[mission_id] = (
                            await self.gateway.call(
                                "get_mission_details", {"mission_id": mission_id}
                            )
                        )["status"]
                    except (McpToolError, McpUnavailable):
                        mission_status[mission_id] = None
                status = mission_status[mission_id]
                if status and status != "in_progress":
                    reason = f"mission {status}"
            if reason is None:
                continue
            try:
                await self.gateway.call(
                    "resolve_alert", {"alert_id": alert["id"], "reason": reason}
                )
            except (McpToolError, McpUnavailable) as exc:
                errors.append(f"clôture de {alert['id']} : {exc}")
                continue
            entry = {
                "alert_id": alert["id"],
                "rule_id": alert["rule_id"],
                "mission_id": mission_id,
                "reason": reason,
            }
            resolved.append(entry)
            self.emit("alert_resolved", entry)
        return resolved

    async def _investigate(
        self, entry: dict[str, Any], tms_time: str, facts: list[str] | None
    ) -> dict[str, Any]:
        alert = entry["alert"]
        if self._investigation_slots is None:
            self._investigation_slots = asyncio.Semaphore(
                max(1, self.settings.investigation_concurrency)
            )
        async with self._investigation_slots:
            self.emit("investigation_started", {"alert": alert})
            started = time.monotonic()
            try:
                assessment = await self.investigator.investigate(
                    alert, entry["notifications"], tms_time, facts
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — l'échec d'une enquête n'affecte ni l'alerte ni les autres
                await self._safe_log(
                    "investigation_failed",
                    alert.get("mission_id"),
                    alert["id"],
                    {"error": _err(exc)},
                )
                self.emit(
                    "error", {"where": f"enquête {alert['id']}", "error": _err(exc)}
                )
                return {"alert_id": alert["id"], "status": "failed", "error": _err(exc)}
            assessment["duration_s"] = round(time.monotonic() - started, 1)
            try:
                await self.gateway.call(
                    "annotate_alert", {"alert_id": alert["id"], "analysis": assessment}
                )
            except (McpToolError, McpUnavailable) as exc:
                self.emit(
                    "error", {"where": f"annotation {alert['id']}", "error": str(exc)}
                )
            self.emit("investigation_done", {"alert": alert, "assessment": assessment})
            return {"alert_id": alert["id"], **assessment}

    async def analyze(self, state: CycleState) -> dict[str, Any]:
        tms_time = state.get("tms_time", "")
        snapshots = state.get("snapshots", {})
        jobs = []
        for entry in self._investigation_targets(state):
            snapshot = snapshots.get(entry["alert"].get("mission_id"))
            try:
                facts = mission_facts(snapshot) if snapshot else None
            except Exception:
                log.exception("Faits clés indisponibles pour %s", entry["alert"]["id"])
                facts = None
            jobs.append((entry, facts))

        if not self.settings.background_investigations:
            results = await asyncio.gather(
                *(self._investigate(e, tms_time, f) for e, f in jobs),
                return_exceptions=True,
            )
            return {
                "analyses": [
                    r if isinstance(r, dict) else {"status": "failed", "error": _err(r)}
                    for r in results
                ]
            }

        queued = []
        for entry, facts in jobs:
            alert = entry["alert"]
            if len(self._investigations) >= self.settings.max_pending_investigations:
                await self._safe_log(
                    "investigation_skipped",
                    alert.get("mission_id"),
                    alert["id"],
                    {"reason": "file d'enquêtes pleine"},
                )
                self.emit(
                    "error",
                    {
                        "where": f"enquête {alert['id']}",
                        "error": "non lancée : file d'enquêtes pleine",
                    },
                )
                continue
            task = asyncio.create_task(
                self._investigate(entry, tms_time, facts), name=f"enquete-{alert['id']}"
            )
            self._investigations[alert["id"]] = task
            task.add_done_callback(
                lambda _t, key=alert["id"]: self._investigations.pop(key, None)
            )
            queued.append(alert["id"])
        return {"investigations_queued": queued}

    @property
    def pending_investigations(self) -> int:
        return len(self._investigations)

    async def wait_investigations(self, timeout: float | None = None) -> None:
        """Attend la fin des enquêtes en arrière-plan (commande `cycle`, arrêt propre)."""
        tasks = list(self._investigations.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self, grace_s: float = 0) -> None:
        if grace_s:
            await self.wait_investigations(grace_s)
        for task in list(self._investigations.values()):
            task.cancel()
        await asyncio.gather(*self._investigations.values(), return_exceptions=True)
        if self._owns_state_store:
            self.state_store.close()

    async def report(self, state: CycleState) -> dict[str, Any]:
        summary = {
            "cycle": state.get("cycle"),
            "tms_time": state.get("tms_time"),
            "duration_s": round(
                time.monotonic() - state.get("started_at", time.monotonic()), 2
            ),
            "fatal": state.get("fatal"),
            "missions": len(state.get("missions", [])),
            "rules_triggered": len(state.get("matches", [])),
            "alerts_emitted": len(state.get("emitted", [])),
            "alerts_suppressed": len(state.get("suppressed", [])),
            "alerts_resolved": len(state.get("resolved", [])),
            "logged": len(state.get("logged", [])),
            "analyses": sum(
                1
                for a in state.get("analyses", [])
                if a.get("status") in ("complete", "partial")
            ),
            "investigations_queued": len(state.get("investigations_queued", [])),
            "investigations_pending": self.pending_investigations,
            "errors": len(state.get("observe_errors", []))
            + len(state.get("act_errors", [])),
        }
        if state.get("tms_time") and not state.get("fatal"):
            self.state_store.set("last_cycle_tms_time", state["tms_time"])
        self.emit("cycle_done", summary)
        return {"summary": summary}

    # =================================================================================
    # Graphe
    # =================================================================================

    def _investigation_targets(self, state: CycleState) -> list[dict[str, Any]]:
        threshold = SEVERITY_RANK[self.settings.investigate_min_severity]
        targets = [
            e
            for e in state.get("emitted", [])
            if SEVERITY_RANK.get(e["alert"]["severity"], 0) >= threshold
        ]
        targets.sort(key=lambda e: -SEVERITY_RANK.get(e["alert"]["severity"], 0))
        return targets[: self.settings.max_investigations_per_cycle]

    def _after_observe(self, state: CycleState) -> str:
        return "report" if state.get("fatal") else "evaluate"

    def _after_act(self, state: CycleState) -> str:
        if (
            self.investigator is not None
            and self.llm.available
            and self._investigation_targets(state)
        ):
            return "analyze"
        return "report"

    def _build_graph(self):
        graph = StateGraph(CycleState)
        graph.add_node("observe", self.observe)
        graph.add_node("evaluate", self.evaluate)
        graph.add_node("act", self.act)
        graph.add_node("analyze", self.analyze)
        graph.add_node("report", self.report)
        graph.add_edge(START, "observe")
        graph.add_conditional_edges(
            "observe", self._after_observe, {"evaluate": "evaluate", "report": "report"}
        )
        graph.add_edge("evaluate", "act")
        graph.add_conditional_edges(
            "act", self._after_act, {"analyze": "analyze", "report": "report"}
        )
        graph.add_edge("analyze", "report")
        graph.add_edge("report", END)
        return graph.compile()

    async def run_cycle(self, cycle: int = 1) -> CycleState:
        every = self.settings.llm_reprobe_every_cycles
        if (
            not self.llm.available
            and self.llm.recoverable
            and every
            and cycle % every == 0
        ):
            # Serveur LLM arrêté au démarrage ou depuis : on retente sans interrompre la surveillance.
            await self._prepare_investigator()
        if self.rulebook.last_error:
            self.emit("error", {"where": "règles", "error": self.rulebook.last_error})
        return await self.graph.ainvoke(
            {"cycle": cycle, "started_at": time.monotonic()}
        )

    async def _safe_log(
        self,
        type_: str,
        mission_id: str | None,
        alert_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self.gateway.call(
                "log_agent_action",
                {
                    "type": type_,
                    "mission_id": mission_id,
                    "alert_id": alert_id,
                    "payload": payload,
                },
            )
        except (McpToolError, McpUnavailable) as exc:
            log.warning("Journalisation impossible (%s) : %s", type_, exc)
