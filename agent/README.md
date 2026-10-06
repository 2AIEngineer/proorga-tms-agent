# Agent IA de suivi des missions

Agent de surveillance des missions de transport. Il fait le travail d'un agent de suivi d'exploitation : il lit le TMS **uniquement via le serveur MCP**, applique les **règles métier YAML** (`tms_agent.rules_engine`, règles dans [`domain/rules/`](domain/rules/README.md)), émet les alertes, notifie les bons niveaux de management, puis fait **enquêter un LLM** sur chaque nouvelle alerte pour produire un diagnostic exploitable. Un **assistant opérateur** répond aux questions en langage naturel.

Le LLM est **local par défaut** (vLLM, Qwen3.5-2B en développement) : aucune donnée ne quitte l'infrastructure. Le fournisseur Anthropic (Claude, cloud) reste disponible si la politique de données l'autorise.

## Architecture

```
                          ┌──────────────── Agent (LangGraph) ────────────────────┐
TMS (GET) ◄── MCP ◄───────┤ observe → evaluate → act ─┬→ analyze → report          │
              serveur     │   │          │        │   └──────────→ report          │
              (stdio/HTTP)│   │      rules_engine │          │                     │
                          │   │      (YAML, à chaud)         ▼ (arrière-plan)      │
                          │   └─ get_active_missions,   enquêteur ReAct            │
                          │      get_mission_snapshot   (LLM ⇄ tools MCP)          │
                          │      create_alert, notify_recipients, resolve_alert    │
                          │                                                        │
                          │ assistant : ReAct + mémoire de conversation            │
                          │ LLM : local (API OpenAI-compatible, vLLM) | anthropic  │
                          └────────────────────────────────────────────────────────┘
```

**Boucle de surveillance** (`graph/monitor.py`), un graphe LangGraph par cycle :

| Nœud | Rôle |
|---|---|
| `observe` | Missions en cours + instantané complet de chacune, en parallèle (concurrence bornée) |
| `evaluate` | Règles d'état sur chaque mission, règles d'événement sur chaque **nouvel** événement ; règles rechargées à chaud |
| `act` | `create_alert` (dédup/cooldown côté serveur) → `notify_recipients` selon le bloc `notify` de la règle ; `log_only` / `create_report` journalisés ; clôture automatique des alertes dont la condition a disparu ou dont la mission est terminée |
| `analyze` | Lance l'enquête LLM sur les nouvelles alertes (≥ `medium`) **en arrière-plan** ; l'analyse est jointe à l'alerte à la fin |
| `report` | Bilan du cycle, état persistant |

**Enquêteur** (`graph/investigator.py`) : graphe ReAct (modèle ⇄ tools) qui reçoit l'alerte et les **faits clés** de la mission (calculés de façon déterministe : retard ou avance, vitesse, cap, âge du dernier événement, écart). Il lit le TMS via MCP, explique les règles, peut appeler le chauffeur (garde-fous du serveur), puis remet une évaluation structurée (`submit_assessment`) : synthèse, cause probable, risque, actions recommandées, preuves, confiance.

**Assistant opérateur** (`graph/assistant.py`) : même graphe ReAct, avec checkpointer LangGraph pour la mémoire de conversation.

## Fournisseurs de LLM

| | `local` (défaut) | `anthropic` |
|---|---|---|
| Où tourne le modèle | Chez nous (vLLM, Ollama, LM Studio…) | API Anthropic (cloud) |
| API | Compatible OpenAI (`/v1/chat/completions`) | Messages API (SDK `anthropic`) |
| Modèle par défaut | `Qwen/Qwen3.5-2B` | `claude-opus-5-5` |
| Fenêtre de contexte | Lue sur `/v1/models` (8 192 en dev) | 1M |
| Profil d'outils | Compact (auto sous 32k tokens) | Complet |

Le format interne des conversations est commun ; chaque client traduit vers son API (`llm/openai_compat.py`, `llm/anthropic_client.py`). Passer à un modèle plus grand ne demande qu'un changement de configuration (`AGENT_LOCAL_MODEL`, et `--max-model-len` côté vLLM) : au-delà de 32k tokens de contexte, le profil complet (26 tools, descriptions détaillées) est activé automatiquement.

### Adaptations aux petits modèles locaux

Mesuré sur Qwen3.5-2B (8 192 tokens) :

