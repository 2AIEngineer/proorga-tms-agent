Convention de ports suggérée pour tout votre POC
Pour éviter ce genre de collision à l'avenir, je vous suggère de fixer une convention claire dès maintenant :

 
- Backend TMS:	8000	 
- vLLM (Qwen):	8001	 
- Serveur MCP:	8002	 
- Agent IA:	8003	 
- Frontend: 5173	 
## Environnement Python

Un seul environnement virtuel et un seul `uv.lock` pour l'agent (`agent/`) et le serveur MCP (`mcp_server/`), déclarés comme membres d'un espace de travail uv (`pyproject.toml` à la racine). Les deux restent des paquets distincts.

```bash
uv sync                      # à la racine uniquement : installe les deux paquets dans .venv
uv run tms-agent watch       # `uv run` fonctionne depuis n'importe quel dossier
```

Ne pas lancer `uv sync` depuis `agent/` ou `mcp_server/` : la synchronisation est exacte et retirerait de `.venv` les dépendances de l'autre paquet.

Déploiement : un conteneur par paquet, construit depuis le même `uv.lock` (versions identiques). L'agent joint alors le serveur MCP en HTTP (`AGENT_MCP_TRANSPORT=http`, `AGENT_MCP_URL`).

```bash
uv sync --frozen --no-dev --package tms-mcp-server   # conteneur du serveur MCP
uv sync --frozen --no-dev --package tms-agent        # conteneur de l'agent
```
