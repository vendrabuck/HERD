"""OpenAI-compatible chat-completions adapter for the LLMProvider protocol.

Targets the OpenAI chat-completions wire format and works against any
compatible endpoint: OpenAI proper, Azure OpenAI, vLLM, Ollama (with /v1
base), LM Studio, llama.cpp's server, and similar.

Uses the openai>=1.55 AsyncOpenAI SDK rather than hand-rolling the HTTP
calls so that tool_calls schema, ID round-trip, and arguments encoding
follow the canonical reference implementation.

Translation notes:
- Tool results map to one `role=tool` message per tool_call_id (not a
  single user message). is_error has no native channel, so we prepend a
  sentinel to the content string when true.
- Tool-call `arguments` is a JSON-encoded string in the OpenAI wire
  format; we json.dumps on the way out and safe_json_loads on the way in.
- `_sanitize_schema_for_openai` starts as a passthrough; it exists as a
  chokepoint to add narrow rewrites if a specific vLLM or Ollama version
  trips on a JSON-schema construct.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

import httpx
from openai import APIConnectionError, AsyncOpenAI

from app.services.llm_provider import (
    AIError,
    AIProviderUnavailableError,
    ContentBlock,
    Message,
    ProviderResponse,
    StopReason,
    TextBlock,
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)

logger = logging.getLogger(__name__)

# Anthropic's is_error flag has no native OpenAI counterpart. When True we
# prepend this sentinel to the tool-result content so the model sees the
# textual error signal even though the protocol bit is lost.
_TOOL_ERROR_SENTINEL = "[tool error] "


def _build_http_client(*, verify_tls: bool, ca_cert: str | None) -> httpx.AsyncClient | None:
    """Build the httpx client for the OpenAI SDK based on the endpoint's TLS needs.

    Precedence:
      ca_cert set      -> verify against that CA bundle (e.g. a pinned self-signed
                          on-prem cert); verification stays ON and fails closed.
      verify_tls False -> skip verification (self-signed endpoint, no pinned cert).
      otherwise        -> None, letting the SDK use default system-CA verification.

    httpx 0.28 takes an ssl.SSLContext (not a path string) for a custom CA, so we
    build the context from the cafile here.
    """
    if ca_cert:
        ctx = ssl.create_default_context(cafile=ca_cert)
        return httpx.AsyncClient(verify=ctx)
    if not verify_tls:
        return httpx.AsyncClient(verify=False)
    return None


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        verify_tls: bool = True,
        ca_cert: str | None = None,
    ) -> None:
        # AsyncOpenAI rejects blank api_key strings; local servers (vLLM, Ollama,
        # LM Studio) typically ignore auth, so "EMPTY" is the conventional placeholder.
        http_client = _build_http_client(verify_tls=verify_tls, ca_cert=ca_cert)
        self._client = AsyncOpenAI(
            api_key=api_key or "EMPTY", base_url=base_url, http_client=http_client
        )
        self._model = model

    async def call(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        tool_choice: ToolChoice,
        max_tokens: int,
        timeout_s: float | None,
    ) -> ProviderResponse:
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            oai_messages.extend(_message_to_openai(m))

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        if not isinstance(tool_choice, ToolChoiceNone) and tools is not None:
            kwargs["tools"] = [_tool_to_openai(t) for t in tools]
            kwargs["tool_choice"] = _tool_choice_to_openai(tool_choice)

        coro = self._client.chat.completions.create(**kwargs)
        try:
            resp = await (asyncio.wait_for(coro, timeout_s) if timeout_s is not None else coro)
        except asyncio.TimeoutError as exc:
            raise AIError(f"AI call exceeded {timeout_s}s") from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            # APIConnectionError (base of APITimeoutError) covers connection
            # refused, DNS failure, and TLS/CA verification failure; the bare
            # httpx.TransportError catch is belt-and-suspenders for a transport
            # error that reaches here unwrapped. A live endpoint returning an API
            # error (APIStatusError, auth) is NOT caught here and stays an AIError.
            raise AIProviderUnavailableError(
                f"AI provider endpoint unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        return _response_from_openai(resp, self._model)


def _message_to_openai(m: Message) -> list[dict[str, Any]]:
    """Translate one neutral Message into one or more OpenAI messages.

    A single user message containing ToolResultBlocks must expand into one
    `role=tool` message per block, because OpenAI ties each result back to
    its tool_call_id via the message role rather than a nested block.
    """
    if m.role == "user":
        tool_results = [b for b in m.content if isinstance(b, ToolResultBlock)]
        if tool_results:
            return [
                {
                    "role": "tool",
                    "tool_call_id": b.tool_use_id,
                    "content": (_TOOL_ERROR_SENTINEL + b.content) if b.is_error else b.content,
                }
                for b in tool_results
            ]
        # Plain user text turn.
        text_parts = [b.text for b in m.content if isinstance(b, TextBlock)]
        return [{"role": "user", "content": "\n".join(text_parts)}]

    # role == "assistant": collapse text + tool_use into one assistant message.
    text_parts = [b.text for b in m.content if isinstance(b, TextBlock)]
    tool_uses = [b for b in m.content if isinstance(b, ToolUseBlock)]
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_uses:
        msg["tool_calls"] = [
            {
                "id": b.id,
                "type": "function",
                "function": {"name": b.name, "arguments": json.dumps(b.input)},
            }
            for b in tool_uses
        ]
    return [msg]


def _tool_to_openai(t: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": _sanitize_schema_for_openai(t.input_schema),
        },
    }


def _tool_choice_to_openai(c: ToolChoice) -> Any:
    if isinstance(c, ToolChoiceTool):
        return {"type": "function", "function": {"name": c.name}}
    if isinstance(c, ToolChoiceRequired):
        return "required"
    if isinstance(c, ToolChoiceAuto):
        return "auto"
    # ToolChoiceNone is filtered out by the caller; reaching here is a bug.
    raise AIError(f"unsupported tool_choice: {type(c).__name__}")


def _sanitize_schema_for_openai(schema: dict[str, Any]) -> dict[str, Any]:
    """Chokepoint for provider-specific JSON-schema rewrites.

    HERD's existing schemas are clean (no anyOf, no $ref, no format
    constraints), so this is passthrough today. Add narrow rewrites here
    when a specific vLLM/Ollama version reveals a quirk; do NOT broaden it
    into a generic schema validator.
    """
    return schema


def _response_from_openai(resp: Any, model: str) -> ProviderResponse:
    if not resp.choices:
        raise AIError("OpenAI response had no choices")
    choice = resp.choices[0]
    msg = choice.message

    content: list[ContentBlock] = []
    text = getattr(msg, "content", None)
    if text:
        content.append(TextBlock(text=text))
    for tc in getattr(msg, "tool_calls", None) or []:
        content.append(
            ToolUseBlock(
                id=tc.id,
                name=tc.function.name,
                input=_safe_json_loads(tc.function.arguments),
            )
        )

    usage = getattr(resp, "usage", None)
    return ProviderResponse(
        content=content,
        stop_reason=_stop_reason_from_openai(getattr(choice, "finish_reason", None)),
        usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        ),
        raw_model=model,
    )


_OPENAI_STOP_REASONS: dict[str | None, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "other",
    "function_call": "tool_use",
}


def _stop_reason_from_openai(raw: str | None) -> StopReason:
    return _OPENAI_STOP_REASONS.get(raw, "other")


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    """Decode tool_call arguments. Returns {} on malformed JSON so the
    dispatcher's argument-validation path catches the empty-args case
    rather than the orchestrator swallowing the failure.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("openai_tool_arguments_malformed", extra={"raw_arguments": raw[:200]})
        return {}
    return parsed if isinstance(parsed, dict) else {}
