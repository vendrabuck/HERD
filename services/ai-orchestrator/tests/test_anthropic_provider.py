"""Tests for AnthropicProvider's Anthropic-SDK translation layer.

Mocks AsyncAnthropic.messages.create to verify request kwargs and response
parsing in both directions. AIClient orchestration tests live in
test_ai_client.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.services.llm_provider import (
    AIError,
    Message,
    TextBlock,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from app.services.providers.anthropic_provider import (
    AnthropicProvider,
    _block_to_anthropic,
    _build_anthropic_http_client,
    _message_to_anthropic,
    _response_from_anthropic,
    _stop_reason_from_anthropic,
    _tool_choice_to_anthropic,
    _tool_to_anthropic,
)


def _provider_with_response(
    content_blocks, stop_reason="end_turn", input_tokens=10, output_tokens=20
):
    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    fake_message = SimpleNamespace(
        content=content_blocks,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )
    create_mock = AsyncMock(return_value=fake_message)
    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    return provider, create_mock


def test_init_keyless_local_endpoint_does_not_raise():
    """A blank api_key plus a base_url targets a local Anthropic-compatible
    endpoint (e.g. vLLM); the blank key becomes the "EMPTY" placeholder so the
    SDK constructor does not reject it."""
    provider = AnthropicProvider(api_key="", base_url="https://vllm:8000/v1", model="claude-test")
    assert provider._model == "claude-test"


def test_init_sets_explicit_client_timeout():
    """The SDK refuses a non-streaming request when it computes that max_tokens
    could exceed its default-timeout ceiling ("Streaming is required for
    operations that may take longer than 10 minutes"), which trips for large
    max_tokens against a reasoning model. An explicit long client timeout clears
    that pre-flight guard; the real per-call bound stays asyncio.wait_for in
    call(). Pin the timeout so the guard fix does not silently regress."""
    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-test")
    assert provider._client.timeout == 1800.0


def test_init_with_verify_tls_false_does_not_raise():
    """Regression test for the anthropic SDK 1.x migration (issue #592): under
    0.x, AsyncAnthropic accepted a plain httpx.AsyncClient as http_client=; under
    1.x (built on httpx2) that raised TypeError: Expected an instance of
    httpx2.AsyncClient. Constructing with verify_tls=False builds a real
    non-verifying httpx2 client and must not raise."""
    provider = AnthropicProvider(
        api_key="",
        base_url="https://self-signed.test:8443",
        model="claude-test",
        verify_tls=False,
    )
    assert provider._model == "claude-test"


# --- _build_anthropic_http_client (verify_tls / ca_cert precedence) ---


def test_build_http_client_default_returns_none():
    """verify_tls=True (the default) with no ca_cert returns None, so the SDK
    falls back to its own default client and default system-CA verification."""
    assert _build_anthropic_http_client(verify_tls=True, ca_cert=None) is None


def test_build_http_client_verify_tls_false_returns_non_verifying_client():
    """verify_tls=False builds an anthropic.DefaultAsyncHttpxClient with
    verification off (self-signed on-prem endpoint, no pinned cert)."""
    sentinel = object()
    with patch(
        "app.services.providers.anthropic_provider.DefaultAsyncHttpxClient",
        return_value=sentinel,
    ) as mock_client:
        result = _build_anthropic_http_client(verify_tls=False, ca_cert=None)
    mock_client.assert_called_once_with(verify=False)
    assert result is sentinel


def test_build_http_client_ca_cert_verifies_against_bundle_and_takes_precedence():
    """ca_cert builds an SSL context from the bundle and verifies against it
    (fail-closed), overriding verify_tls."""
    client_sentinel = object()
    ctx_sentinel = object()
    with (
        patch(
            "app.services.providers.anthropic_provider.ssl.create_default_context",
            return_value=ctx_sentinel,
        ) as mock_ctx,
        patch(
            "app.services.providers.anthropic_provider.DefaultAsyncHttpxClient",
            return_value=client_sentinel,
        ) as mock_client,
    ):
        result = _build_anthropic_http_client(verify_tls=True, ca_cert="/certs/spark-ca.pem")
    mock_ctx.assert_called_once_with(cafile="/certs/spark-ca.pem")
    mock_client.assert_called_once_with(verify=ctx_sentinel)
    assert result is client_sentinel


def _sdk_tool_use(name="t", args=None, block_id="toolu_001"):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=args or {})


def _sdk_text(text):
    return SimpleNamespace(type="text", text=text)


# --- Tool choice translation ---


def test_tool_choice_auto_returns_none():
    assert _tool_choice_to_anthropic(ToolChoiceAuto()) is None


def test_tool_choice_required_returns_any():
    assert _tool_choice_to_anthropic(ToolChoiceRequired()) == {"type": "any"}


def test_tool_choice_tool_returns_named_tool():
    assert _tool_choice_to_anthropic(ToolChoiceTool(name="foo")) == {"type": "tool", "name": "foo"}


@pytest.mark.asyncio
async def test_call_drops_tools_and_tool_choice_when_none():
    provider, create_mock = _provider_with_response([_sdk_text("ok")])
    await provider.call(
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[ToolSchema(name="x", description="x", input_schema={"type": "object"})],
        tool_choice=ToolChoiceNone(),
        max_tokens=100,
        timeout_s=None,
    )
    kwargs = create_mock.call_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


@pytest.mark.asyncio
async def test_call_omits_tool_choice_for_auto():
    provider, create_mock = _provider_with_response([_sdk_text("ok")])
    await provider.call(
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[ToolSchema(name="x", description="x", input_schema={"type": "object"})],
        tool_choice=ToolChoiceAuto(),
        max_tokens=100,
        timeout_s=None,
    )
    kwargs = create_mock.call_args.kwargs
    assert "tools" in kwargs
    assert "tool_choice" not in kwargs


@pytest.mark.asyncio
async def test_call_sends_named_tool_choice():
    provider, create_mock = _provider_with_response([_sdk_text("ok")])
    await provider.call(
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[ToolSchema(name="x", description="x", input_schema={"type": "object"})],
        tool_choice=ToolChoiceTool(name="x"),
        max_tokens=100,
        timeout_s=None,
    )
    assert create_mock.call_args.kwargs["tool_choice"] == {"type": "tool", "name": "x"}


# --- Tool schema translation ---


def test_tool_to_anthropic_passthrough():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    result = _tool_to_anthropic(
        ToolSchema(name="search", description="do search", input_schema=schema)
    )
    assert result == {"name": "search", "description": "do search", "input_schema": schema}


# --- Message and block translation ---


def test_text_block_to_anthropic():
    assert _block_to_anthropic(TextBlock(text="hello")) == {"type": "text", "text": "hello"}


def test_tool_use_block_to_anthropic():
    out = _block_to_anthropic(ToolUseBlock(id="toolu_abc", name="get_x", input={"a": 1}))
    assert out == {"type": "tool_use", "id": "toolu_abc", "name": "get_x", "input": {"a": 1}}


def test_tool_result_block_to_anthropic_preserves_is_error():
    out = _block_to_anthropic(
        ToolResultBlock(tool_use_id="toolu_abc", content="oops", is_error=True)
    )
    assert out == {
        "type": "tool_result",
        "tool_use_id": "toolu_abc",
        "content": "oops",
        "is_error": True,
    }


def test_message_to_anthropic_user_with_text():
    m = Message(role="user", content=[TextBlock(text="hi")])
    assert _message_to_anthropic(m) == {"role": "user", "content": [{"type": "text", "text": "hi"}]}


def test_message_to_anthropic_assistant_mixed_blocks():
    m = Message(
        role="assistant",
        content=[
            TextBlock(text="Let me check."),
            ToolUseBlock(id="toolu_1", name="get_device", input={"id": "d"}),
        ],
    )
    out = _message_to_anthropic(m)
    assert out["role"] == "assistant"
    assert out["content"][0] == {"type": "text", "text": "Let me check."}
    assert out["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_device",
        "input": {"id": "d"},
    }


# --- Response translation ---


def test_response_from_anthropic_extracts_text_and_tool_use():
    sdk_msg = SimpleNamespace(
        content=[_sdk_text("here is some text"), _sdk_tool_use(name="t", args={"k": 1})],
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
        stop_reason="tool_use",
    )
    resp = _response_from_anthropic(sdk_msg, "claude-test")
    assert len(resp.content) == 2
    assert isinstance(resp.content[0], TextBlock)
    assert resp.content[0].text == "here is some text"
    assert isinstance(resp.content[1], ToolUseBlock)
    assert resp.content[1].name == "t"
    assert resp.content[1].input == {"k": 1}
    assert resp.stop_reason == "tool_use"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 7
    assert resp.raw_model == "claude-test"


def test_response_from_anthropic_drops_unknown_block_types():
    sdk_msg = SimpleNamespace(
        content=[SimpleNamespace(type="some_future_block"), _sdk_text("real")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
    )
    resp = _response_from_anthropic(sdk_msg, "model-x")
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], TextBlock)


# --- Stop reason normalization ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        (None, "other"),
        ("something_new_anthropic_invented", "other"),
        ("", "other"),
    ],
)
def test_stop_reason_normalization(raw, expected):
    assert _stop_reason_from_anthropic(raw) == expected


# --- Timeout and SDK error behavior ---


@pytest.mark.asyncio
async def test_call_raises_aierror_on_timeout():
    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")

    async def _hang(**_kwargs):
        import asyncio as _a

        await _a.sleep(10)

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_hang))
    with pytest.raises(AIError) as exc:
        await provider.call(
            system="sys",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=None,
            tool_choice=ToolChoiceAuto(),
            max_tokens=100,
            timeout_s=0.05,
        )
    assert "exceeded" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_call_propagates_sdk_exceptions():
    """Errors from the SDK (rate limits, auth, network) are not swallowed."""

    class _RateLimited(Exception):
        pass

    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=_RateLimited("429")))
    )
    with pytest.raises(_RateLimited):
        await provider.call(
            system="sys",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=None,
            tool_choice=ToolChoiceAuto(),
            max_tokens=100,
            timeout_s=None,
        )


@pytest.mark.asyncio
async def test_call_maps_connection_error_to_unavailable():
    """A transport-layer failure (connection refused, DNS, TLS/CA) surfaces from
    the SDK as APIConnectionError; the provider translates it to
    AIProviderUnavailableError so a configured-but-unreachable endpoint maps to
    503, not a bare 500/502 (issue #280)."""
    import httpx
    from anthropic import APIConnectionError
    from app.services.llm_provider import AIProviderUnavailableError

    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    req = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=APIConnectionError(request=req)))
    )
    with pytest.raises(AIProviderUnavailableError):
        await provider.call(
            system="sys",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=None,
            tool_choice=ToolChoiceAuto(),
            max_tokens=100,
            timeout_s=None,
        )


