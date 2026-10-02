"""Ligne de commande de l'agent.

tms-agent watch                 # surveillance continue (boucle LangGraph)
tms-agent cycle                 # un seul cycle, bilan JSON
tms-agent ask "question"        # question ponctuelle à l'assistant opérateur
tms-agent chat                  # conversation avec l'assistant opérateur
tms-agent report                # rapport de situation
tms-agent rules | tools         # règles actives / tools découverts via MCP
tms-agent reset-state -y        # oublie les événements déjà traités
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from tms_agent.config import AgentSettings, get_settings
from tms_agent.graph.assistant import OperatorAssistant
from tms_agent.graph.monitor import MonitoringAgent
from tms_agent.llm import LlmUnavailable, create_llm_client
from tms_agent.mcp_gateway import McpGateway, McpUnavailable
from tms_agent.prompts import SHIFT_REPORT_REQUEST
from tms_agent.rules import RuleBook
from tms_agent.state import AgentStateStore
from tms_agent.tools import (
    COMPACT_ASSISTANT_TOOLS,
    RULE_DRIVEN_TOOLS,
    build_toolbox,
    use_compact_toolset,
)

SEVERITY_MARK = {"low": "·", "medium": "!", "high": "!!", "critical": "!!!"}


class Console:
    """Affiche les événements de l'agent de façon lisible pour un opérateur."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._header: str | None = (
            None  # en-tête du cycle, imprimé au premier fait marquant
        )

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        handler = getattr(self, f"on_{kind}", None)
        if handler:
            handler(data)

    def out(self, text: str = "") -> None:
        if self._header is not None:
            header, self._header = self._header, None
            print(header, flush=True)
        print(text, flush=True)

    def on_observed(self, d):
        errors = f" — {len(d['errors'])} erreur(s)" if d["errors"] else ""
        self._header = (
            f"\n[{d['tms_time']}] {d['missions']} mission(s) en cours{errors}"
        )
        if self.verbose or d["errors"]:
            for e in d["errors"]:
                self.out(f"    ⚠ {e}")
        if self.verbose:
            self.out()

    def on_alert_emitted(self, d):
        a = d["alert"]
        self.out(
            f"  {SEVERITY_MARK.get(a['severity'], '')} ALERTE {a['severity'].upper()} {a['rule_id']} "
            f"[{d.get('mission_reference') or a['mission_id']}] {a['id']}"
        )
        self.out(f"      {a['message']}")
        for n in d["notifications"]:
            status = "✓" if n["status"] == "delivered" else f"✗ {n.get('error')}"
            self.out(
                f"      → {n['recipient_role']} {n['management_level']} {n.get('recipient_name') or ''} via {n['channel']} {status}"
            )

    def on_alert_resolved(self, d):
        self.out(f"  ✓ alerte {d['alert_id']} ({d['rule_id']}) close : {d['reason']}")

    def on_rules_reloaded(self, d):
        self.out(f"  ↻ règles rechargées : {d['count']} active(s)")

    def on_investigation_started(self, d):
        self.out(f"  🔎 enquête sur {d['alert']['id']} ({d['alert']['rule_id']})…")

    def on_investigation_done(self, d):
        a = d["assessment"]
        self.out(
            f"  🔎 {d['alert']['id']} — risque {a.get('risk_level', '?')}, confiance {a.get('confidence', '?')} "
            f"({a.get('duration_s')} s, {len(a.get('tool_calls', []))} lecture(s))"
        )
        self.out(f"      {a.get('summary')}")
        if a.get("probable_cause"):
            self.out(f"      cause probable : {a['probable_cause']}")
        for action in a.get("recommended_actions") or []:
            self.out(f"      ▸ {action}")
        if a.get("driver_called"):
            self.out("      ☎ chauffeur appelé")

    def on_llm_tool_call(self, d):
        if self.verbose:
            self.out(
                f"      · {d['name']}({json.dumps(d['input'], ensure_ascii=False)[:140]})"
            )

    def on_llm_tool_result(self, d):
        if self.verbose and d["is_error"]:
            self.out(f"      ✗ {d['name']} : {d['preview'][:160]}")

    def on_llm_nudge(self, d):
        if self.verbose:
            self.out(f"      ↺ relance : conclure avec {d['tool']}")

    def on_llm_thinking(self, d):
        if self.verbose:
            self.out(f"      … {d['text'][:400]}")

    def on_llm_disabled(self, d):
        retry = " ; nouvel essai périodique" if d.get("retry") else ""
        self.out(
            f"⚠ LLM indisponible ({d['reason']}) : mode déterministe (règles + notifications, sans enquête){retry}."
        )

    def on_llm_ready(self, d):
        profile = "compact" if d["compact"] else "complet"
        self.out(
            f"✓ LLM {d['provider']} {d['model']} prêt — contexte {d['context_window'] or '?'} tokens, "
            f"profil d'outils {profile} ({d['tools']} tools)."
        )

    def on_error(self, d):
        self.out(f"  ⚠ {d['where']} : {d['error']}")

    def on_cycle_done(self, d):
        if d["fatal"]:
            self.out(f"  ✗ cycle {d['cycle']} interrompu : {d['fatal']}")
            return
        if self.verbose or d["alerts_emitted"] or d["alerts_resolved"] or d["errors"]:
            investigations = (
                f"{d['investigations_queued']} enquête(s) lancée(s), {d['investigations_pending']} en cours"
                if d.get("investigations_queued") or d.get("investigations_pending")
                else f"{d['analyses']} analyse(s)"
            )
            self.out(
                f"  cycle {d['cycle']} : {d['rules_triggered']} règle(s) déclenchée(s), {d['alerts_emitted']} alerte(s) émise(s), "
                f"{d['alerts_suppressed']} dédupliquée(s), {d['alerts_resolved']} close(s), {investigations}, "
                f"{d['duration_s']} s"
            )


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpx2", "anthropic", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _settings(args) -> AgentSettings:
    settings = get_settings()
    updates: dict[str, Any] = {}
    if getattr(args, "no_llm", False):
        updates["llm_enabled"] = False
    if getattr(args, "interval", None):
        updates["poll_interval_s"] = args.interval
    if getattr(args, "provider", None):
        updates["llm_provider"] = args.provider
    if getattr(args, "model", None):
        provider = updates.get("llm_provider", settings.llm_provider)
        updates["anthropic_model" if provider == "anthropic" else "local_model"] = (
            args.model
        )
    return settings.model_copy(update=updates) if updates else settings


