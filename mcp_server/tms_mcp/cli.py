"""Ligne de commande du serveur MCP.

    tms-mcp serve                         # stdio (lancé par l'agent)
    tms-mcp serve --transport http        # Streamable HTTP sur http://127.0.0.1:8002/mcp
    tms-mcp tools                         # catalogue des tools (nom, catégorie, description)
    tms-mcp alerts [--status open]        # alertes de la base de l'agent
    tms-mcp reset-db -y                   # vide la base de l'agent
"""

import argparse
import asyncio
import json
import logging
import sys

from tms_mcp.config import get_settings
from tms_mcp.server import create_server
from tms_mcp.store import AgentStore


def _logging(level: str) -> None:
    # stderr uniquement : en transport stdio, stdout est réservé au protocole MCP.
    logging.basicConfig(stream=sys.stderr, level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    settings = get_settings()
    server = create_server(settings)
    if args.transport == "http":
        server.run("streamable-http", host=args.host or settings.mcp_host, port=args.port or settings.mcp_port)
    else:
        server.run("stdio")
    return 0


def cmd_tools(_args) -> int:
    from mcp import Client

    async def main():
        async with Client(create_server()) as client:
            for tool in (await client.list_tools()).tools:
                meta = tool.meta or {}
                print(f"{tool.name:28} [{meta.get('backend', '?')}/{meta.get('category', '?')}] {tool.title or ''}")

    asyncio.run(main())
    return 0


def cmd_alerts(args) -> int:
    store = AgentStore(get_settings().agent_db_path)
    alerts = store.list_alerts(status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(alerts, ensure_ascii=False, indent=2, default=str))
        return 0
    for a in alerts:
        print(f"{a['created_at']}  {a['id']}  {a['status']:8} {a['severity']:8} {a['rule_id']:24} {a['mission_id'] or '-'}")
        print(f"{'':22}{a['message'] or a['title']}")
        for n in store.list_notifications(a["id"]):
            print(f"{'':24}→ {n['recipient_role']}/{n['management_level']} {n['recipient_name'] or n['recipient_user_id']} "
                  f"via {n['channel']} : {n['status']}{' (' + n['error'] + ')' if n['error'] else ''}")
        if a.get("analysis"):
            print(f"{'':24}analyse : {a['analysis'].get('summary')}")
    print(f"{len(alerts)} alerte(s).")
    return 0


def cmd_reset_db(args) -> int:
    if not args.yes:
        print("Ajouter -y pour confirmer la suppression des alertes, notifications, appels et journal.", file=sys.stderr)
        return 1
    AgentStore(get_settings().agent_db_path).reset()
    print("Base de l'agent vidée.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tms-mcp", description="Serveur MCP de l'orchestrateur TMS.")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="Démarre le serveur MCP.")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("tools", help="Liste les tools exposés.")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("alerts", help="Affiche les alertes de la base de l'agent.")
    p.add_argument("--status", choices=["open", "resolved"])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("reset-db", help="Vide la base de l'agent.")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_reset_db)

    args = parser.parse_args(argv)
    _logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