- **Profil compact** : le catalogue complet occupe ~7 500 tokens et ne laisse aucune place à la conversation. Le profil compact (8 tools pour l'enquête, 11 pour l'assistant, descriptions réduites à leur première phrase, JSON épuré) tient en ~2 700 tokens.
- **Budget de contexte** : les plus anciens résultats de tools sont retirés de la requête quand elle déborde. L'estimation caractères → tokens se calibre sur les `prompt_tokens` réels de vLLM (bornée) ; un dépassement est rattrapé par recalibrage et nouvel essai.
- **Faits pré-calculés** : le modèle reçoit « RETARD de 47 min », « cap 245° (direction, pas une vitesse) » plutôt que des champs bruts à interpréter. La vue d'ensemble MCP donne aussi des indicateurs déjà interprétés par mission.
- **Conclusion garantie** : si le modèle conclut en texte, est coupé (`max_tokens`) ou atteint son dernier tour, l'appel à `submit_assessment` est **forcé** (`tool_choice` nommé, décodage guidé vLLM, sans raisonnement).
- **Validation tolérante** : « élevé » → `high`, « 80 % » → 0,8, actions en texte → liste ; seuls la synthèse et le risque sont indispensables. Une évaluation invalide est renvoyée au modèle pour correction.
- **Raisonnement** (`enable_thinking`) activé pour les enquêtes, coupé pour l'assistant (configurable).

## Matériel et modèle recommandés (vLLM)

### Ce dont l'agent a besoin

- **Appels de tools fiables, en français** : c'est ce qui sépare un modèle utilisable d'un modèle qui « raconte » au lieu d'agir. En dessous de ~8B paramètres, il faut les béquilles décrites plus haut.
- **32k tokens de contexte par requête** : seuil où l'agent passe au profil d'outils complet (26 tools, ~7 500 tokens). Il reste alors ~20k tokens pour une enquête de 8 à 10 lectures et le raisonnement.
- **Plusieurs requêtes simultanées** : 2 enquêtes en parallèle (`AGENT_INVESTIGATION_CONCURRENCY`), plus l'assistant et le rapport. Il faut prévoir 4 à 8 séquences de 32k dans le cache KV de vLLM.

La VRAM se répartit entre les poids du modèle et le cache KV, qui grossit avec le contexte et le nombre de requêtes simultanées. Ordres de grandeur pour un modèle dense de type Qwen3 (cache KV en FP8) :

| Modèle | Poids (FP8) | Cache KV pour 4 × 32k tokens | Total ≈ |
|---|---|---|---|
| 8B | ~9 Go | ~9 Go | ~20 Go |
| 14B | ~15 Go | ~10 Go | ~27 Go |
| 30B-A3B (MoE) | ~31 Go | ~6 Go | ~40 Go |
| 32B | ~33 Go | ~16 Go | ~52 Go |

Ce sont des estimations, à confirmer avec les logs de démarrage de vLLM (`Maximum concurrency for N tokens per request`), qui donnent la capacité réelle.

### Recommandations

| Niveau | GPU | Modèle | Contexte | Usage |
|---|---|---|---|---|
| **Actuel (dev)** | 8 Go | Qwen3.5-2B FP8 | 8k | Tester la chaîne de bout en bout ; analyses approximatives |
| **Minimum pour un bon usage** | **24 Go** (RTX 4090, RTX 3090, L4, A10) | **Qwen3-8B** en FP8 (ou AWQ 4 bits) | 32k | Profil complet, tool calling fiable, 4 à 8 requêtes simultanées |
| **Recommandé (production)** | **48 Go** (L40S, RTX 6000 Ada, A6000) | **Qwen3-30B-A3B** (MoE) en FP8, ou Qwen3-14B | 32k–64k | Meilleure qualité d'enquête ; le MoE n'active que ~3B paramètres par token, donc il reste rapide malgré sa taille |
| Confort / forte charge | 80 Go (A100, H100) | Qwen3-32B ou 30B-A3B en BF16 | 64k–128k | Nombreuses missions et opérateurs simultanés |

Le choix recommandé est **24 Go avec Qwen3-8B** pour un usage réel, puis **48 Go avec Qwen3-30B-A3B** quand la qualité des analyses doit approcher celle d'un agent de suivi expérimenté. Si une version Qwen3.5 de taille équivalente est publiée, elle se substitue directement : vérifier sur Hugging Face son nom exact, sa version quantifiée et le parser de tools indiqué dans sa fiche.

Sur la machine actuelle (8 Go), une étape intermédiaire est possible : **Qwen3-4B en AWQ** avec 16k de contexte et un cache KV en FP8. L'agent restera en profil compact, mais le tool calling sera nettement plus fiable qu'avec le 2B. À valider sur la carte.

### Lancement type (24 Go, Qwen3-8B)

```bash
docker run -d --name vllm-qwen --gpus all -p 8001:8000 --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:v0.30.0 \
  --model Qwen/Qwen3-8B \
  --quantization fp8 \
  --kv-cache-dtype fp8 \
  --max-model-len 32768 \
  --max-num-seqs 8 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --reasoning-parser qwen3
```

