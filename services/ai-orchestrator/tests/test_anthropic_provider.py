"""Tests for AnthropicProvider's Anthropic-SDK translation layer.

Mocks AsyncAnthropic.messages.create to verify request kwargs and response
parsing in both directions. AIClient orchestration tests live in
test_ai_client.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
