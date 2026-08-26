"""Anthropic SDK adapter for the LLMProvider protocol.

Translates neutral Message/ContentBlock/ToolSchema instances into the
shape AsyncAnthropic.messages.create expects, and translates the response
back into a ProviderResponse with normalized stop_reason.

This is the ONLY file in the orchestrator that imports `anthropic`.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
from anthropic import APIConnectionError, AsyncAnthropic, DefaultAsyncHttpxClient

from app.services.llm_provider import (
    AIError,
    AIProviderUnavailableError,
    ContentBlock,
    Message,
    ProviderResponse,
    StopReason,
    StreamDone,
    StreamEvent,
    TextBlock,
    TextDelta,
    ToolChoice,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)


def _build_anthropic_http_client(
    *, verify_tls: bool, ca_cert: str | None
) -> DefaultAsyncHttpxClient | None:
    """Build the httpx2 client for the Anthropic SDK based on the endpoint's TLS needs.

    Precedence (mirrors openai_provider._build_http_client, kept separate per SDK
    since anthropic 1.x is built on httpx2 and rejects a plain httpx.AsyncClient):
      ca_cert set      -> verify against that CA bundle (e.g. a pinned self-signed
                          on-prem cert); verification stays ON and fails closed.
      verify_tls False -> skip verification (self-signed endpoint, no pinned cert).
      otherwise        -> None, letting the SDK use default system-CA verification.

    anthropic.DefaultAsyncHttpxClient subclasses httpx2's AsyncClient and preserves
    the SDK's own default timeouts and connection limits, so it is used here instead
    of a bare httpx2.AsyncClient.
    """
    if ca_cert:
        ctx = ssl.create_default_context(cafile=ca_cert)
        return DefaultAsyncHttpxClient(verify=ctx)
    if not verify_tls:
        return DefaultAsyncHttpxClient(verify=False)
    return None


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
        http_client = _build_anthropic_http_client(verify_tls=verify_tls, ca_cert=ca_cert)
        self._client = AsyncAnthropic(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            http_client=http_client,
            timeout=1800.0,
        )
        self._model = model

    def _build_kwargs(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        tool_choice: ToolChoice,
        max_tokens: int,
    ) -> dict[str, Any]:
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
        return kwargs

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
        kwargs = self._build_kwargs(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )
        coro = self._client.messages.create(**kwargs)
        try:
            sdk_msg = await (asyncio.wait_for(coro, timeout_s) if timeout_s is not None else coro)
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

        return _response_from_anthropic(sdk_msg, self._model)

    async def call_stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        tool_choice: ToolChoice,
        max_tokens: int,
        timeout_s: float | None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one turn, yielding TextDelta for real text then StreamDone.

        Uses the SDK's `messages.stream()` context manager. Only `text` content
        deltas are forwarded; a reasoning model's leading `thinking` deltas are
        dropped so the orchestrator and client never see chain-of-thought. The
        terminal StreamDone carries the same ProviderResponse `call()` would
        return (assembled from `get_final_message()`), so usage, stop_reason, and
        multi-turn persistence are identical to the buffered path.

        The per-call deadline is enforced as a wall-clock budget across the whole
        stream: each delta await is bounded so a stalled stream cannot hang the
        turn, matching the asyncio.wait_for bound on call().
        """
        kwargs = self._build_kwargs(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )
        deadline = (asyncio.get_event_loop().time() + timeout_s) if timeout_s is not None else None

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if deadline is not None and asyncio.get_event_loop().time() > deadline:
                        raise AIError(f"AI call exceeded {timeout_s}s")
                    # The SDK surfaces text-only deltas via this typed event; it
                    # excludes thinking/tool_use input deltas, which is exactly
                    # the filter we want (forward real answer text only).
                    if getattr(event, "type", None) == "text":
                        text = getattr(event, "text", "")
                        if text:
                            yield TextDelta(text=text)
                final = await stream.get_final_message()
        except asyncio.TimeoutError as exc:
            raise AIError(f"AI call exceeded {timeout_s}s") from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            raise AIProviderUnavailableError(
                f"AI provider endpoint unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        yield StreamDone(response=_response_from_anthropic(final, self._model))


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
