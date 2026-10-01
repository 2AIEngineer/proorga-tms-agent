"""Client LLM local via une API compatible OpenAI (vLLM, Ollama, LM Studio, TGI...).

Les données ne quittent pas l'infrastructure : le modèle tourne sur nos serveurs.

- Traduction du format interne (blocs `tool_use` / `tool_result`) vers `chat.completions`
  (`tool_calls`, messages `role: tool`) et retour.
- Budget de contexte : la fenêtre du modèle est lue sur `/v1/models` (`max_model_len` chez vLLM).
  Si la conversation déborde, les plus anciens résultats de tools sont remplacés par une mention
  « retiré » — dans la requête uniquement, l'historique de l'agent n'est pas modifié.
  L'estimation caractères → tokens se **calibre** sur les `prompt_tokens` réellement comptés par
  le serveur ; un dépassement malgré tout est rattrapé (recalibrage, budget réduit, nouvel essai).
- Raisonnement (Qwen3 et apparentés) : activé ou non via `chat_template_kwargs.enable_thinking`,
  selon l'`effort` demandé et la configuration ; il est affiché mais jamais renvoyé au modèle.
- Appel forcé d'un tool (`force_tool`, ex. la remise de l'évaluation) : `tool_choice` nommé,
  garanti par le décodage guidé de vLLM.
- Arguments JSON illisibles : le tool n'est pas exécuté, l'erreur est renvoyée au modèle.
"""

import json
import logging
import re
from typing import Any

import openai

from tms_agent.config import AgentSettings
from tms_agent.llm.base import INVALID_ARGUMENTS, LlmError, LlmUnavailable, ModelTurn

log = logging.getLogger(__name__)

_FINISH = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
_INITIAL_CHARS_PER_TOKEN = 2.5  # prudent (français + JSON), recalibré à chaque réponse
_MIN_CHARS_PER_TOKEN = 1.2
_MAX_CHARS_PER_TOKEN = 4.0  # borne haute réaliste : un `usage` aberrant ne doit pas faire sous-estimer
_CONTEXT_OVERFLOW = re.compile(r"(\d+) input tokens")
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
_TRUNCATED = "[résultat ancien retiré pour tenir dans le contexte du modèle ; relancer le tool si nécessaire]"


def _chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def estimate_tokens(value: Any, chars_per_token: float = _INITIAL_CHARS_PER_TOKEN) -> int:
    return int(_chars(value) / chars_per_token) + 4


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if message["role"] == "system":
            out.append({"role": "system", "content": _text_of(content)})
        elif message["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            # Les résultats de tools d'abord : ils répondent au tour assistant précédent.
            for block in content:
                if block.get("type") == "tool_result":
                    text = _text_of(block.get("content"))
                    if block.get("is_error"):
                        text = f"ERREUR : {text}"
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
            text = _text_of(content)
            if text:
                out.append({"role": "user", "content": text})
        else:  # assistant ; le raisonnement n'est pas renvoyé (recommandation Qwen3)
            blocks = content if isinstance(content, list) else []
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(
                            {k: v for k, v in (b.get("input") or {}).items() if k != INVALID_ARGUMENTS},
                            ensure_ascii=False,
                        ),
                    },
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": _text_of(content) or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
    return out


def fit_context(
    messages: list[dict[str, Any]],
    budget_tokens: int,
    chars_per_token: float = _INITIAL_CHARS_PER_TOKEN,
) -> tuple[list[dict[str, Any]], int]:
    """Retire le contenu des plus anciens résultats de tools jusqu'à tenir dans le budget.

    Le dernier message et les messages non-tool (dont la demande) ne sont jamais touchés.
    Renvoie (messages, nombre de résultats retirés).
    """

    def est(value: Any) -> int:
        return estimate_tokens(value, chars_per_token)

    total = sum(est(m.get("content") or "") + est(m.get("tool_calls") or "") for m in messages)
    if total <= budget_tokens:
        return messages, 0
    fitted = [dict(m) for m in messages]
    removed = 0
    for i, m in enumerate(fitted[:-1]):
        if total <= budget_tokens:
            break
        if m["role"] == "tool" and m["content"] != _TRUNCATED:
            total -= est(m["content"]) - est(_TRUNCATED)
            fitted[i] = {**m, "content": _TRUNCATED}
            removed += 1
    return fitted, removed