# --- Commandes --------------------------------------------------------------------------


async def _watch(args) -> int:
    settings = _settings(args)
    console = Console(args.verbose)
    gateway = McpGateway.from_settings(settings)
    agent = MonitoringAgent(settings, gateway, on_event=console)
    try:
        await agent.setup()
        mode = (
            f"LLM {agent.llm.provider} {agent.llm.model}"
            if agent.llm.available
            else "déterministe"
        )
        console.out(
            f"Agent de surveillance démarré — {len(agent.rulebook.engine.enabled_rules)} règle(s), "
            f"mode {mode}, cycle toutes les {settings.poll_interval_s:g} s. Ctrl+C pour arrêter."
        )
        cycle, failures = 0, 0
        while not args.cycles or cycle < args.cycles:
            cycle += 1
            state = await agent.run_cycle(cycle)
            failures = failures + 1 if state.get("fatal") else 0
            if args.cycles and cycle >= args.cycles:
                break
            # Backoff si le TMS ou le serveur MCP est injoignable.
            await asyncio.sleep(
                settings.poll_interval_s * min(2**failures, 12)
                if failures
                else settings.poll_interval_s
            )
    except McpUnavailable as exc:
        console.out(f"✗ {exc}")
        return 2
    finally:
        await (
            agent.shutdown()
        )  # enquêtes en cours annulées ; les alertes sont déjà enregistrées
        await gateway.close()
    return 0


async def _cycle(args) -> int:
    settings = _settings(args)
    console = Console(args.verbose)
    async with McpGateway.from_settings(settings) as gateway:
        agent = MonitoringAgent(settings, gateway, on_event=console)
        await agent.setup()
        try:
            state = await agent.run_cycle(1)
            if agent.pending_investigations:
                console.out(
                    f"  … attente de {agent.pending_investigations} enquête(s) en cours"
                )
                await agent.wait_investigations()
        finally:
            await agent.shutdown()
    console.out(json.dumps(state.get("summary"), ensure_ascii=False, indent=2))
    return 1 if state.get("summary", {}).get("fatal") else 0


async def _assistant_session(args, questions: list[str] | None) -> int:
    settings = _settings(args)
    console = Console(args.verbose)
    llm = create_llm_client(settings)
    if not await llm.probe():
        console.out(f"✗ L'assistant nécessite un LLM : {llm.disabled_reason}")
        return 2
    async with McpGateway.from_settings(settings) as gateway:
        assistant = OperatorAssistant(
            settings, gateway, llm, RuleBook(settings.rules_dir), on_event=console
        )
        try:
            return await _converse(assistant, console, args.verbose, questions)
        finally:
            await assistant.close()


