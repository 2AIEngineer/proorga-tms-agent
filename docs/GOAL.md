 
---

# Architecture et contraintes d'intégration

Le POC est découpé en **deux projets indépendants** :

```
┌──────────────────────────────┐         ┌──────────────────────────────────────────────┐
│  Projet TMS (ce dépôt)       │         │  Projet Orchestrateur (à venir)              │
│                              │  HTTP   │                                              │
│  API REST en lecture seule ◄─┼─────────┼── Serveur MCP ◄── Agent IA                   │
│  (GET uniquement)            │  pull   │                    │                         │
│                              │         │                    ├── Moteur de règles      │
│  Simulateur de missions      │         │                    ├── Notifications         │
│  (systèmes internes du TMS)  │         │                    └── Base de l'agent       │
│                              │         │                        (alertes, notifications,│
│  Base du TMS                 │         │                         journal, appels...)   │
└──────────────────────────────┘         └──────────────────────────────────────────────┘
```

**Le TMS est un système que nous ne contrôlons pas** : nous l'intégrons pour consommer ses ressources. Quatre contraintes en découlent :

1. **Lecture seule** : l'agent IA n'effectue aucune mutation sur l'API du TMS. L'API n'expose que des `GET`.
2. **Pull uniquement** : le TMS ne pousse rien vers l'agent (ni webhook, ni événement). C'est l'agent qui interroge l'API à son rythme (polling avec `updated_since` / `since`).
3. **Toujours via le serveur MCP** : l'agent n'appelle jamais l'API du TMS directement. Il passe par les tools du serveur MCP.
4. **Les données produites par l'agent restent chez l'agent** : alertes, notifications, journal des actions et appels chauffeur sont stockés dans la base du projet orchestrateur, jamais dans le TMS.

Le **projet orchestrateur** regroupe le serveur MCP, l'agent IA, le moteur de règles, la gestion des alertes et l'envoi des notifications.

---

# État du projet TMS et commandes de lancement

**Le serveur TMS (API) et le client TMS (visualisation) sont prêts.** Le projet orchestrateur peut s'en servir comme TMS de test : lancer l'API, démarrer des missions simulées, puis suivre visuellement leur déroulement pendant que l'agent les surveille.

Emplacement du projet TMS (chemin absolu) :

```bash
TMS_DIR=/home/salifou/Desktop/Dev/ProOrga/tms_agent/tms
```

Toutes les commandes ci-dessous utilisent ce chemin absolu. Elles fonctionnent donc depuis n'importe quel dossier, notamment depuis le projet orchestrateur.

| Composant | URL | Rôle |
|---|---|---|
| API TMS | `http://127.0.0.1:8000/v1` (doc : `/v1/docs`, spec : `/v1/openapi.json`) | Ressources en lecture seule, consommées par le serveur MCP |
| Client TMS | `http://localhost:5173` | Visualisation des missions en temps réel (carte, dérives, ETA, événements) |

## 1. Lancer l'API TMS

```bash
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms serve --port 8000
```

Vérifier qu'elle répond : `curl -s http://127.0.0.1:8000/v1/health`.

## 2. Démarrer les missions simulées (relançable à volonté)

```bash
# 3 missions (nominale, dérive, immobilisation), temps accéléré ×60, base de missions remise à zéro
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms start-missions --reset --speed 60
```

Variantes utiles pour tester l'agent :

```bash
# Ajouter le scénario « incident critique » (message chauffeur critical) aux 3 scénarios par défaut
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms start-missions --reset --speed 60 \
  -s nominal -s deviation -s immobilization -s incident

# Arriver directement sur les anomalies (les 60 premières minutes sont jouées instantanément)
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms start-missions --reset --speed 60 --skip-minutes 60

# Rejouer toute la simulation instantanément (historique complet, sans temps réel)
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms start-missions --reset --fast-forward

# Lister les scénarios / vider les missions
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms scenarios
uv --directory /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/server run tms reset -y
```

Repères temporels des anomalies (en temps simulé depuis le démarrage, T0) :

| Moment | Ce qui se passe |
|---|---|
| T+45 à T+75 min | Mission 2 : dérive de 6 à 8 km |
| T+55 min | Incident critique (si le scénario `incident` est lancé) |
| À partir de T+60 min | Mission 3 : véhicule `stopped`, plus aucun événement pendant 3 h |

Au rythme ×60, une heure simulée passe en une minute réelle. L'heure courante du TMS, à utiliser comme `now` par l'agent et le moteur de règles, est donnée par `GET /v1/health` → `time`. Ctrl+C (ou `kill`) arrête la simulation et annule les missions non terminées.

## 3. Lancer le client TMS

```bash
npm --prefix /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/client install    # première fois uniquement
npm --prefix /home/salifou/Desktop/Dev/ProOrga/tms_agent/tms/client run dev    # http://localhost:5173
```

Le client attend l'API sur `http://127.0.0.1:8000`. Pour une autre adresse, préfixer la commande avec `TMS_API_URL=http://127.0.0.1:<port>`.

## 4. Tout lancer en arrière-plan, puis tout arrêter

```bash
TMS_DIR=/home/salifou/Desktop/Dev/ProOrga/tms_agent/tms
uv --directory $TMS_DIR/server run tms serve --port 8000 > /tmp/tms-api.log 2>&1 &
npm --prefix $TMS_DIR/client run dev > /tmp/tms-client.log 2>&1 &
until curl -sf http://127.0.0.1:8000/v1/health > /dev/null; do sleep 0.5; done
uv --directory $TMS_DIR/server run tms start-missions --reset --speed 60 > /tmp/tms-missions.log 2>&1 &

# Arrêt : simulation d'abord (Ctrl+C simulé, annule les missions en cours), puis API et client via leur port
pkill -INT -f "[t]ms start-missions"
kill $(lsof -t -iTCP:8000 -sTCP:LISTEN) $(lsof -t -iTCP:5173 -sTCP:LISTEN)
```