Puis côté agent : `AGENT_LOCAL_MODEL=Qwen/Qwen3-8B`. La fenêtre de 32k est lue automatiquement et active le profil complet ; rien d'autre ne change.

Le parser de tools dépend du modèle : `hermes` pour Qwen3, `qwen3_coder` pour les modèles qui l'indiquent (comme le Qwen3.5-2B actuel). Le parser de raisonnement dépend de la version de vLLM. Une erreur de parser se voit tout de suite avec `scripts/run_vllm_toolcall_test.sh` : le tool call arrive en texte au lieu de `tool_calls`. Retirer `--enforce-eager` (présent dans le script de dev) améliore aussi nettement le débit, si la mémoire le permet.

## Robustesse

- **Détection indépendante du LLM** : serveur LLM arrêté, panne, quota → les alertes et notifications continuent selon les règles ; seule l'analyse est sautée. Le LLM est vérifié au démarrage, et **retesté périodiquement** s'il était injoignable.
- **La détection n'attend jamais le LLM** : les enquêtes tournent en arrière-plan (concurrence et file bornées). Une enquête de 60 s sur un modèle local ne retarde pas la détection du cycle suivant.
- **Les alertes naissent des règles** : `create_alert` et `notify_recipients` sont retirés des tools du LLM. `call_driver` est refusé par le serveur sans alerte ouverte high/critical, et limité à un appel par 15 min et par mission.
- **Pas de perte d'événement** : un événement n'est marqué traité qu'une fois ses alertes enregistrées ; sinon il est repris au cycle suivant. L'état survit aux redémarrages (PostgreSQL, schéma `monitor`).
- **Pas de doublon** : déduplication et cooldown appliqués atomiquement par le serveur MCP, en temps TMS (accéléré) ; remise à zéro de l'horloge de simulation gérée.
- **Pas de faux « résolu »** : une alerte d'état n'est close que si l'instantané de la mission est complet et que la règle n'est plus vérifiée.
- **Pannes isolées** : une mission illisible, un tool en erreur ou une enquête ratée n'arrête pas le cycle. TMS ou MCP injoignable → cycle signalé, backoff exponentiel, reconnexion MCP automatique.
- **Règles à chaud** : un fichier modifié est rechargé au cycle suivant ; une règle invalide est signalée et l'ancien jeu reste actif.
- **Boucle LLM bornée** : limite d'appels par enquête, arguments JSON invalides renvoyés au modèle, erreurs de tools renvoyées plutôt que levées, résultats tronqués au-delà d'une taille donnée.

## Démarrage

Prérequis : TMS lancé (`docs/CONTEXT.md`), `uv`, PostgreSQL (base `tms_agent_db`, identifiants dans `agent/.env` et `mcp_server/.env` ; les schémas et tables sont créés au démarrage), vLLM lancé (`scripts/run_vllm_container.sh`, vérification : `scripts/run_vllm_heathy.sh`).

```bash
uv sync                                      # à la racine ai_agent/ : environnement unique (agent + serveur MCP)
cd agent
cp .env.example .env                         # optionnel

uv run tms-agent rules                       # règles actives
uv run rules-engine test domain/rules domain/scenarios  # cas de test métier des règles (voir domain/rules/README.md)
uv run tms-agent tools                       # profil d'outils et tools vus par le LLM
uv run tms-agent watch                       # surveillance continue (lance le serveur MCP en stdio, même environnement)
uv run tms-agent watch --interval 3 -v       # plus fréquent, avec le détail des enquêtes
uv run tms-agent --no-llm watch              # mode déterministe
uv run tms-agent cycle                       # un seul cycle (attend les enquêtes), bilan JSON
uv run tms-agent ask "Quelles missions sont à risque ?"
uv run tms-agent chat                        # conversation (/nouveau, /quitter)
uv run tms-agent report                      # rapport de situation
uv run tms-agent --model Qwen/Qwen3-8B watch # autre modèle servi par vLLM
uv run tms-agent --provider anthropic ask "…" # Claude (si autorisé ; ANTHROPIC_API_KEY)
uv run tms-agent reset-state -y              # oublie les événements déjà traités
env -u PYTHONPATH uv run pytest              # tests (LLM scripté, TMS simulé, PostgreSQL : schémas jetables)
```

Démonstration complète (depuis `ai_agent/`) :

```bash
uv --directory ../tms/server run tms start-missions --reset --speed 30 --skip-minutes 40 \
  -s nominal -s deviation -s immobilization -s incident &
uv run --directory mcp_server tms-mcp reset-db -y && uv run --directory agent tms-agent reset-state -y
uv run --directory agent tms-agent watch --interval 5
```