@pytest.mark.asyncio
async def test_call_maps_raw_httpx2_transport_error_to_unavailable():
    """A raw httpx2.ConnectError that reaches the call boundary unwrapped (rather
    than the SDK's own APIConnectionError) is also classified as unreachable
    (belt-and-suspenders for the SDK connection type; mirrors
    test_openai_provider.test_call_maps_raw_httpx_transport_error_to_unavailable).
    This is the exception-type match the anthropic SDK 1.x migration (issue #592)
    depends on: the except clause catches httpx2.TransportError, so it must match
    what the 1.x SDK actually raises."""
    import httpx2
    from app.services.llm_provider import AIProviderUnavailableError

    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=httpx2.ConnectError("connection refused"))
        )
    )
    with pytest.raises(AIProviderUnavailableError):
        await provider.call(
            system="sys",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=None,
            tool_choice=ToolChoiceAuto(),
            max_tokens=100,
            timeout_s=None,
        )


@pytest.mark.asyncio
async def test_call_stream_maps_connection_error_to_unavailable():
    """The streaming path classifies the same transport failure the buffered path
    does, so an unreachable provider on a streamed turn also raises
    AIProviderUnavailableError (which the route renders as an SSE error event)."""
    import httpx
    from anthropic import APIConnectionError
    from app.services.llm_provider import AIProviderUnavailableError

    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    req = httpx.Request("POST", "https://api.anthropic.test/v1/messages")

    def _raise(**_kwargs):
        raise APIConnectionError(request=req)

    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=_raise))
    with pytest.raises(AIProviderUnavailableError):
        await _drain(provider)