async def _converse(
    assistant: OperatorAssistant,
    console: Console,
    verbose: bool,
    questions: list[str] | None,
) -> int:
    async def answer(question: str) -> None:
        try:
            result = await assistant.ask(question)
        except LlmUnavailable as exc:
            console.out(f"✗ {exc}")
            return
        console.out(f"\n{result.text}\n")
        if verbose:
            console.out(
                f"  [{len(result.tool_calls)} tool(s) : {', '.join(result.tool_calls)} — {result.usage}]"
            )

    if questions is not None:
        for q in questions:
            await answer(q)
        return 0
    console.out(
        "Assistant opérateur — posez vos questions (« /nouveau » pour repartir de zéro, « /quitter » pour sortir)."
    )
    while True:
        try:
            question = await asyncio.to_thread(input, "opérateur> ")
        except (EOFError, KeyboardInterrupt):
            console.out()
            return 0
        question = question.strip()
        if not question:
            continue
        if question in ("/quitter", "/exit", "/quit"):
            return 0
        if question == "/nouveau":
            assistant.new_thread()
            console.out("Nouvelle conversation.")
            continue
        await answer(question)


async def _tools(args) -> int:
    settings = _settings(args)
    llm = create_llm_client(settings)
    await llm.probe()
    compact = use_compact_toolset(settings, llm.context_window)
    print(
        f"Profil {'compact' if compact else 'complet'} (LLM {llm.provider} {llm.model}, contexte {llm.context_window or '?'}) "
        "— outils de l'assistant :"
    )
    async with McpGateway.from_settings(settings) as gateway:
        toolbox = await build_toolbox(
            gateway,
            RuleBook(settings.rules_dir),
            deny=RULE_DRIVEN_TOOLS,
            allow=COMPACT_ASSISTANT_TOOLS if compact else None,
            compact=compact,
        )
        for d in toolbox.definitions():
            first = d["description"].split(". ")[0][:90]
            print(f"{d['name']:28} {first}")
    return 0


def cmd_rules(args) -> int:
    rulebook = RuleBook(_settings(args).rules_dir)
    for r in rulebook.describe():
        state = "active" if r["enabled"] else "désactivée"
        print(
            f"{r['id']} v{r['version']} [{r['severity']}/{r['category']}] {state}, portée {r['scope']}"
        )
        print(f"    {r['description']}")
        print(f"    si : {' ET '.join(r['conditions'])}")
        recipients = [
            f"{n.get('role') or n.get('user_id')}/{n['management_level']}/{n['channel']}"
            for n in r["notify"]
        ]
        print(f"    notifier : {', '.join(recipients) or '—'}")
    return 0


def cmd_reset_state(args) -> int:
    if not args.yes:
        print("Ajouter -y pour confirmer.", file=sys.stderr)
        return 1
    store = AgentStateStore.from_settings(_settings(args))
    store.reset()
    store.close()
    print("État de la boucle de surveillance réinitialisé.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tms-agent", description="Agent IA de suivi des missions de transport."
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Détail des lectures, raisonnement et erreurs.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Mode déterministe (règles + notifications, sans LLM).",
    )
    parser.add_argument(
        "--provider",
        choices=["local", "anthropic"],
        help="Fournisseur LLM (défaut : configuration, local).",
    )
    parser.add_argument(
        "--model", help="Modèle (défaut : configuration du fournisseur)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("watch", help="Surveillance continue.")
    p.add_argument("--interval", type=float, help="Secondes entre deux cycles.")
    p.add_argument(
        "--cycles", type=int, default=0, help="Nombre de cycles (0 = sans fin)."
    )
    p.set_defaults(run=_watch)

    sub.add_parser("cycle", help="Exécute un seul cycle de surveillance.").set_defaults(
        run=_cycle
    )

    p = sub.add_parser("ask", help="Pose une question à l'assistant opérateur.")
    p.add_argument("question", nargs="+")
    p.set_defaults(run=lambda a: _assistant_session(a, [" ".join(a.question)]))

    sub.add_parser(
        "chat", help="Conversation avec l'assistant opérateur."
    ).set_defaults(run=lambda a: _assistant_session(a, None))
    sub.add_parser("report", help="Rapport de situation.").set_defaults(
        run=lambda a: _assistant_session(a, [SHIFT_REPORT_REQUEST])
    )
    sub.add_parser(
        "tools", help="Tools découverts via MCP, tels que vus par le LLM."
    ).set_defaults(run=_tools)
    sub.add_parser("rules", help="Règles métier actives.").set_defaults(func=cmd_rules)

    p = sub.add_parser("reset-state", help="Oublie les événements déjà traités.")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_reset_state)

    args = parser.parse_args(argv)
    _logging(args.verbose)
    if hasattr(args, "func"):
        return args.func(args)
    try:
        return asyncio.run(args.run(args))
    except KeyboardInterrupt:
        print("\nArrêt demandé.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