En quelques minutes : incident critique (4 niveaux notifiés), dérive de la mission 2 (puis clôture quand le camion revient), retard ETA et immobilisation de la mission 3, chacun suivi de son analyse ; clôtures à la fin des missions. Avec Qwen3.5-2B, une enquête prend 20 à 70 s. Le rythme ×30 laisse au modèle le temps de suivre ; à ×60, une enquête couvre une heure simulée.

## Configuration

Variables `AGENT_*` (ou `agent/.env`) — voir `.env.example`. Les principales :

| Variable | Défaut | Rôle |
|---|---|---|
| `POSTGRES_DB` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | `tms_agent_db` / `postgres` / — / `localhost` / `5432` | Base PostgreSQL (sans préfixe `AGENT_`, comme le serveur MCP) |
| `AGENT_STATE_DB_SCHEMA` / `AGENT_ASSISTANT_DB_SCHEMA` | `monitor` / `assistant` | État de la boucle / conversations de l'assistant |
| `AGENT_LLM_PROVIDER` | `local` | `local` (API compatible OpenAI) ou `anthropic` |
| `AGENT_LOCAL_BASE_URL` | `http://localhost:8001/v1` | Serveur vLLM |
| `AGENT_LOCAL_MODEL` | `Qwen/Qwen3.5-2B` | Modèle servi |
| `AGENT_LOCAL_MAX_TOKENS` | `2048` | Taille maximale d'une réponse |
| `AGENT_LOCAL_THINKING` | `auto` | Raisonnement : `auto` (effort ≥ high), `on`, `off` |
| `AGENT_LOCAL_CONTEXT_WINDOW` | lu sur `/v1/models` | Forcer la fenêtre de contexte |
| `AGENT_TOOLSET` | `auto` | `auto`, `full`, `compact` |
| `AGENT_ANTHROPIC_MODEL` | `claude-opus-5-5` | Modèle Claude |
| `AGENT_LLM_EFFORT_INVESTIGATION` / `_CHAT` | `high` / `medium` | Profondeur de raisonnement |
| `AGENT_LLM_MAX_TURNS` | `10` | Appels au modèle par enquête / question |
| `AGENT_BACKGROUND_INVESTIGATIONS` | `true` | Enquêtes en arrière-plan |
| `AGENT_INVESTIGATE_MIN_SEVERITY` | `medium` | Sévérité minimale déclenchant une enquête |
| `AGENT_MCP_TRANSPORT` / `AGENT_MCP_URL` | `stdio` / `http://127.0.0.1:8002/mcp` | Accès au serveur MCP |
| `AGENT_POLL_INTERVAL_S` | `10` | Secondes entre deux cycles |
| `AGENT_AUTO_RESOLVE` | `true` | Clôture automatique des alertes |
| `AGENT_LLM_ENABLED` | `true` | `false` = mode déterministe |

## Structure

```
domain/                   connaissance métier, éditable par l'équipe métier
├── rules/                règles métier YAML (rechargées à chaud)
└── scenarios/            cas de test métier des règles
examples/                 instantané d'exemple, démo du moteur contre le TMS
tms_agent/
├── graph/
│   ├── monitor.py        boucle de surveillance (LangGraph), enquêtes en arrière-plan
│   ├── react.py          graphe ReAct générique (modèle ⇄ tools, relance, conclusion forcée)
│   ├── investigator.py   enquête sur une alerte → évaluation structurée
│   └── assistant.py      assistant opérateur multi-tours
├── llm/
│   ├── base.py           interface commune, format interne des messages
│   ├── openai_compat.py  fournisseur local (vLLM…) : traduction, budget de contexte
│   └── anthropic_client.py fournisseur Anthropic (Claude)
├── mcp_gateway.py        client MCP : stdio / HTTP / mémoire, reconnexion, timeouts
├── tools/                outils exposés au LLM
│   ├── toolbox.py        tools MCP dynamiques + tools locaux, filtrage par mode, exécution
│   ├── profiles.py       tools réservés aux règles, sous-ensembles du profil compact
│   ├── formatting.py     résultats épurés, schémas et descriptions allégés
│   ├── rules.py          tools locaux sur les règles (liste, explication sur une mission)
│   ├── assessment.py     évaluation d'enquête : schémas, validation, submit_assessment
│   └── base.py           LocalTool, ToolInputError
├── rules_engine/         moteur de règles déclaratif (YAML → alertes), voir domain/rules/README.md
│   ├── rulebook.py       jeu de règles actif, rechargement à chaud
│   └── snapshot.py       instantané MCP → contexte d'évaluation, faits clés pour le LLM
├── state.py              événements traités, curseurs (PostgreSQL, schéma monitor)
├── db.py                 pool PostgreSQL par schéma, création idempotente
├── prompts.py            prompts système (figés)
├── config.py
└── cli.py
```
