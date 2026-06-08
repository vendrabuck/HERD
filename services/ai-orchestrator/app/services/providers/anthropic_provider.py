"""Anthropic SDK adapter for the LLMProvider protocol.

Translates neutral Message/ContentBlock/ToolSchema instances into the
shape AsyncAnthropic.messages.create expects, and translates the response
back into a ProviderResponse with normalized stop_reason.

This is the ONLY file in the orchestrator that imports `anthropic`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from anthropic import AsyncAnthropic

from app.services.llm_provider import (
    AIError,
    ContentBlock,
    Message,
    ProviderResponse,
    StopReason,
    TextBlock,
    ToolChoice,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)
from app.services.providers.openai_provider import _build_http_client


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        verify_tls: bool = True,
        ca_cert: str | None = None,
    ) -> None:
        # AsyncAnthropic rejects blank api_key strings; a local Anthropic-compatible
        # endpoint (e.g. vLLM) typically ignores auth, so "EMPTY" is the conventional
        # placeholder, mirroring OpenAICompatProvider. base_url lets that local server
        # be targeted instead of Anthropic's hosted API, and the shared httpx client
        # carries the same self-signed-TLS handling the OpenAI path uses.
        #
        # Explicit long client timeout: the SDK refuses a non-streaming request when
        # it computes that max_tokens could exceed its default-timeout ceiling
        # (raising "Streaming is required for operations that may take longer than 10
        # minutes"), which trips for large max_tokens with a reasoning model. The real
        # per-call bound is enforced upstream by asyncio.wait_for(timeout_s) in call(),
        # so a generous SDK timeout here just clears that pre-flight guard without
        # changing the effective deadline.
        http_client = _build_http_client(verify_tls=verify_tls, ca_cert=ca_cert)
        self._client = AsyncAnthropic(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            http_client=http_client,
            timeout=1800.0,
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
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": _system_to_anthropic(system),
            "messages": [_message_to_anthropic(m) for m in messages],
        }
        if not isinstance(tool_choice, ToolChoiceNone) and tools is not None:
            kwargs["tools"] = [_tool_to_anthropic(t) for t in tools]
            anthropic_choice = _tool_choice_to_anthropic(tool_choice)
            if anthropic_choice is not None:
                kwargs["tool_choice"] = anthropic_choice

        coro = self._client.messages.create(**kwargs)
        try:
            sdk_msg = await (asyncio.wait_for(coro, timeout_s) if timeout_s is not None else coro)
        except asyncio.TimeoutError as exc:
            raise AIError(f"AI call exceeded {timeout_s}s") from exc

        return _response_from_anthropic(sdk_msg, self._model)


def _system_to_anthropic(system: str) -> list[dict[str, Any]]:
    """Render the system prompt as a single cache-controlled text block.

    Anthropic prompt caching is a PREFIX match over the rendered request in the
    order tools -> system -> messages, so a cache_control breakpoint on the last
    system block caches the tool schemas plus the system prompt together; the
    per-request user content (live inventory, the user's question) stays in
    `messages`, after the breakpoint, where it cannot invalidate the cached
    prefix. The 5-minute ephemeral TTL is the default.

    This only yields cache hits when the rendered prefix is byte-identical across
    requests AND exceeds Anthropic's minimum cacheable prefix (4096 tokens for
    Opus). The assistant and identity call sites pass a frozen system constant,
    so they cache once the tool schemas push the prefix over that floor; the
    topology call site interpolates live inventory into the system prompt, so its
    prefix differs every request and caching is silently a no-op there (no error,
    cache_creation_input_tokens stays 0). Marking the block unconditionally is
    safe: a non-repeating prefix simply never reads back what it wrote.
    """
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _message_to_anthropic(m: Message) -> dict[str, Any]:
    return {"role": m.role, "content": [_block_to_anthropic(b) for b in m.content]}


def _block_to_anthropic(b: ContentBlock) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": b.content,
            "is_error": b.is_error,
        }
    raise AIError(f"unknown ContentBlock variant: {type(b).__name__}")


def _tool_to_anthropic(t: ToolSchema) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _tool_choice_to_anthropic(c: ToolChoice) -> dict[str, Any] | None:
    if isinstance(c, ToolChoiceTool):
        return {"type": "tool", "name": c.name}
    if isinstance(c, ToolChoiceRequired):
        return {"type": "any"}
    # ToolChoiceAuto: omit; Anthropic defaults to auto when tools are present.
    # ToolChoiceNone is handled by the caller (drops both tools and tool_choice).
    return None


def _response_from_anthropic(sdk_msg: Any, model: str) -> ProviderResponse:
    content: list[ContentBlock] = []
    for block in sdk_msg.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            content.append(TextBlock(text=block.text))
        elif block_type == "tool_use":
            content.append(
                ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {}))
            )
        # Unknown block types are dropped silently, matching prior behavior.
    return ProviderResponse(
        content=content,
        stop_reason=_stop_reason_from_anthropic(getattr(sdk_msg, "stop_reason", None)),
        usage=Usage(
            input_tokens=getattr(sdk_msg.usage, "input_tokens", 0),
            output_tokens=getattr(sdk_msg.usage, "output_tokens", 0),
            # Older SDK responses (and responses with no caching) omit these or
            # report None; default to 0 so they never count toward the quota and
            # never break arithmetic. Anthropic reports cache_read_input_tokens
            # separately from input_tokens, so input_tokens already excludes any
            # cached prefix and stays correct as the quota figure.
            cache_creation_input_tokens=getattr(sdk_msg.usage, "cache_creation_input_tokens", 0)
            or 0,
            cache_read_input_tokens=getattr(sdk_msg.usage, "cache_read_input_tokens", 0) or 0,
        ),
        raw_model=model,
    )


_ANTHROPIC_STOP_REASONS: dict[str | None, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


def _stop_reason_from_anthropic(raw: str | None) -> StopReason:
    return _ANTHROPIC_STOP_REASONS.get(raw, "other")
