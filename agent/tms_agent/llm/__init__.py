"""Fournisseurs de LLM : local (API compatible OpenAI, ex. vLLM) ou Anthropic (cloud)."""

from tms_agent.config import AgentSettings
from tms_agent.llm.base import INVALID_ARGUMENTS, LlmClient, LlmError, LlmUnavailable, ModelTurn


def create_llm_client(settings: AgentSettings) -> LlmClient:
    if settings.llm_provider == "anthropic":
        from tms_agent.llm.anthropic_client import ClaudeClient

        return ClaudeClient(settings)
    from tms_agent.llm.openai_compat import OpenAICompatibleClient

    return OpenAICompatibleClient(settings)


__all__ = ["INVALID_ARGUMENTS", "LlmClient", "LlmError", "LlmUnavailable", "ModelTurn", "create_llm_client"]
