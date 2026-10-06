"""Boîte à outils exposée au LLM.

- Tools MCP **découverts dynamiquement** (`list_tools`) : nom, titre, description sémantique et
  schéma d'entrée viennent du serveur. Brancher un autre TMS ne demande aucun code ici.
- Tools locaux de l'agent : explication des règles métier, remise de l'évaluation finale.
- Chaque mode (enquête, assistant) filtre les tools autorisés : l'émission d'alertes et les
  notifications restent pilotées par les règles, jamais improvisées par le LLM.
- Profil **compact** pour les petits modèles locaux (fenêtre de 8k tokens) : sous-ensemble de
  tools par mode, descriptions réduites à leur première phrase, JSON des résultats épuré.
"""

from tms_agent.tools.assessment import (
    ASSESSMENT_SCHEMA,
    COMPACT_ASSESSMENT_SCHEMA,
    SUBMIT_ASSESSMENT,
    Assessment,
    submit_assessment_tool,
    validate_assessment,
)
from tms_agent.tools.base import LocalTool, ToolInputError
from tms_agent.tools.formatting import prune
from tms_agent.tools.profiles import (
    COMPACT_ASSISTANT_TOOLS,
    COMPACT_INVESTIGATION_TOOLS,
    RULE_DRIVEN_TOOLS,
    use_compact_toolset,
)
from tms_agent.tools.rules import rule_tools
from tms_agent.tools.toolbox import Toolbox, build_toolbox

__all__ = [
    "ASSESSMENT_SCHEMA",
    "COMPACT_ASSESSMENT_SCHEMA",
    "COMPACT_ASSISTANT_TOOLS",
    "COMPACT_INVESTIGATION_TOOLS",
    "RULE_DRIVEN_TOOLS",
    "SUBMIT_ASSESSMENT",
    "Assessment",
    "LocalTool",
    "ToolInputError",
    "Toolbox",
    "build_toolbox",
    "prune",
    "rule_tools",
    "submit_assessment_tool",
    "use_compact_toolset",
    "validate_assessment",
]