Notes :

- Le motif `[t]ms start-missions` (avec crochets) évite que `pkill -f` ne tue le shell qui exécute la commande. Sans crochets, le motif apparaît dans la ligne de commande de ce shell, qui serait alors tué lui aussi.

- Si les ports 8000 ou 5173 sont déjà occupés, l'API et le client tournent peut-être déjà : vérifier avec `curl -s http://127.0.0.1:8000/v1/health` avant de relancer.
- Base de données du TMS : `server/data/tms.db`. Pour une base isolée, préfixer les commandes `serve` et `start-missions` avec `TMS_DATABASE_URL=sqlite:////chemin/absolu/test.db`.
- Tests du serveur : `uv --directory $TMS_DIR/server run pytest`. Si un `PYTHONPATH` global (ROS) injecte des plugins pytest, préfixer la commande avec `env -u PYTHONPATH`.
- Documentation détaillée : `server/README.md` et `client/README.md`.

---

# Document 1 — Schéma d'API du faux TMS

## Objectif

Définir une API REST qui simule un TMS réel, avec un contrat stable que la couche MCP pourra traduire en tools sémantiques. Le faux TMS doit rester **agnostique du domaine métier transport** dans sa structure, mais exposer les ressources du cycle de vie décrit par votre responsable.

## Principes de conception

- **REST + JSON** : lisible, testable avec curl/Postman, standard.
- **Lecture seule** : uniquement des `GET`. Les données sont produites par les systèmes internes du TMS (télématique, application chauffeur), représentés par le simulateur.
- **Pull uniquement** : pas de webhook ni de push. Les filtres `updated_since` et `since` permettent un polling efficace.
- **Ressources nommées au pluriel**, hiérarchie claire.
- **Timestamps ISO 8601 en UTC** partout.
- **Pagination** par curseur sur les listes pour rester réaliste.
- **Erreurs normalisées** (RFC 7807 problem+json).
- **Aucune logique métier dans l'API** : elle expose des faits, pas des décisions.

## Modèle de données pivot

```
Mission
 ├── id
 ├── reference          (référence métier lisible)
 ├── vehicle_id
 ├── driver_id
 ├── origin             { label, lat, lon }
 ├── destination        { label, lat, lon }
 ├── planned_route_id
 ├── status             (planned | in_progress | completed | cancelled)
 ├── planned_pickup_at
 ├── planned_delivery_at
 ├── actual_pickup_at
 ├── actual_delivery_at
 ├── current_eta
 ├── created_at
 ├── updated_at         (sert au polling via updated_since)
 └── events[]           (voir MissionEvent, uniquement dans le détail)

MissionEvent
 ├── id
 ├── mission_id
 ├── type               (arrived_pickup | loading_started | loading_completed |
 │                       departed_pickup | arrived_delivery | unloading_started |
 │                       unloading_completed | driver_message | deviation_detected)
 ├── occurred_at
 ├── position           { lat, lon }
 ├── payload            (objet libre selon type)
 └── source             (driver_app | tms)

Vehicle
 ├── id
 ├── plate
 ├── fleet_id
 ├── current_position   { lat, lon, heading, speed_kmh, recorded_at }
 ├── status             (idle | moving | stopped | offline)
 ├── last_seen_at
 └── current_mission_id

Route
 ├── id
 ├── mission_id
 ├── waypoints[]        { lat, lon, sequence }
 ├── polyline           (encodée, format Google)
 ├── distance_km
 └── duration_min

Deviation (calculé par le TMS)
 ├── mission_id
 ├── current_offset_km     (null tant que le véhicule n'a pas quitté le point de chargement)
 ├── current_offset_since
 ├── duration_minutes
 ├── severity              (low | medium | high)
 ├── vehicle_position      { lat, lon }
 └── computed_at

Eta (calculé par le TMS)
 ├── mission_id
 ├── planned_delivery_at
 ├── current_eta
 ├── delay_minutes
 ├── remaining_distance_km
 └── computed_at

User
 ├── id
 ├── name
 ├── role               (driver | ops_agent | ops_supervisor | ops_manager | director | ...)
 ├── management_level   (M+0 | M+1 | M+2 | M+3 | M+4)
 ├── phone
 └── email
```

Les objets `Alert` et `Notification` ne font **pas** partie du TMS : ils sont produits et stockés par le projet orchestrateur (voir Document 3).

## Endpoints

Tous les endpoints sont en **lecture seule** (`GET`). Toute autre méthode renvoie `405`.

### Missions

| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/missions` | Liste paginée, filtres `status`, `vehicle_id`, `driver_id`, `updated_since` |
| `GET` | `/v1/missions/{id}` | Détail d'une mission, avec ses événements |
| `GET` | `/v1/missions/{id}/events` | Liste des événements, filtres `type`, `since` |
| `GET` | `/v1/missions/{id}/route` | Itinéraire planifié |
| `GET` | `/v1/missions/{id}/deviation` | Écart courant vs itinéraire planifié |
| `GET` | `/v1/missions/{id}/eta` | ETA recalculée |

### Véhicules

| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/vehicles` | Liste, filtres `status`, `fleet_id` |
| `GET` | `/v1/vehicles/{id}` | Détail |
| `GET` | `/v1/vehicles/{id}/position` | Dernière position connue |
| `GET` | `/v1/vehicles/{id}/positions` | Historique, filtres `since`, `until`, `mission_id` |