@pytest.mark.asyncio
async def test_call_passes_max_tokens_and_system_through():
    provider, create_mock = _provider_with_response([_sdk_text("ok")])
    await provider.call(
        system="be helpful",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=None,
        tool_choice=ToolChoiceAuto(),
        max_tokens=2048,
        timeout_s=None,
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["max_tokens"] == 2048
    # The system prompt is now rendered as a list of one cache-controlled text
    # block (prompt-caching breakpoint) rather than a bare string.
    assert kwargs["system"] == [
        {"type": "text", "text": "be helpful", "cache_control": {"type": "ephemeral"}}
    ]
    assert kwargs["model"] == "claude-opus-4-7"


# --- Prompt caching (issue #65) ---


@pytest.mark.asyncio
async def test_call_sets_cache_control_on_system_block():
    """The system prompt is sent as a single ephemeral cache-controlled block so
    the tools + system prefix can be cached across requests."""
    provider, create_mock = _provider_with_response([_sdk_text("ok")])
    await provider.call(
        system="frozen instruction prefix",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[ToolSchema(name="x", description="x", input_schema={"type": "object"})],
        tool_choice=ToolChoiceAuto(),
        max_tokens=100,
        timeout_s=None,
    )
    system = create_mock.call_args.kwargs["system"]
    assert isinstance(system, list)
    assert len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["text"] == "frozen instruction prefix"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_response_from_anthropic_reads_cache_token_fields():
    """cache_creation_input_tokens and cache_read_input_tokens flow off the SDK
    usage object into the neutral Usage; input_tokens stays the uncached figure."""
    sdk_msg = SimpleNamespace(
        content=[_sdk_text("answer")],
        usage=SimpleNamespace(
            input_tokens=12,
            output_tokens=8,
            cache_creation_input_tokens=4096,
            cache_read_input_tokens=2048,
        ),
        stop_reason="end_turn",
    )
    resp = _response_from_anthropic(sdk_msg, "claude-test")
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 8
    assert resp.usage.cache_creation_input_tokens == 4096
    assert resp.usage.cache_read_input_tokens == 2048


def test_response_from_anthropic_cache_fields_default_zero_when_absent():
    """Older responses omit the cache fields; they default to 0, never None, so
    they never count toward the quota or break arithmetic."""
    sdk_msg = SimpleNamespace(
        content=[_sdk_text("answer")],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        stop_reason="end_turn",
    )
    resp = _response_from_anthropic(sdk_msg, "claude-test")
    assert resp.usage.cache_creation_input_tokens == 0
    assert resp.usage.cache_read_input_tokens == 0


def test_response_from_anthropic_cache_fields_coerce_none_to_zero():
    """A None on either cache field (some SDK shapes) is coerced to 0."""
    sdk_msg = SimpleNamespace(
        content=[_sdk_text("answer")],
        usage=SimpleNamespace(
            input_tokens=5,
            output_tokens=3,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
        stop_reason="end_turn",
    )
    resp = _response_from_anthropic(sdk_msg, "claude-test")
    assert resp.usage.cache_creation_input_tokens == 0
    assert resp.usage.cache_read_input_tokens == 0


# --- Streaming (call_stream) ---


class _FakeStream:
    """Mimics the SDK's messages.stream() async context manager.

    Yields the given events when iterated and returns final_message from
    get_final_message(), matching how AnthropicProvider.call_stream drives it.
    """

    def __init__(self, events, final_message):
        self._events = events
        self._final = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for ev in self._events:
            yield ev

    async def get_final_message(self):
        return self._final


def _stream_text_event(text):
    """The SDK's text-only delta event (type == 'text', carries .text)."""
    return SimpleNamespace(type="text", text=text)


def _stream_thinking_event(text):
    """A reasoning model's thinking delta; call_stream MUST drop these."""
    return SimpleNamespace(type="thinking", thinking=text)


def _provider_with_stream(events, final_content, stop_reason="end_turn"):
    provider = AnthropicProvider(api_key="sk-ant-fake", model="claude-opus-4-7")
    final_message = SimpleNamespace(
        content=final_content,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        stop_reason=stop_reason,
    )
    stream_factory = lambda **kwargs: _FakeStream(events, final_message)  # noqa: E731
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=stream_factory))
    return provider