def _text_of(blocks: Any) -> str:
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return blocks
    return "\n".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


class OpenAICompatibleClient:
    provider = "local"

    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.model = settings.local_model
        self.base_url = settings.local_base_url
        self.disabled_reason: str | None = None
        self.recoverable = False
        self.context_window: int | None = settings.local_context_window
        self.chars_per_token = _INITIAL_CHARS_PER_TOKEN
        self._client: openai.AsyncOpenAI | None = None
        if not settings.llm_enabled:
            self.disabled_reason = "LLM désactivé par la configuration (AGENT_LLM_ENABLED=false)"
            return
        self._client = openai.AsyncOpenAI(
            base_url=settings.local_base_url,
            api_key=settings.local_api_key or "EMPTY",
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    @property
    def available(self) -> bool:
        return self._client is not None and self.disabled_reason is None

    def _disable(self, reason: str, *, recoverable: bool) -> LlmUnavailable:
        self.disabled_reason, self.recoverable = reason, recoverable
        log.error("LLM local désactivé : %s", reason)
        return LlmUnavailable(reason)

    async def probe(self) -> bool:
        """Vérifie que le serveur répond, que le modèle est servi, et lit sa fenêtre de contexte."""
        if self._client is None:
            return False
        try:
            models = (await self._client.models.list()).data
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            self._disable(f"serveur LLM injoignable sur {self.base_url} : {exc}", recoverable=True)
            return False
        except openai.APIStatusError as exc:
            self._disable(f"serveur LLM en erreur ({exc.status_code}) sur {self.base_url}", recoverable=True)
            return False
        served = {m.id: m for m in models}
        if self.model not in served:
            available = ", ".join(served) or "aucun"
            self._disable(
                f"modèle {self.model!r} non servi par {self.base_url} (disponibles : {available})",
                recoverable=True,
            )
            return False
        if self.context_window is None:
            extra = served[self.model].model_extra or {}
            self.context_window = extra.get("max_model_len") or extra.get("context_length")
        self.disabled_reason, self.recoverable = None, False
        return True

    def _thinking_enabled(self, effort: str) -> bool:
        mode = self.settings.local_thinking
        if mode == "auto":
            return _EFFORT_RANK.get(effort, 1) >= _EFFORT_RANK["high"]
        return mode == "on"

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        effort: str,
        force_tool: str | None = None,
    ) -> ModelTurn:
        if not self.available:
            raise LlmUnavailable(self.disabled_reason or "LLM indisponible")
        full_payload = to_openai_messages(system, messages)
        oa_tools = to_openai_tools(tools)
        max_tokens = self.settings.local_max_tokens
        margin = 0.9
        response = None
        for attempt in range(3):
            payload = full_payload
            if self.context_window:
                tools_tokens = estimate_tokens(oa_tools, self.chars_per_token)
                budget = int((self.context_window - max_tokens) * margin) - tools_tokens
                if budget < 256:
                    raise LlmError(
                        f"contexte du modèle ({self.context_window} tokens) trop petit pour les tools et la réponse"
                    )
                payload, removed = fit_context(full_payload, budget, self.chars_per_token)
                if removed:
                    log.info("%d ancien(s) résultat(s) de tool retiré(s) pour tenir dans le contexte", removed)
            try:
                response = await self._request(payload, oa_tools, max_tokens, effort, force_tool)
                break
            except openai.BadRequestError as exc:
                actual = _CONTEXT_OVERFLOW.search(str(exc.message))
                if not actual or attempt == 2:
                    raise LlmError(f"requête refusée par le serveur LLM : {exc.message}") from exc
                # Dépassement : recalibrage sur le compte réel, marge resserrée, nouvel essai.
                sent = _chars(payload) + _chars(oa_tools)
                ratio = sent / int(actual.group(1))
                self.chars_per_token = max(_MIN_CHARS_PER_TOKEN, min(self.chars_per_token, ratio))
                margin *= 0.85
                max_tokens = max(768, int(max_tokens * 0.75))
                log.warning(
                    "Contexte dépassé (%s tokens) : %.2f caractères/token, nouvel essai",
                    actual.group(1),
                    self.chars_per_token,
                )

        if not response.choices:
            raise LlmError("réponse vide du serveur LLM")
        if response.usage and response.usage.prompt_tokens:
            self._calibrate(_chars(payload) + _chars(oa_tools), response.usage.prompt_tokens)
        return self._to_turn(response)

    def _calibrate(self, sent_chars: int, prompt_tokens: int) -> None:
        """Moyenne glissante du ratio caractères/token mesuré, orientée vers la prudence."""
        measured = 0.95 * sent_chars / max(prompt_tokens, 1)
        updated = 0.7 * self.chars_per_token + 0.3 * measured
        self.chars_per_token = min(_MAX_CHARS_PER_TOKEN, max(_MIN_CHARS_PER_TOKEN, updated))

    async def _request(
        self,
        payload: list[dict[str, Any]],
        oa_tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        force_tool: str | None,
    ):
        # Raisonnement coupé quand l'appel est forcé : la sortie est contrainte par le décodage guidé.
        thinking = self._thinking_enabled(effort) and not force_tool
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "max_tokens": max_tokens,
            "temperature": self.settings.local_temperature,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}},
        }
        if oa_tools:
            kwargs["tools"] = oa_tools
            if force_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            else:
                kwargs["tool_choice"] = "auto"
        try:
            return await self._client.chat.completions.create(**kwargs)
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise LlmError(f"serveur LLM injoignable : {exc}") from exc
        except openai.NotFoundError as exc:
            raise self._disable(f"modèle {self.model!r} introuvable : {exc.message}", recoverable=True) from exc
        except openai.BadRequestError:
            raise  # traité par l'appelant (dépassement de contexte)
        except openai.APIStatusError as exc:
            raise LlmError(f"erreur du serveur LLM {exc.status_code} : {exc.message}") from exc

    @staticmethod
    def _to_turn(response) -> ModelTurn:
        choice = response.choices[0]
        message = choice.message
        extra = message.model_extra or {}
        reasoning = extra.get("reasoning") or extra.get("reasoning_content")
        text = (message.content or "").strip()

        content: list[dict[str, Any]] = []
        if reasoning:
            content.append({"type": "thinking", "thinking": reasoning})
        if text:
            content.append({"type": "text", "text": text})
        for call in message.tool_calls or []:
            raw = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw)
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):  # JSON illisible, ou valeur qui n'est pas un objet
                arguments = {INVALID_ARGUMENTS: raw}
            content.append({"type": "tool_use", "id": call.id, "name": call.function.name, "input": arguments})

        tool_uses = [b for b in content if b["type"] == "tool_use"]
        # Certains serveurs renvoient finish_reason "stop" avec des tool_calls : la présence d'appels prime.
        stop_reason = "tool_use" if tool_uses else _FINISH.get(choice.finish_reason or "stop", "end_turn")
        usage = response.usage.model_dump() if response.usage else {}
        return ModelTurn(
            content=content,
            stop_reason=stop_reason,
            text=text,
            tool_uses=tool_uses,
            thinking=[reasoning] if reasoning else [],
            usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
            refusal="filtrage de contenu" if stop_reason == "refusal" else None,
        )
