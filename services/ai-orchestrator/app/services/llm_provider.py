"""Provider-neutral types for the LLM abstraction layer.

AIClient orchestrates against this Protocol; concrete providers
(AnthropicProvider, OpenAICompatProvider) live under app/services/providers/
and translate to and from their respective SDK shapes. Nothing in this
module imports any provider SDK.

Stop-reason values are normalized across providers so the orchestrator
loop can key off `stop_reason == "tool_use"` without caring whether the
underlying API returned Anthropic's "tool_use" or OpenAI's "tool_calls".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"]


class AIError(Exception):
    """Raised by providers and the orchestrator on any LLM-side failure.

    Covers: model returned no usable content, malformed tool input, per-call
    timeout, SDK-level errors surfacing as opaque failures. Routes catch this
    and return HTTP 502.
    """


class AIProviderUnavailableError(AIError):
    """Raised when the configured provider endpoint is unreachable at the
    transport layer: connection refused, DNS failure, TLS/CA verification
    failure, or a connect/read timeout from the SDK's HTTP client.

    A subclass of AIError so any existing `except AIError` still catches it,
    but distinct so routes can map an unreachable-but-configured provider to
    HTTP 503 (per the issue #131 upstream-unreachable standardization) while a
    live provider that returned an API error (auth, 4xx, 5xx) stays 502. Only
    unreachable-class failures raise this; provider API errors stay AIError.
    """


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    """Tool call emitted by the model.

    The `id` is opaque to AIClient and tests; Anthropic emits `toolu_*`,
    OpenAI returns whatever string the model invented. It must round-trip
    verbatim into the matching `ToolResultBlock.tool_use_id` on the next turn.
    """

    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True)
class ToolResultBlock:
    """Tool dispatch result, echoed back to the model on the next turn.

    `content` is the JSON-serialized result string produced by ToolDispatcher
    (already truncated to the dispatcher's char_cap). `is_error` carries
    Anthropic's first-class error flag; providers without a native error
    channel (OpenAI) surface this in-band by prepending a sentinel.
    """

    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class Message:
    role: Role
    content: list[ContentBlock]


@dataclass(frozen=True)
class ToolSchema:
    """Provider-neutral tool definition.

    `input_schema` is the JSON Schema Anthropic accepts directly. The OpenAI
    provider re-wraps it under `{type: "function", function: {parameters: ...}}`
    on the way out.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolChoiceAuto:
    kind: Literal["auto"] = "auto"


@dataclass(frozen=True)
class ToolChoiceNone:
    kind: Literal["none"] = "none"


@dataclass(frozen=True)
class ToolChoiceRequired:
    kind: Literal["required"] = "required"


@dataclass(frozen=True)
class ToolChoiceTool:
    name: str
    kind: Literal["tool"] = "tool"


ToolChoice = ToolChoiceAuto | ToolChoiceNone | ToolChoiceRequired | ToolChoiceTool


@dataclass
class Usage:
    """Token counts. Mutable so the orchestrator loop can accumulate across turns.

    `input_tokens` is the UNCACHED input the model processed at full price; it is
    the only input figure that counts toward the per-user daily quota. The two
    cache fields are reported by Anthropic prompt caching for observability only
    (cache writes cost ~1.25x base, reads ~0.1x) and are deliberately NOT metered
    against the quota: they are logged and persisted separately. OpenAI-compat has
    no prompt caching, so its provider leaves both cache fields at 0.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens


@dataclass(frozen=True)
class ProviderResponse:
    """One LLM turn, normalized.

    `content` only contains TextBlock and ToolUseBlock instances; ToolResultBlock
    is request-only. `raw_model` is the model string the provider actually used,
    echoed so logs can record what answered the call.
    """

    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage
    raw_model: str


@dataclass(frozen=True)
class TextDelta:
    """An incremental chunk of assistant TEXT during a streamed turn.

    Streaming only forwards real `text` content. A reasoning model (e.g. Qwen
    via a local Anthropic-compatible endpoint) emits a leading `thinking` block;
    providers MUST drop thinking deltas here so the orchestrator and client never
    see raw chain-of-thought, exactly as the buffered path keeps only TextBlock.
    """

    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True)
class StreamDone:
    """Terminal event of a streamed turn, carrying the fully-assembled turn.

    `response` is byte-for-byte what `call()` would have returned for the same
    request, so the orchestrator's tool-loop, usage accounting, and multi-turn
    persistence work identically whether the turn was streamed or buffered.
    """

    response: ProviderResponse
    type: Literal["stream_done"] = "stream_done"


StreamEvent = TextDelta | StreamDone


@runtime_checkable
class LLMProvider(Protocol):
    """One LLM-turn primitive. All orchestration (multi-turn loops, timeouts,
    tool dispatching) sits above this interface in AIClient.
    """

    async def call(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        tool_choice: ToolChoice,
        max_tokens: int,
        timeout_s: float | None,
    ) -> ProviderResponse: ...

    # call_stream is OPTIONAL: a provider that omits it simply does not support
    # streaming, and AIClient falls back to the buffered call(). Implementers
    # yield TextDelta events for real text content (dropping thinking/tool_use
    # deltas) and a final StreamDone carrying the assembled ProviderResponse.
    # Declared here for documentation; presence is checked with hasattr, not by
    # the Protocol, so non-streaming providers still satisfy LLMProvider.