### Chauffeurs et utilisateurs

| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/drivers/{id}` | Détail chauffeur (nom, téléphone, statut) |
| `GET` | `/v1/users` | Liste des utilisateurs, filtres `role`, `management_level` |
| `GET` | `/v1/users/{id}` | Détail d'un utilisateur |
| `GET` | `/v1/roles/{role}/users` | Utilisateurs d'un rôle donné (sert à l'orchestrateur pour résoudre les destinataires d'une notification) |

### Meta

| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/v1/health` | Statut du service |
| `GET` | `/v1/openapi.json` | Spécification OpenAPI 3.1 |

## Exemples de payloads

### `GET /v1/missions?status=in_progress`

```json
{
  "data": [
    {
      "id": "msn_8f3a",
      "reference": "MIS-2025-00412",
      "vehicle_id": "veh_201",
      "driver_id": "drv_77",
      "origin": { "label": "Entrepôt Casablanca", "lat": 33.5731, "lon": -7.5898 },
      "destination": { "label": "Client Rabat", "lat": 34.0209, "lon": -6.8416 },
      "planned_route_id": "rte_551",
      "status": "in_progress",
      "planned_pickup_at": "2025-11-04T08:00:00Z",
      "planned_delivery_at": "2025-11-04T12:30:00Z",
      "actual_pickup_at": "2025-11-04T08:12:00Z",
      "current_eta": "2025-11-04T12:47:00Z"
    }
  ],
  "pagination": { "cursor": null, "has_more": false }
}
```

### `GET /v1/missions/msn_8f3a/events?since=2025-11-04T07:00:00Z`

```json
{
  "data": [
    {
      "id": "evt_001",
      "mission_id": "msn_8f3a",
      "type": "arrived_pickup",
      "occurred_at": "2025-11-04T08:12:00Z",
      "position": { "lat": 33.5733, "lon": -7.5901 },
      "payload": {},
      "source": "driver_app"
    },
    {
      "id": "evt_002",
      "mission_id": "msn_8f3a",
      "type": "loading_completed",
      "occurred_at": "2025-11-04T08:54:00Z",
      "position": { "lat": 33.5733, "lon": -7.5901 },
      "payload": { "weight_kg": 12400 },
      "source": "driver_app"
    }
  ]
}
```

### `GET /v1/missions/msn_8f3a/deviation`
```json
{
  "mission_id": "msn_8f3a",
  "current_offset_km": 6.4,
  "current_offset_since": "2025-11-04T09:15:00Z",
  "duration_minutes": 27.0,
  "severity": "medium",
  "vehicle_position": { "lat": 33.61, "lon": -7.52 },
  "computed_at": "2025-11-04T09:42:00Z"
}
```

### `GET /v1/missions/msn_8f3a/eta`
```json
{
  "mission_id": "msn_8f3a",
  "planned_delivery_at": "2025-11-04T12:30:00Z",
  "current_eta": "2025-11-04T12:47:00Z",
  "delay_minutes": 17.0,
  "remaining_distance_km": 54.2,
  "computed_at": "2025-11-04T09:42:00Z"
}
```

### `GET /v1/users?role=ops_supervisor`

```json
{
  "data": [
    {
      "id": "usr_sup_04",
      "name": "Yassine B.",
      "role": "ops_supervisor",
      "management_level": "M+2",
      "phone": "+212600000004",
      "email": "yassine.b@example.com"
    }
  ]
}
```

## Simulation du flux temps réel

Les données du TMS sont produites par ses **systèmes internes** (boîtiers télématiques, application chauffeur, back-office). Dans le POC, ils sont représentés par un **simulateur** qui écrit directement dans la base du TMS, sans passer par l'API. On le lance avec `uv run tms start-missions` (voir `server/README.md`), autant de fois que voulu. Il produit :

