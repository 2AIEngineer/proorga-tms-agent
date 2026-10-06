# Moteur de règles

Moteur de règles métier **déclaratif** pour la surveillance des missions de transport. Il implémente le Document 3 de `docs/GOAL.md` (projet TMS).

Les règles sont écrites en YAML par (ou avec) l'équipe métier. Le moteur les évalue de façon **déterministe** sur un contexte figé : mission, véhicule, écart, ETA et événements. Il renvoie les règles déclenchées avec :

- les données observées ;
- le message rendu ;
- la clé de déduplication ;
- les destinataires à notifier.

Ce dossier est **autonome** : aucune dépendance au projet TMS, seulement `pydantic` et `pyyaml`. Il est destiné au projet orchestrateur (serveur MCP, agent IA, notifications).

## Place dans l'architecture

```
TMS (API GET) ──► tools MCP ──► Orchestrateur ──► build_context() ──► RuleEngine.evaluate_mission()
                                     ▲                                          │
                                     │                                          ▼
                              base de l'agent ◄── notify / log ◄── apply_deduplication()
```

Le moteur reste **pur** : il ne lit pas le TMS, n'écrit rien et n'envoie aucune notification. Tout cela revient à l'orchestrateur. On peut ainsi :

- tester les règles sans LLM ;
- changer les règles sans redéployer l'agent ;
- expliquer chaque décision.

## Démarrage

```bash
uv sync                                             # à la racine ai_agent/
cd agent
uv run rules-engine validate domain/rules           # valide les règles
uv run rules-engine test domain/rules domain/scenarios  # exécute les scénarios sur données figées
uv run rules-engine eval domain/rules examples/snapshot_deviation.json            # règles déclenchées (JSON)
uv run rules-engine eval domain/rules examples/snapshot_deviation.json --explain  # détail condition par condition
uv run pytest tests/rules_engine
```

Démonstration contre le TMS simulé (lecture seule, polling) :

```bash
# dans server/ : uv run tms serve   puis   uv run tms start-missions --speed 60
uv run python examples/watch_tms.py --api http://127.0.0.1:8000 --interval 2
```

## Utilisation depuis l'orchestrateur

```python
from tms_agent.rules_engine import RuleEngine, build_context, apply_deduplication

engine = RuleEngine.from_path("domain/rules/")          # à recharger quand les fichiers changent

# 1. Lire le TMS (tools MCP) : réponses JSON brutes du contrat pivot
context = build_context(
    now=tms_now,            # heure du TMS (GET /v1/health -> time)
    mission=mission,        # GET /v1/missions/{id} (avec events)
    vehicle=vehicle,        # GET /v1/vehicles/{id}
    deviation=deviation,    # GET /v1/missions/{id}/deviation
    eta=eta,                # GET /v1/missions/{id}/eta
)

# 2. Évaluer : règles d'état + règles d'événement pour chaque événement non encore traité
matches = engine.evaluate_mission(context, tms_now, new_events)

# 3. Déduplication et cooldown, à partir de l'historique d'alertes de la base de l'agent
for decision in apply_deduplication(matches, alert_history, tms_now):
    if decision.emit:
        ...  # créer l'alerte (base de l'agent), notifier match.notify, journaliser
```

`alert_history` implémente le protocole `AlertHistory` :

- `has_open_alert(key) -> bool` ;
- `last_emitted_at(key) -> datetime | None`.

L'orchestrateur l'adosse à sa base. `InMemoryAlertHistory` sert aux tests.

### `RuleMatch` (résultat d'un déclenchement)

