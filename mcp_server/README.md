# Serveur MCP de l'orchestrateur

Seul point d'accès de l'agent IA au TMS (Document 2 de `docs/GOAL.md`). Il expose :

- **Famille A — tools TMS** (lecture seule) : missions, événements, véhicules, positions, itinéraires, écarts, ETA, chauffeurs, utilisateurs.
- **Famille B — tools de l'orchestrateur** : alertes, notifications, appels chauffeur, journal d'audit, tous stockés dans la **base de l'agent** (PostgreSQL, schéma `agent`). Le TMS n'est jamais modifié.

Chaque tool porte une description sémantique (intention métier + moment d'usage), un `inputSchema` documenté, un `outputSchema`, des annotations MCP (`readOnlyHint`…) et des métadonnées `_meta` (`domain`, `category`, `backend`, `read_only`, `latency_hint`, `call_frequency_hint`, `cost_hint`).

## Démarrage

```bash
uv sync                                      # à la racine ai_agent/ : environnement unique (agent + serveur MCP)
cd mcp_server
uv run tms-mcp tools                         # catalogue des tools
uv run tms-mcp serve                         # stdio (c'est ainsi que l'agent le lance)
uv run tms-mcp serve --transport http        # Streamable HTTP : http://127.0.0.1:8002/mcp
uv run tms-mcp alerts [--status open]        # alertes, notifications et analyses
uv run tms-mcp reset-db -y                   # vide la base de l'agent
env -u PYTHONPATH uv run pytest              # tests (PostgreSQL de .env, un schéma jetable par test)
```

Configuration (variables d'environnement ou `mcp_server/.env`, voir `.env.example`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `TMS_API_URL` | `http://127.0.0.1:8000/v1` | API du TMS |
| `TMS_TIMEOUT_S`, `TMS_MAX_RETRIES` | `10`, `3` | Timeout et réessais (réseau, 429, 5xx) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | `tms_agent_db`, `postgres`, —, `localhost`, `5432` | Base de l'agent (PostgreSQL) |
| `AGENT_DB_SCHEMA` | `agent` | Schéma des alertes, notifications, journal et appels (créé au démarrage) |
| `DRIVER_CALL_MIN_INTERVAL_MIN` | `15` | Délai minimal (temps TMS) entre deux appels au même chauffeur |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8002` | Transport HTTP |

## Catalogue des tools

| Tool | Famille | Rôle |
|---|---|---|
| `get_tms_time` | A | Heure du TMS (accélérée en simulation) : référence pour `now` |
| `get_active_missions` | A | Missions `in_progress` — point d'entrée de la surveillance |
| `list_missions` | A | Missions par statut / véhicule / chauffeur / `updated_since` |
| `get_mission_details` | A | Mission complète avec événements |
| `get_mission_events` | A | Chronologie, filtres `since` et `type` |
| `get_mission_snapshot` | A | Mission + véhicule + écart + ETA + chauffeur en un appel (entrée du moteur de règles), tolérant aux pannes partielles |
| `get_vehicle_details`, `get_vehicle_position`, `get_vehicle_positions` | A | Statut, position courante, historique |
| `get_mission_route` | A | Itinéraire planifié (géométrie sur demande) |
| `get_mission_deviation`, `get_mission_eta` | A | Écart et ETA calculés par le TMS |
| `get_driver_details`, `list_users` | A | Chauffeur, utilisateurs par rôle / niveau |
| `create_alert` | B | Émission d'alerte avec **déduplication et cooldown atomiques** |
| `list_alerts`, `get_alert` | B | Alertes, détail avec notifications et analyse |
| `resolve_alert`, `annotate_alert` | B | Clôture motivée, analyse de l'agent |
| `notify_recipients` | B | Résolution des rôles via le TMS puis envoi par canal |
| `list_alert_notifications` | B | Qui a été prévenu, quand, comment |
| `call_driver` | B | Appel simulé, **refusé sans alerte ouverte high/critical**, pas de rappel avant 15 min |
| `log_agent_action`, `list_agent_actions` | B | Journal d'audit |
| `get_operations_overview` | B | Tableau de bord : alertes ouvertes et indicateurs par mission déjà interprétés (retard/avance, écart, statut véhicule) |

Ressources : `agent://alerts/open`, `tms://contract`. Prompts : `surveillance_cycle`, `investigate_alert`.

## Brancher un autre TMS

Le serveur ne parle au TMS qu'à travers le protocole `TmsConnector` (`tms_mcp/connector/base.py`), qui reprend le contrat pivot du Document 1. Pour un vrai TMS, écrire un connecteur qui traduit ses ressources vers ce contrat et le passer à `create_server(connector=...)`. Les tools, l'agent et les règles ne changent pas. `tms_mcp/testing.py` fournit un TMS en mémoire qui sert de modèle et de banc de test.

## Notifications

`notify_recipients` résout `role: driver` en chauffeur de la mission, les autres rôles via `GET /roles/{role}/users`, et `user_id` via `GET /users/{id}`. Le canal choisit la coordonnée (`call`/`sms` → téléphone, `email` → e-mail, `in_app` → identifiant). Un destinataire sans coordonnée ou un rôle vide donne une notification `failed` tracée, sans bloquer les autres. Les envois sont simulés (`SimulatedSender`) ; un vrai fournisseur s'ajoute en implémentant `ChannelSender`.

## Structure

```
tms_mcp/
├── server.py          tools, ressources, prompts (create_server)
├── connector/         contrat pivot (base.py) et connecteur REST (rest.py)
├── store.py           base de l'agent : alertes, notifications, journal, appels
├── notifications.py   résolution des destinataires et envoi par canal
├── models.py          schémas de sortie (outputSchema)
├── testing.py         TMS en mémoire
├── config.py
└── cli.py
```