1. **Création de missions** au démarrage (3 missions : nominale, dérive, immobilisation ; un scénario `incident` optionnel couvre la règle 6).
2. **Mises à jour de position** toutes les 30 secondes (lat/lon qui évoluent le long de l'itinéraire).
3. **Événements de statut** aux moments clés (arrivée au chargement, fin du chargement, etc.).
4. **Injection d'anomalies** :
   - Mission 2 : à T+45 min, décalage progressif de la position de 6 à 8 km pendant 30 min.
   - Mission 3 : à T+1 h, gel des événements pendant 3 h, avec véhicule `stopped`.
5. **Utilisateurs de test** : chauffeurs, un agent de suivi, un superviseur, un manager et un directeur, chacun avec un rôle et un niveau de management.

Le temps peut être accéléré (`--speed 60`). L'API et le simulateur partagent la même horloge, ce qui garde cohérentes les durées exposées (âge du dernier événement, durée de dérive, ETA).

## Contrat de stabilité

Ce schéma est **le contrat pivot**, en lecture seule : le connecteur MCP ne fait que lire le TMS, quel qu'il soit. Quand vous passerez à un vrai TMS :
- Soit il expose déjà ces ressources et le connecteur MCP fait un mapping direct.
- Soit il expose des ressources différentes et le connecteur MCP fait une **traduction** vers ce schéma.

Dans les deux cas, **l'agent et le moteur de règles ne changent pas**. C'est ce que le POC doit démontrer.

---

# Document 2 — Structure des descriptions sémantiques des tools MCP

## Objectif

Définir comment chaque ressource du TMS est exposée à l'agent sous forme de **tool** avec une description compréhensible par un LLM. C'est ce qui permet à l'agent de comprendre un TMS qu'il n'a jamais vu.

Le serveur MCP fait partie du **projet orchestrateur** (avec l'agent IA et le moteur de règles), pas du TMS. Il est le seul point d'accès de l'agent au TMS.

## Rappel MCP en une phrase

Le Model Context Protocol standardise la façon dont un agent LLM découvre et appelle des outils externes. Un **serveur MCP** expose des `tools`, `resources` et `prompts`. L'agent (client MCP) liste les tools disponibles et décide lesquels appeler selon la description.

## Principe directeur

Un tool MCP doit répondre à trois questions pour un LLM :

1. **Que fait-il ?** (description en langage naturel)
2. **Quand l'utiliser ?** (contexte d'usage, cas typiques)
3. **Quels paramètres et quel retour ?** (schéma JSON)

La description ne doit **pas** être une paraphrase du nom. Elle doit expliquer **l'intention métier**.

## Anatomie d'un tool MCP

```json
{
  "name": "get_active_missions",
  "title": "Lister les missions en cours",
  "description": "Retourne la liste des missions actuellement en cours d'exécution (statut in_progress). Utiliser ce tool pour obtenir la vue d'ensemble des transports à surveiller. Ne pas utiliser pour les missions planifiées ou terminées.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "vehicle_id": {
        "type": "string",
        "description": "Filtre optionnel sur un véhicule précis."
      },
      "updated_since": {
        "type": "string",
        "format": "date-time",
        "description": "Ne retourne que les missions mises à jour après cette date ISO 8601."
      }
    },
    "required": []
  },
  "outputSchema": {
    "type": "object",
    "properties": {
      "missions": {
        "type": "array",
        "items": { "$ref": "#/definitions/Mission" }
      }
    }
  },
  "annotations": {
    "domain": "transport",
    "category": "observation",
    "read_only": true,
    "latency_hint": "fast",
    "call_frequency_hint": "high"
  }
}
```

## Catalogue des tools pour le POC

Les tools se répartissent en **deux familles** :

| Famille | Adossée à | Nature | Tools |
|---|---|---|---|
| **A. Tools TMS** | API du TMS (`GET` uniquement) | Observation, lecture seule | 1 à 7, 13 |
| **B. Tools de l'orchestrateur** | Base de l'agent et canaux de notification | Action, journalisation, audit | 8 à 12 |

Aucun tool ne modifie le TMS. Les tools de la famille B écrivent uniquement dans la base de l'agent. Pour obtenir des données du TMS (contacts, utilisateurs d'un rôle), ils passent eux aussi par l'API du TMS en lecture.

Correspondance des tools TMS avec l'API :

| Tool | Endpoint TMS |
|---|---|
| `get_active_missions` | `GET /v1/missions?status=in_progress` |
| `get_mission_details` | `GET /v1/missions/{id}` |
| `get_mission_events` | `GET /v1/missions/{id}/events` |
| `get_vehicle_position` | `GET /v1/vehicles/{id}/position` |
| `get_mission_route` | `GET /v1/missions/{id}/route` |
| `get_mission_deviation` | `GET /v1/missions/{id}/deviation` |
| `get_mission_eta` | `GET /v1/missions/{id}/eta` |
| `get_driver_details` | `GET /v1/drivers/{id}` |

### A. Tools TMS (lecture seule)

### 1. `get_active_missions`

**Description** : "Retourne toutes les missions actuellement en cours d'exécution. Utiliser ce tool pour obtenir la vue d'ensemble des transports à surveiller avant d'effectuer une analyse plus fine. C'est le point d'entrée recommandé pour toute boucle de surveillance."

**Quand l'utiliser** : au début de chaque cycle d'observation.

**Paramètres** : `vehicle_id` (optionnel), `updated_since` (optionnel).

**Retour** : liste de missions avec statut, ETA, positions d'origine/destination.

### 2. `get_mission_details`

**Description** : "Retourne les détails complets d'une mission : origine, destination, véhicule, chauffeur, ETA planifiée et ETA recalculée. Utiliser ce tool lorsqu'on a besoin du contexte complet d'une mission identifiée, par exemple pour évaluer un retard."

**Quand l'utiliser** : après `get_active_missions`, pour zoomer sur une mission.

**Paramètres** : `mission_id` (requis).

**Retour** : objet Mission complet.

### 3. `get_mission_events`

**Description** : "Retourne la chronologie des événements d'une mission : arrivée au point de chargement, chargement terminé, départ, arrivée au déchargement, déchargement terminé, etc. Utiliser ce tool pour reconstituer l'historique d'une mission et détecter une immobilisation anormale ou une absence de mise à jour."

**Quand l'utiliser** : pour analyser la progression d'une mission ou détecter un blocage.

**Paramètres** : `mission_id` (requis), `since` (optionnel), `type` (optionnel).

**Retour** : liste d'événements triés par timestamp.

### 4. `get_vehicle_position`

**Description** : "Retourne la dernière position GPS connue d'un véhicule, avec cap et vitesse. Utiliser ce tool pour localiser un camion en temps réel avant de comparer sa position à l'itinéraire planifié."

**Quand l'utiliser** : pour la détection de dérive ou la vérification d'arrivée.

**Paramètres** : `vehicle_id` (requis).

**Retour** : position, cap, vitesse, timestamp de dernière mise à jour.

### 5. `get_mission_route`

**Description** : "Retourne l'itinéraire planifié d'une mission sous forme de waypoints et polyline. Utiliser ce tool pour disposer de la référence géographique à laquelle comparer la position actuelle du véhicule."