| Champ | Contenu |
|---|---|
| `rule_id`, `rule_version`, `severity`, `category`, `action` | Identité de la règle (traçabilité) |
| `mission_id`, `event_id` | Mission concernée, événement déclencheur pour les règles d'événement |
| `title`, `message` | Rendus à partir des gabarits `{{ champ }}` |
| `deduplication_key`, `cooldown_minutes` | Pour `apply_deduplication` |
| `notify` | `[{role \| user_id, management_level, channel, message}]` : rôles à résoudre via le TMS (`/v1/roles/{role}/users`, chauffeur de la mission pour `driver`) |
| `observed_data` | Valeurs des champs des conditions et des champs `observe` (« pourquoi l'agent a alerté ») |
| `trace` | Chaque condition : champ, opérateur, valeur attendue, valeur observée, résultat |

`match.to_dict()` donne un dictionnaire sérialisable en JSON, prêt à stocker.

## Écrire une règle (équipe métier)

Une règle par fichier dans `domain/rules/`. Plusieurs règles par fichier sont aussi possibles, en liste YAML ou séparées par `---`.

```yaml
id: deviation_persistent          # identifiant stable : minuscules, chiffres, _
version: 2                        # à incrémenter à chaque modification
enabled: true                     # désactivable sans suppression
severity: high                    # low | medium | high | critical
category: route_deviation         # route_deviation, delay, immobilization, data_gap, safety...
author: équipe exploitation       # optionnel
description: >
  Alerte si le véhicule s'écarte de plus de 5 km de l'itinéraire pendant plus de 20 minutes.
when:                             # conditions (all = ET, any = OU, not = NON)
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - field: deviation.current_offset_km
      operator: greater_than
      value: 5
    - field: deviation.duration_minutes
      operator: greater_than
      value: 20
observe:                          # optionnel : données à joindre à l'alerte
  - deviation.vehicle_position
then:
  action: emit_alert              # emit_alert | log_only | create_report
  alert:
    title: "Dérive d'itinéraire"
    message: >                    # optionnel (défaut : titre + référence mission)
      Le véhicule {{ vehicle.plate }} de la mission {{ mission.reference }}
      s'est écarté de {{ deviation.current_offset_km }} km depuis {{ deviation.duration_minutes }} minutes.
    deduplication_key: "deviation:{{ mission.id }}"   # défaut : "<id de la règle>:<id mission>"
    cooldown_minutes: 30
  notify:                         # qui prévenir, à quel niveau, par quel canal
    - { role: driver,         management_level: M+0, channel: call }
    - { role: ops_agent,      management_level: M+1, channel: in_app }
    - { role: ops_supervisor, management_level: M+2, channel: sms }
```

Les erreurs sont signalées en clair, avec le fichier, la règle, l'emplacement et la cause. Par exemple : `opérateur inconnu 'plus_grand_que'`, `champ 'eat.delay_minutes' invalide`, ou une faute de frappe sur un nom de clé.

### Champs disponibles

Tous les champs des ressources du TMS sont accessibles : `mission.*`, `vehicle.*`, `deviation.*`, `eta.*`, `driver.*`, et `event.*` pour les règles d'événement. Le moteur ajoute les champs dérivés suivants :

| Champ | Signification |
|---|---|
| `mission.last_event_age_minutes` | Minutes depuis le dernier événement de la mission |
| `mission.last_event_type`, `mission.last_event_at`, `mission.event_count` | Dernier événement, nombre d'événements |
| `vehicle.speed_kmh` | Vitesse courante |
| `vehicle.last_seen_age_minutes` | Minutes depuis la dernière position |
| `deviation.duration_minutes` | Durée de l'écart (calculée depuis `current_offset_since` si absente) |
| `eta.delay_minutes` | Retard (calculé depuis `current_eta` et `planned_delivery_at` si absent) |
| `event.distance_to_target_km` | Pour `arrived_pickup` / `arrived_delivery` : distance au point prévu |
| `event.age_minutes` | Âge de l'événement |

Une règle qui lit un champ `event.*` est **évaluée pour chaque nouvel événement**, par exemple un incident chauffeur ou une arrivée hors zone. Les autres règles sont évaluées une fois par cycle, sur l'état de la mission.

**Un champ absent ou nul rend la condition fausse.** Par exemple, l'écart à l'itinéraire n'est pas calculé avant le départ, donc aucune règle de dérive ne peut se déclencher avant.

### Opérateurs

| Famille | Opérateurs | `value` |
|---|---|---|
| Comparaison | `equals`, `not_equals`, `greater_than`, `greater_or_equal`, `less_than`, `less_or_equal` | valeur ou nombre |
| | `between` | `[min, max]` (bornes incluses) |
| Appartenance | `in`, `not_in` | liste |
| Temporel | `older_than_minutes`, `newer_than_minutes` | minutes (le champ est une date) |
| | `duration_greater_than_minutes` | minutes (le champ est une durée en minutes ou une date de début) |
| Géospatial | `distance_greater_than_km` | `{from: <point {lat, lon} ou champ, ex. mission.destination>, km: 2}` |
| | `inside_polygon`, `outside_polygon` | liste d'au moins 3 points `{lat, lon}` ou `[lat, lon]` |
| Logique | `all`, `any`, `not` | conditions imbriquées |

## Tester une règle sur des données figées

Les fichiers de `domain/scenarios/` décrivent des situations et les règles attendues. Ils sont lisibles par l'équipe métier :

```yaml
name: Règle eta_delay — retard ETA significatif
base: { mission: {...}, vehicle: {...}, deviation: {...}, eta: {...}, events: [...] }
cases:
  - name: Retard de 52 min → alerte
    now: "2025-11-04T10:00:00Z"
    snapshot:                       # ce qui change par rapport à `base`
      eta: { delay_minutes: 52 }
    expect:
      triggered: [eta_delay]        # liste EXACTE des règles déclenchées
```

Pour les règles d'événement, ajouter `new_events: [...]` au cas. `uv run rules-engine test domain/rules domain/scenarios` exécute tous les cas (code de sortie 1 en cas d'échec). C'est aussi le point de départ pour calibrer les seuils après les entretiens avec les agents de suivi.

## Structure

```
agent/
├── domain/
│   ├── rules/             règles du POC (les 6 du Document 3) — ce README
│   └── scenarios/         cas de test métier sur données figées
├── examples/              instantané d'exemple, boucle de surveillance contre le TMS
├── tms_agent/rules_engine/
│   ├── models.py          format des règles (validation stricte)
│   ├── operators.py       opérateurs
│   ├── context.py         contexte depuis les ressources TMS + champs dérivés
│   ├── engine.py          évaluation, rendu des messages, trace
│   ├── dedup.py           déduplication / cooldown (protocole AlertHistory)
│   ├── loader.py          chargement YAML, erreurs lisibles
│   ├── scenarios.py       exécution des scénarios
│   ├── rulebook.py        jeu de règles actif, rechargement à chaud (utilisé par l'agent)
│   ├── snapshot.py        instantané MCP → contexte d'évaluation, faits clés
│   ├── templating.py      gabarits {{ champ }} (sans code exécutable)
│   └── cli.py             rules-engine validate | eval | test
└── tests/rules_engine/
```

> Si un `PYTHONPATH` global (ROS, par exemple) injecte des plugins pytest, lancer `env -u PYTHONPATH uv run pytest`.
