# Stack technique

État au 2 octobre 2026. Ce document décrit ce que le POC utilise aujourd'hui, pourquoi, ce qui lui manque, et ce qu'il faudra ajouter pour un pilote puis une mise en production.

## 1. Vue d'ensemble

```
 TMS simulé (../tms)            Orchestrateur (ce dépôt)                        LLM
┌───────────────────┐   GET   ┌──────────────────┐  MCP   ┌────────────────────┐  HTTP  ┌──────────────┐
│ FastAPI + SQLite  │◄────────┤ Serveur MCP      │◄───────┤ Agent (LangGraph)  ├───────►│ vLLM (Qwen)  │
│ Front React/Vite  │         │ tms_mcp          │ stdio  │ tms_agent          │        │ ou Claude    │
└───────────────────┘         └────────┬─────────┘ / HTTP └─────────┬──────────┘        └──────────────┘
                                       │                            │
                                       ▼                            ▼
                              ┌─────────────────────── PostgreSQL : tms_agent_db ─────────────────────┐
                              │ schéma agent : alertes, notifications, journal, appels                 │
                              │ schéma monitor : état de la boucle   │ schéma assistant : conversations │
                              └────────────────────────────────────────────────────────────────────────┘
```

Ports utilisés en local : TMS `8000`, vLLM `8001`, serveur MCP `8002`, agent `8003` (réservé, pas encore d'API HTTP), front `5173`.

| Composant | Rôle | Déploiement actuel |
|---|---|---|
| `mcp_server/` (`tms_mcp`) | Seul point d'accès au TMS (lecture seule). Expose 25 tools sémantiques, gère la base de l'agent et les notifications. | Processus Python lancé par l'agent (stdio) ou serveur HTTP autonome |
| `agent/` (`tms_agent`) | Boucle de surveillance, moteur de règles, enquêtes LLM, assistant opérateur | CLI Python (`tms-agent watch`, `chat`, …) |
| `agent/tms_agent/rules_engine` | Moteur de règles déclaratif (YAML), déterministe, sans LLM | Module de l'agent |
| `../tms` | TMS simulé : API REST et interface web | Projet séparé, hors de ce dépôt |
| vLLM | Sert le modèle local via une API compatible OpenAI | Conteneur Docker sur GPU (`scripts/run_vllm_container.sh`) |

## 2. Ce qui est utilisé aujourd'hui

### Langage et outillage

| Techno | Version | Usage |
|---|---|---|
| Python | ≥ 3.11 (3.13 dans les venvs) | Tout l'orchestrateur |
| uv | 0.12 | Environnements, dépendances, verrouillage (`uv.lock` par projet), exécution |
| hatchling | — | Build des paquets |
| pytest + pytest-asyncio | 9.1 / 1.4 | 101 tests : 82 agent, 19 serveur MCP |
| ruff | utilisé en local | Lint/format, sans configuration commitée |

### Agent IA

| Techno | Version | Usage |
|---|---|---|
| LangGraph | 1.2 | Graphe de la boucle de surveillance (`observe → evaluate → act → analyze → report`) et graphes ReAct (enquêteur, assistant) |
| langgraph-checkpoint-postgres | 3.1 | Mémoire des conversations de l'assistant (schéma `assistant`) |
| SDK `openai` | 3.22 | Client du LLM local (vLLM, Ollama : toute API compatible OpenAI) |
| SDK `anthropic` | 1.11 | Client Claude (cloud), en option si la politique de données l'autorise |
| Pydantic / pydantic-settings | 2.13 / 2.15 | Validation des règles, des arguments de tools et de la configuration (`.env`, variables d'environnement) |
| PyYAML | 6.0 | Lecture des règles métier |

Choix structurants :
- **Le LLM ne décide pas des alertes.** Les règles YAML détectent et notifient de façon déterministe. Le LLM enquête ensuite, en arrière-plan, et joint une analyse structurée à l'alerte.
- **Le LLM est local par défaut** (Qwen3.5-2B, quantifié FP8, contexte de 8k) : aucune donnée ne sort de l'infrastructure. Un profil d'outils « compact » adapte les tools et les prompts aux petites fenêtres de contexte.
- **Les règles se rechargent à chaud.** Une règle invalide est signalée et l'ancien jeu de règles reste actif.

### Intégration TMS

| Techno | Version | Usage |
|---|---|---|
| MCP (SDK Python `mcp`) | 2.2 | Protocole entre l'agent et le serveur d'outils. Transports stdio et Streamable HTTP. Les tools sont découverts dynamiquement par l'agent. |
| httpx | 0.28 | Connecteur REST vers le TMS : réessais sur erreur réseau, 429 et 5xx ; pagination |
| uvicorn / Starlette | 0.54 / 1.7 | Serveur HTTP du transport MCP (dépendances du SDK) |

Le connecteur TMS est une interface (`TmsConnector`). Brancher un autre TMS revient à écrire une nouvelle implémentation, sans toucher à l'agent.

### Données

| Techno | Version | Usage |
|---|---|---|
| PostgreSQL | 16 | Base `tms_agent_db`, un schéma par responsabilité (`agent`, `monitor`, `assistant`). Les schémas et tables sont créés au démarrage. |
| psycopg 3 + psycopg-pool | 3.3 | Driver et pools de connexions. `JSONB` pour les données observées et les analyses, `TIMESTAMPTZ` pour les dates. La création d'alerte (déduplication et cooldown) est protégée par un verrou consultatif par clé. |

### Inférence

| Techno | Version | Usage |
|---|---|---|
| vLLM (`vllm/vllm-openai`) | 0.30 | Serveur d'inférence, avec parsers d'appels de tools (`qwen3_coder`) et de raisonnement (`qwen3`) |
| Qwen3.5-2B | FP8 | Modèle de développement |
| Docker + GPU NVIDIA | — | Exécution de vLLM |

### TMS simulé (`../tms`, hors de ce dépôt)

FastAPI, SQLAlchemy 2, SQLite, Typer (CLI de simulation des missions). Front : React 19, Vite 8, TypeScript, Tailwind 4, shadcn, TanStack Query, MapLibre GL.

### Notifications

Quatre canaux sont modélisés (`in_app`, `sms`, `email`, `call`), avec résolution des destinataires par rôle et niveau de management. **L'envoi est simulé** (`SimulatedSender`) : chaque notification est enregistrée en base comme délivrée, mais rien ne part réellement.

## 3. Limites actuelles

Le POC est fait pour valider l'architecture, pas pour tourner en production.

| Domaine | Limite |
|---|---|
| Exposition | L'agent n'a pas d'API : il ne s'utilise qu'en CLI. Le serveur MCP en HTTP n'a ni authentification ni TLS. |
| Notifications | Aucun envoi réel (SMS, e-mail, appel). |
| Déploiement | Pas de conteneur pour l'agent ni le serveur MCP, pas de CI, pas d'environnements séparés. |
| Base de données | Pas de migrations versionnées (`CREATE TABLE IF NOT EXISTS` au démarrage), pas de sauvegarde ni de purge. Le serveur MCP appelle la base en synchrone depuis du code asynchrone. |
| Secrets | Identifiants en clair dans des fichiers `.env` (ignorés par git). |
| Observabilité | Logs texte sur stderr uniquement : pas de métriques, de traces ni de suivi des appels LLM (latence, tokens, coût). |
| Haute disponibilité | Une seule instance de la boucle de surveillance. Deux instances traiteraient les mêmes missions : la déduplication en base évite les doublons d'alertes, mais pas le double travail. |
| LLM | Modèle de 2B paramètres avec 8k de contexte : qualité d'enquête limitée. Pas de jeu d'évaluation pour mesurer la qualité des analyses. |
| Sécurité métier | Pas de gestion des utilisateurs ni des droits côté opérateur. Le journal d'audit existe, mais il n'est pas exposé. |
| Règles | Les règles sont des fichiers du dépôt : pas d'interface d'édition, pas de workflow de validation métier. |

## 4. Pour passer au niveau supérieur

### Étape 1 : pilote sur un vrai TMS

Objectif : faire tourner l'agent en continu sur un TMS réel, avec quelques agents de suivi.

| Besoin | Proposition |
|---|---|
| Service agent | API HTTP **FastAPI** sur le port 8003 : santé, alertes, rapports, assistant (streaming SSE). L'agent devient un service, pas une commande. |
| Conteneurs | **Dockerfile** pour l'agent et le serveur MCP, plus un **docker compose** (agent, MCP, PostgreSQL, vLLM). |
| Migrations | **Alembic**, ou des scripts SQL versionnés, à la place des `CREATE TABLE IF NOT EXISTS`. |
| Connecteur réel | Implémentation de `TmsConnector` pour le TMS cible : authentification OAuth2 ou clé d'API, limites de débit, correspondance avec le contrat pivot. |
| Notifications réelles | Implémentations de `ChannelSender` : e-mail (SMTP ou SendGrid), SMS et appels (Twilio, Vonage ou opérateur local), Teams ou Slack pour le canal in-app. Mode « bac à sable » conservé pour les tests. |
| Sécurité MCP | Transport HTTP derrière TLS, avec authentification (jeton ou OAuth, prévus par la spécification MCP). |
| Qualité | **CI** (GitHub Actions) : ruff, tests avec PostgreSQL en service, `rules-engine test`. Configuration ruff et **mypy** commitée. |
| Modèle | Modèle plus grand, si le GPU le permet (Qwen3-8B à 32B, 32k de contexte), avec un **jeu d'évaluation** d'enquêtes annotées pour comparer les modèles. |

### Étape 2 : production

| Besoin | Proposition |
|---|---|
| Observabilité | **OpenTelemetry** (traces de cycle et d'appels de tools), **Prometheus + Grafana** (missions surveillées, alertes émises, latence des cycles, disponibilité du LLM), traces LLM avec **Langfuse** (auto-hébergeable, compatible avec la contrainte de confidentialité). |
| Logs | Logs JSON structurés, centralisés (Loki ou ELK). |
| Secrets | **Vault**, ou le gestionnaire de secrets du cloud ou de Kubernetes. |
| Orchestration | **Kubernetes**, ou Docker Swarm pour une petite équipe, avec vLLM sur un nœud GPU dédié. |
| Montée en charge | Répartition des missions entre instances : verrou par mission en base (`pg_try_advisory_lock`), ou file de travail (**Redis Streams**, RabbitMQ) alimentée par les événements du TMS (webhooks) plutôt que par du polling. |
| Base de données | Sauvegardes (pgBackRest), rétention et purge du journal, passage du serveur MCP en accès asynchrone (`AsyncConnectionPool`), réplica en lecture pour le reporting. |
| Utilisateurs | SSO (**Keycloak**, Entra ID) et rôles alignés sur les niveaux de management. Les opérateurs ne voient que leur périmètre. |
| Interface opérateur | Front (même stack que le front TMS : React, TanStack Query, MapLibre) : tableau des alertes, détail de l'enquête, accusé de réception, chat avec l'assistant. |
| Gestion des règles | Édition des règles par l'équipe métier (interface ou workflow Git avec revue), versions de règles tracées dans les alertes (déjà présent : `rule_version`), rejeu des scénarios avant activation. |
| Conformité | Durée de conservation des données chauffeurs (RGPD), journal d'audit consultable, politique d'usage du LLM cloud. |

### Ce qu'il ne faut pas changer

- **L'accès au TMS uniquement par MCP.** C'est ce qui rend l'agent indépendant du TMS.
- **Les règles déterministes pour décider, le LLM pour expliquer.** C'est ce qui rend le système auditable.
- **Le LLM local par défaut.** Le cloud reste une option, à activer explicitement.