**Quand l'utiliser** : avant tout calcul d'écart.

**Paramètres** : `mission_id` (requis).

**Retour** : waypoints, polyline, distance, durée planifiée.

### 6. `get_mission_deviation`

**Description** : "Retourne l'écart courant entre la position du véhicule et l'itinéraire planifié, ainsi que la durée depuis laquelle cet écart persiste et sa sévérité (low/medium/high). Utiliser ce tool en priorité pour détecter une dérive, plutôt que de recalculer l'écart manuellement à partir de `get_vehicle_position` et `get_mission_route`."

**Quand l'utiliser** : pour la surveillance de dérive.

**Paramètres** : `mission_id` (requis).

**Retour** : offset en km, depuis quand, sévérité.

### 7. `get_mission_eta`

**Description** : "Retourne l'ETA recalculée d'une mission en fonction de la position actuelle et des conditions de trafic connues. Utiliser ce tool pour évaluer un retard par rapport à l'ETA planifiée."

**Quand l'utiliser** : pour la détection de retard.

**Paramètres** : `mission_id` (requis).

**Retour** : ETA recalculée, écart vs planifiée en minutes.

### B. Tools de l'orchestrateur (base de l'agent)

### 8. `log_agent_action`

**Description** : "Enregistre une action de l'agent dans son journal d'audit, rattachée à une mission (par exemple, une alerte générée ou un appel chauffeur effectué). Utiliser ce tool pour garder une trace des décisions de l'agent. Le journal est stocké dans la base de l'agent : le TMS n'est jamais modifié."