async def _drain(provider, **kwargs):
    kwargs.setdefault("system", "sys")
    kwargs.setdefault("messages", [Message(role="user", content=[TextBlock(text="q")])])
    kwargs.setdefault("tools", None)
    kwargs.setdefault("tool_choice", ToolChoiceAuto())
    kwargs.setdefault("max_tokens", 256)
    kwargs.setdefault("timeout_s", None)
    return [ev async for ev in provider.call_stream(**kwargs)]


@pytest.mark.asyncio
async def test_call_stream_yields_text_deltas_then_done():
    provider = _provider_with_stream(
        events=[_stream_text_event("Hel"), _stream_text_event("lo")],
        final_content=[_sdk_text("Hello")],
    )
    events = await _drain(provider)
    # Text deltas in order, then exactly one terminal StreamDone last.
    assert [e.type for e in events] == ["text_delta", "text_delta", "stream_done"]
    assert [e.text for e in events[:2]] == ["Hel", "lo"]
    done = events[-1]
    assert done.response.stop_reason == "end_turn"
    assert done.response.content[0].text == "Hello"
    assert done.response.usage.output_tokens == 7


@pytest.mark.asyncio
async def test_call_stream_drops_thinking_deltas():
    """A reasoning model's leading thinking deltas must not reach the client;
    only real text deltas are forwarded, matching the buffered path."""
    provider = _provider_with_stream(
        events=[
            _stream_thinking_event("Let me think"),
            _stream_thinking_event(" about it"),
            _stream_text_event("Answer"),
        ],
        final_content=[_sdk_text("Answer")],
    )
    events = await _drain(provider)
    assert [e.type for e in events] == ["text_delta", "stream_done"]
    assert events[0].text == "Answer"


@pytest.mark.asyncio
async def test_call_stream_skips_empty_text_deltas():
    provider = _provider_with_stream(
        events=[_stream_text_event(""), _stream_text_event("x")],
        final_content=[_sdk_text("x")],
    )
    events = await _drain(provider)
    assert [e.type for e in events] == ["text_delta", "stream_done"]
    assert events[0].text == "x"


@pytest.mark.asyncio
async def test_call_stream_done_carries_tool_use_for_loop():
    """When the streamed turn ends in tool_use, StreamDone.response carries the
    tool_use block and stop_reason so the orchestrator loop can dispatch it."""
    provider = _provider_with_stream(
        events=[_stream_text_event("checking")],
        final_content=[_sdk_text("checking"), _sdk_tool_use(name="list_ports")],
        stop_reason="tool_use",
    )
    events = await _drain(provider)
    done = events[-1]
    assert done.type == "stream_done"
    assert done.response.stop_reason == "tool_use"
    assert any(isinstance(b, ToolUseBlock) for b in done.response.content)
