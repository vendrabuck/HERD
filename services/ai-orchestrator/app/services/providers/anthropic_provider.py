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


class AnthropicProvider:
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
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
            "system": system,
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