*(Remplace l'ancien `post_mission_event`, qui écrivait dans le TMS.)*

**Quand l'utiliser** : après une décision d'escalade.

**Paramètres** : `mission_id`, `type`, `payload`.

**Retour** : entrée de journal créée.

### 9. `list_alerts`

**Description** : "Retourne les alertes émises par l'agent, stockées dans sa base. Utiliser ce tool pour éviter de dupliquer une alerte déjà émise, ou pour vérifier l'état d'escalade d'une mission."

**Quand l'utiliser** : avant d'émettre une nouvelle alerte.

**Paramètres** : `mission_id` (optionnel), `severity` (optionnel), `status` (optionnel), `deduplication_key` (optionnel).

**Retour** : liste d'alertes.

### 10. `call_driver` *(tool simulé)*

**Description** : "Simule un appel téléphonique vers le chauffeur d'une mission pour gérer une situation. Le numéro est lu dans le TMS (`get_driver_details`). Dans le POC, l'appel est journalisé dans la base de l'agent et ne déclenche pas de communication réelle. Utiliser ce tool uniquement lorsqu'une règle métier de sévérité élevée s'est déclenchée et que l'escalade téléphonique est justifiée."

**Quand l'utiliser** : escalade de niveau élevé.

**Paramètres** : `mission_id`, `reason`.

**Retour** : statut de l'appel simulé.

### 11. `notify_recipients`

**Description** : "Envoie une notification à une liste de destinataires pour une alerte donnée. Chaque destinataire est identifié par son rôle ou son identifiant utilisateur, avec un niveau de management (M+0, M+1, M+2...) et un canal (in_app, sms, email, call). Utiliser ce tool pour émettre les notifications associées à une alerte, selon ce que prescrit la règle métier. Le tool résout les rôles en utilisateurs concrets en lisant le TMS (`GET /v1/roles/{role}/users` ; le rôle `driver` désigne le chauffeur de la mission), puis enregistre les notifications dans la base de l'agent."

**Quand l'utiliser** : juste après l'émission d'une alerte, avec la liste de destinataires fournie par la règle.

**Paramètres** :
- `alert_id` (requis)
- `recipients` (requis) : liste d'objets `{ role | user_id, management_level, channel }`
- `message` (optionnel, sinon message de l'alerte)

**Retour** : liste des notifications créées avec statut de livraison.

### 12. `list_alert_notifications`

**Description** : "Retourne la liste des notifications émises pour une alerte. Utiliser ce tool pour vérifier qui a été notifié, quand, sur quel canal, et avec quel niveau de management. Utile pour le reporting et l'audit."

**Quand l'utiliser** : reporting, audit, ou vérification qu'une notification a bien été envoyée.

**Paramètres** : `alert_id` (requis).

**Retour** : liste de notifications avec destinataire, canal, niveau, statut de livraison.

### 13. `get_driver_details` *(famille A)*

**Description** : "Retourne la fiche d'un chauffeur depuis le TMS : nom, téléphone, e-mail, statut (`available` / `on_mission`) et mission en cours. Utiliser ce tool pour connaître le contact du chauffeur d'une mission avant une escalade."

**Quand l'utiliser** : avant `call_driver` ou pour contextualiser une alerte.

**Paramètres** : `driver_id` (requis).

**Retour** : fiche chauffeur.

## Convention de nommage

- Préfixe `get_` : lecture.
- Préfixe `log_` : journalisation dans la base de l'agent (jamais dans le TMS).
- Préfixe `list_` : liste filtrée.
- Verbe d'action clair (`call_driver`, `notify_recipients`).
- Jamais d'abréviation obscure (`msn`, `veh` réservés aux IDs, pas aux noms de tools).

## Annotations utiles pour l'agent

Chaque tool peut porter des annotations qui aident le LLM à prioriser :

- `domain` : `transport`, `fleet`, `alerting`.
- `category` : `observation`, `action`, `meta`.
- `read_only` : booléen (toujours `true` pour les tools TMS).
- `backend` : `tms` (API du TMS, lecture seule) ou `agent` (base de l'orchestrateur).
- `latency_hint` : `fast`, `medium`, `slow`.
- `call_frequency_hint` : `high`, `medium`, `low`.
- `cost_hint` : si l'appel consomme du quota ou de l'argent.

## Ce que la description sémantique apporte

Quand vous brancherez un **vrai TMS**, vous écrirez de nouvelles descriptions pour ses ressources. Par exemple, si SAP TM expose un endpoint `GET /tm/missions?status=EXEC`, la description MCP dira : "Retourne les missions en exécution dans SAP TM. Équivalent de `get_active_missions` pour le TMS SAP."

L'agent n'a pas besoin de connaître SAP : il lit la description et comprend que c'est la même intention.

## Anti-patterns à éviter

- **Description = nom** : "Récupère les missions" n'apprend rien au LLM.
- **Description trop vague** : "Utilisé pour les missions" ne dit pas quand.
- **Absence de contexte d'usage** : le LLM doit savoir *quand* appeler, pas juste *ce que* fait le tool.
- **Paramètres non documentés** : chaque paramètre doit avoir une description.
- **Mélange lecture/écriture dans un même tool** : viole le principe de moindre surprise.
- **Tool qui écrit dans le TMS** : le TMS est consommé en lecture seule ; toute écriture va dans la base de l'agent.

---

# Document 3 — Format du moteur de règles

## Objectif

Définir un format de règles métier **découplé de l'agent**, lisible et éditable par l'équipe métier, exécutable par un moteur déterministe, et remplaçable sans toucher au code.

## Principes

- **Déclaratif** : les règles décrivent *quoi* détecter, pas *comment*.
- **Séparé de l'agent** : l'agent lit les règles, il ne les code pas.
- **Lisible par un non-développeur** : YAML, pas de code.
- **Traçable** : chaque règle a un identifiant, une version, un auteur.
- **Testable** : une règle peut être exécutée sur des données figées.
- **Composable** : conditions ET/OU, seuils, fenêtres temporelles.

## Structure d'une règle

```yaml
id: deviation_persistent
version: 1
enabled: true
severity: high
category: route_deviation

description: >
  Détecte un écart persistant entre la position du véhicule et
  l'itinéraire planifié. Déclenche une alerte si l'écart dépasse
  un seuil kilométrique pendant une durée minimale.

when:
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

then:
  action: emit_alert
  alert:
    title: "Dérive d'itinéraire détectée"
    message: >
      Le véhicule {{ vehicle.plate }} de la mission {{ mission.reference }}
      s'est écarté de {{ deviation.current_offset_km }} km depuis
      {{ deviation.duration_minutes }} minutes.
    deduplication_key: "deviation:{{ mission.id }}"
    cooldown_minutes: 30
  notify:
    - role: driver
      management_level: M+0
      channel: call
    - role: ops_agent
      management_level: M+1
      channel: in_app
    - role: ops_supervisor
      management_level: M+2
      channel: sms
```

## Champs obligatoires

| Champ | Rôle |
|---|---|
| `id` | Identifiant unique stable |
| `version` | Version de la règle (incrémentée à chaque modification) |
| `enabled` | Activable/désactivable sans suppression |
| `severity` | `low` / `medium` / `high` / `critical` |
| `category` | Regroupement (route_deviation, delay, immobilization, data_gap, safety) |
| `description` | Explication métier en langage naturel |
| `when` | Conditions de déclenchement |
| `then` | Action à exécuter et destinataires à notifier |

## Opérateurs supportés

**Comparaison** : `equals`, `not_equals`, `greater_than`, `greater_or_equal`, `less_than`, `less_or_equal`, `between`.

**Appartenance** : `in`, `not_in`.

**Temporel** : `older_than_minutes`, `newer_than_minutes`, `duration_greater_than_minutes`.

**Géospatial** : `distance_greater_than_km`, `outside_polygon`, `inside_polygon`.

**Logique** : `all` (ET), `any` (OU), `not`.

## Bloc `notify`

Le bloc `notify` liste les destinataires de l'alerte. Chaque entrée précise :

| Champ | Rôle |
|---|---|
| `role` ou `user_id` | Destinataire cible (l'un ou l'autre) |
| `management_level` | Étiquette M+0, M+1, M+2... (libre, sert au filtrage/affichage) |
| `channel` | `in_app`, `sms`, `email`, `call` |
| `message` | Optionnel, sinon message de l'alerte |

L'orchestrateur résout les rôles en utilisateurs concrets au moment de l'envoi, en **lisant** le TMS : `GET /v1/roles/{role}/users`, et pour le rôle `driver`, le chauffeur de la mission (`GET /v1/drivers/{driver_id}`). Les notifications sont envoyées et enregistrées par l'orchestrateur, jamais par le TMS. Aucune logique d'escalade temporisée ni d'acquittement : la règle dit qui notifier, le moteur notifie immédiatement.

## Exemples de règles stub pour le POC

### Règle 1 — Dérive d'itinéraire persistante

```yaml
id: deviation_persistent
version: 2
enabled: true
severity: high
category: route_deviation
description: >
  Alerte si le véhicule s'écarte de plus de 5 km de l'itinéraire
  planifié pendant plus de 20 minutes. Notifie le chauffeur, l'agent
  de suivi et le superviseur.
when:
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
then:
  action: emit_alert
  alert:
    title: "Dérive d'itinéraire"
    deduplication_key: "deviation:{{ mission.id }}"
    cooldown_minutes: 30
  notify:
    - role: driver
      management_level: M+0
      channel: call
    - role: ops_agent
      management_level: M+1
      channel: in_app
    - role: ops_supervisor
      management_level: M+2
      channel: sms
```

### Règle 2 — Retard ETA significatif

```yaml
id: eta_delay
version: 2
enabled: true
severity: medium
category: delay
description: >
  Alerte si l'ETA recalculée dépasse l'ETA planifiée de plus de
  45 minutes. Notifie l'agent de suivi et le manager.
when:
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - field: eta.delay_minutes
      operator: greater_than
      value: 45
then:
  action: emit_alert
  alert:
    title: "Retard ETA significatif"
    deduplication_key: "eta_delay:{{ mission.id }}"
    cooldown_minutes: 60
  notify:
    - role: ops_agent
      management_level: M+1
      channel: in_app
    - role: ops_manager
      management_level: M+3
      channel: email
```

### Règle 3 — Immobilisation anormale

```yaml
id: immobilization_anomaly
version: 1
enabled: true
severity: high
category: immobilization
description: >
  Alerte si aucun événement de mission n'est enregistré depuis plus
  de 2 heures alors que le véhicule est en statut stopped.
when:
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - field: vehicle.status
      operator: equals
      value: stopped
    - field: mission.last_event_age_minutes
      operator: greater_than
      value: 120
then:
  action: emit_alert
  alert:
    title: "Immobilisation anormale"
    deduplication_key: "immob:{{ mission.id }}"
    cooldown_minutes: 60
  notify:
    - role: driver
      management_level: M+0
      channel: call
    - role: ops_agent
      management_level: M+1
      channel: in_app
```

### Règle 4 — Absence de mise à jour en mouvement

```yaml
id: data_gap_moving
version: 1
enabled: true
severity: medium
category: data_gap
description: >
  Alerte si le véhicule roule mais qu'aucun événement de mission
  n'a été enregistré depuis plus de 3 heures.
when:
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - field: vehicle.status
      operator: equals
      value: moving
    - field: mission.last_event_age_minutes
      operator: greater_than
      value: 180
then:
  action: emit_alert
  alert:
    title: "Absence de mise à jour"
    deduplication_key: "data_gap:{{ mission.id }}"
    cooldown_minutes: 120
  notify:
    - role: ops_agent
      management_level: M+1
      channel: in_app
```

### Règle 5 — Arrivée hors zone

```yaml
id: arrival_out_of_zone
version: 1
enabled: true
severity: medium
category: route_deviation
description: >
  Alerte si le véhicule marque un événement d'arrivée à plus de
  2 km du point prévu.
when:
  all:
    - field: event.type
      operator: in
      value: [arrived_pickup, arrived_delivery]
    - field: event.distance_to_target_km
      operator: greater_than
      value: 2
then:
  action: emit_alert
  alert:
    title: "Arrivée hors zone prévue"
    deduplication_key: "out_of_zone:{{ event.id }}"
    cooldown_minutes: 0
  notify:
    - role: ops_agent
      management_level: M+1
      channel: in_app
    - role: ops_supervisor
      management_level: M+2
      channel: sms
```

### Règle 6 — Incident critique

```yaml
id: critical_incident
version: 1
enabled: true
severity: critical
category: safety
description: >
  Alerte immédiate à tous les niveaux pour tout incident critique
  (accident, incident sécurité) signalé par le chauffeur.
when:
  all:
    - field: event.type
      operator: equals
      value: driver_message
    - field: event.payload.severity
      operator: equals
      value: critical
then:
  action: emit_alert
  alert:
    title: "Incident critique signalé par le chauffeur"
    deduplication_key: "critical:{{ event.id }}"
    cooldown_minutes: 0
  notify:
    - role: ops_agent
      management_level: M+1
      channel: in_app
    - role: ops_supervisor
      management_level: M+2
      channel: sms
    - role: ops_manager
      management_level: M+3
      channel: call
    - role: director
      management_level: M+4
      channel: email
```

## Actions supportées

| Action | Effet |
|---|---|
| `emit_alert` | Génère une alerte dans la base de l'agent et déclenche les notifications du bloc `notify` |
| `log_only` | Journalise sans action (mode observation) |
| `create_report` | Génère un rapport structuré |

## Alertes et notifications (base de l'agent)

Les alertes et les notifications sont des données **produites par l'agent**. Elles sont stockées dans la base du projet orchestrateur et exposées à l'agent par les tools MCP de la famille B (`list_alerts`, `notify_recipients`, `list_alert_notifications`). Le TMS n'en a pas connaissance.

```
Alert
 ├── id
 ├── rule_id
 ├── rule_version
 ├── mission_id          (référence vers la mission du TMS)
 ├── severity            (low | medium | high | critical)
 ├── title
 ├── message
 ├── observed_data       (données lues dans le TMS ayant déclenché la règle)
 ├── deduplication_key
 ├── created_at
 ├── status              (open | resolved)
 └── resolved_at

Notification
 ├── id
 ├── alert_id
 ├── recipient_user_id   (référence vers un utilisateur du TMS)
 ├── recipient_role
 ├── management_level
 ├── channel             (in_app | sms | email | call)
 ├── message
 ├── sent_at
 └── delivered_at

AgentLog                 (journal d'audit, tool log_agent_action)
 ├── id
 ├── mission_id
 ├── type                (alert_emitted | driver_called | notification_sent | ...)
 ├── payload
 └── created_at
```

### Exemple : `notify_recipients`

Requête :

```json
{
  "alert_id": "alr_9c21",
  "recipients": [
    { "role": "driver",         "management_level": "M+0", "channel": "call" },
    { "role": "ops_agent",      "management_level": "M+1", "channel": "in_app" },
    { "role": "ops_supervisor", "management_level": "M+2", "channel": "sms" }
  ],
  "message": "Dérive d'itinéraire détectée sur la mission MIS-2025-00412"
}
```

Réponse (notifications enregistrées dans la base de l'agent) :

```json
{
  "data": [
    { "id": "ntf_001", "alert_id": "alr_9c21", "recipient_user_id": "drv_77",       "recipient_role": "driver",         "management_level": "M+0", "channel": "call",   "sent_at": "2025-11-04T09:42:11Z", "delivered_at": "2025-11-04T09:42:15Z" },
    { "id": "ntf_002", "alert_id": "alr_9c21", "recipient_user_id": "usr_agent_12", "recipient_role": "ops_agent",      "management_level": "M+1", "channel": "in_app", "sent_at": "2025-11-04T09:42:11Z", "delivered_at": "2025-11-04T09:42:12Z" },
    { "id": "ntf_003", "alert_id": "alr_9c21", "recipient_user_id": "usr_sup_04",   "recipient_role": "ops_supervisor", "management_level": "M+2", "channel": "sms",    "sent_at": "2025-11-04T09:42:11Z", "delivered_at": "2025-11-04T09:42:13Z" }
  ]
}
```

## Format d'une alerte générée

```json
{
  "id": "alr_9c21",
  "rule_id": "deviation_persistent",
  "rule_version": 2,
  "mission_id": "msn_8f3a",
  "severity": "high",
  "title": "Dérive d'itinéraire",
  "message": "Le véhicule AB-123-CD de la mission MIS-2025-00412 s'est écarté de 6.4 km depuis 27 minutes.",
  "observed_data": {
    "current_offset_km": 6.4,
    "duration_minutes": 27,
    "vehicle_position": { "lat": 33.61, "lon": -7.52 }
  },
  "notifications": [
    { "recipient_role": "driver",         "management_level": "M+0", "channel": "call" },
    { "recipient_role": "ops_agent",      "management_level": "M+1", "channel": "in_app" },
    { "recipient_role": "ops_supervisor", "management_level": "M+2", "channel": "sms" }
  ],
  "created_at": "2025-11-04T09:42:11Z",
  "status": "open"
}
```

Chaque alerte embarque les **données observées** qui ont déclenché la règle, et la **liste des notifications** émises. C'est ce qui permet à un humain de comprendre pourquoi l'agent a alerté et qui a été prévenu.

## Cycle de vie d'une règle

1. **Rédaction** par l'équipe métier (ou avec elle) dans un fichier YAML.
2. **Validation** par un humain (revue de la règle et de ses seuils).
3. **Déploiement** : chargement par le moteur, versionnement.
4. **Exécution** à chaque cycle de l'agent.
5. **Observation** : mesure du taux de faux positifs.
6. **Ajustement** : nouvelle version, l'ancienne reste traçable.

## Déduplication et cooldown

Deux champs critiques pour éviter le spam. Ils sont gérés par l'orchestrateur, à partir des alertes de sa propre base :

- `deduplication_key` : identifie une alerte de façon unique. Si une alerte avec la même clé existe déjà et est ouverte, la nouvelle n'est pas émise.
- `cooldown_minutes` : délai minimal avant de ré-émettre une alerte avec la même clé, même si la première a été fermée.

## Moteur d'exécution

Le moteur de règles est un composant **séparé de l'agent**. Il reçoit :

- Le **contexte** : mission, vehicle, deviation, eta, events, lus dans le TMS via les tools MCP.
- Les **règles** chargées depuis le stockage.

Et retourne :

- La **liste des règles déclenchées** avec leurs données observées et leurs destinataires.

L'agent appelle ce moteur, puis émet l'alerte et déclenche les notifications via `notify_recipients`. Tout est enregistré dans la base de l'agent. Une boucle de surveillance type :

1. Lire le TMS : `get_active_missions` (avec `updated_since`), puis pour chaque mission `get_mission_deviation`, `get_mission_eta`, `get_mission_events` et `get_vehicle_position`.
2. Évaluer les règles sur ce contexte (moteur de règles).
3. Pour chaque règle déclenchée : vérifier la déduplication et le cooldown (`list_alerts`), créer l'alerte, notifier (`notify_recipients`) et journaliser (`log_agent_action`).

Cette séparation garantit que :

- On peut tester les règles sans LLM.
- On peut changer les règles sans redéployer l'agent.
- On peut auditer pourquoi une alerte a été émise et qui a été notifié.
- On peut brancher un autre TMS sans rien y écrire : seule la lecture est requise.

## Passage aux vraies règles métier (Tache à venir apres le MVP)

Quand vous aurez les audiences avec les agents, le travail sera de :

1. **Traduire** chaque règle implicite en YAML.
2. **Calibrer** les seuils avec eux (5 km ? 10 km ? 20 min ? 1h ?).
3. **Définir** avec eux les destinataires et les canaux par type d'alerte (qui doit être prévenu, à quel niveau, par quel moyen).
4. **Tester** chaque règle sur des cas historiques si disponibles.
5. **Itérer** en observant les faux positifs sur le POC puis en production.

Le format YAML est conçu pour que l'équipe métier puisse **lire, commenter et proposer des modifications** sans passer par un développeur. Le bloc `notify` en particulier est directement lisible : "qui prévenir, à quel niveau, par quel canal". C'est ce qui rendra la transition naturelle après les audiences.

---
 