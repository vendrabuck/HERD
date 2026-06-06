"""Tests for OpenAICompatProvider's OpenAI-SDK translation layer.

Mocks AsyncOpenAI.chat.completions.create to verify request kwargs and
response parsing in both directions. Targets the wire shape that vLLM,
Ollama, LM Studio, OpenAI proper, and Azure all speak.
"""

import json
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
from app.services.providers.openai_provider import (
    OpenAICompatProvider,
    _message_to_openai,
    _response_from_openai,
    _safe_json_loads,
    _sanitize_schema_for_openai,
    _stop_reason_from_openai,
    _tool_choice_to_openai,
    _tool_to_openai,
)


def _sdk_tool_call(call_id, name, args_obj):
    """Build a SimpleNamespace mimicking the openai SDK's ChatCompletionMessageToolCall."""
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args_obj)),
    )


def _sdk_message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _sdk_completion(content=None, tool_calls=None, finish_reason="stop", prompt=10, completion=20):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=_sdk_message(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _provider_with_response(completion):
    provider = OpenAICompatProvider(api_key="sk-test", base_url=None, model="test-model")
    create_mock = AsyncMock(return_value=completion)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )
    return provider, create_mock


# --- Tool choice translation ---


def test_tool_choice_auto():
    assert _tool_choice_to_openai(ToolChoiceAuto()) == "auto"


def test_tool_choice_required():
    assert _tool_choice_to_openai(ToolChoiceRequired()) == "required"


def test_tool_choice_specific_tool():
    assert _tool_choice_to_openai(ToolChoiceTool(name="search")) == {
        "type": "function",
        "function": {"name": "search"},
    }


def test_tool_choice_none_in_translation_raises():
    """ToolChoiceNone should be filtered by the caller; the helper rejects it."""
    with pytest.raises(AIError):
        _tool_choice_to_openai(ToolChoiceNone())


@pytest.mark.asyncio
async def test_call_drops_tools_and_tool_choice_when_none():
    provider, create_mock = _provider_with_response(_sdk_completion(content="ok"))
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
async def test_call_sends_tools_and_auto_choice():
    provider, create_mock = _provider_with_response(_sdk_completion(content="ok"))
    await provider.call(
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[ToolSchema(name="x", description="x", input_schema={"type": "object"})],
        tool_choice=ToolChoiceAuto(),
        max_tokens=100,
        timeout_s=None,
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tool_choice"] == "auto"


# --- Tool schema translation ---


def test_tool_to_openai_wraps_under_function():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    result = _tool_to_openai(
        ToolSchema(name="search", description="do search", input_schema=schema)
    )
    assert result == {
        "type": "function",
        "function": {"name": "search", "description": "do search", "parameters": schema},
    }


def test_sanitize_schema_is_passthrough_today():
    """Locks the no-op contract; revisit when vLLM reveals a quirk."""
    schema = {"type": "object", "properties": {"x": {"type": ["string", "null"]}}}
    assert _sanitize_schema_for_openai(schema) is schema


# --- Message translation ---


def test_user_text_message_becomes_single_user_message():
    out = _message_to_openai(Message(role="user", content=[TextBlock(text="hello")]))
    assert out == [{"role": "user", "content": "hello"}]


def test_assistant_with_text_and_tool_use_collapses_to_one_message_with_json_args():
    m = Message(
        role="assistant",
        content=[
            TextBlock(text="Let me check."),
            ToolUseBlock(id="call_1", name="get_device", input={"id": "dev-a"}),
        ],
    )
    out = _message_to_openai(m)
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Let me check."
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_device"
    # Critical: arguments MUST be a JSON-encoded string, not the dict object.
    assert tc["function"]["arguments"] == '{"id": "dev-a"}'


def test_assistant_with_tool_use_only_has_null_content():
    m = Message(
        role="assistant",
        content=[ToolUseBlock(id="call_1", name="t", input={})],
    )
    out = _message_to_openai(m)
    assert out[0]["content"] is None


def test_user_with_tool_results_emits_separate_tool_messages():
    """OpenAI requires one role=tool message per tool_call_id, not a single user message."""
    m = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="call_a", content='{"name":"a"}', is_error=False),
            ToolResultBlock(tool_use_id="call_b", content='{"name":"b"}', is_error=False),
        ],
    )
    out = _message_to_openai(m)
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "call_a", "content": '{"name":"a"}'}
    assert out[1] == {"role": "tool", "tool_call_id": "call_b", "content": '{"name":"b"}'}


def test_tool_result_is_error_true_prepends_sentinel():
    m = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="call_x", content="device not found", is_error=True)],
    )
    out = _message_to_openai(m)
    assert out[0]["content"].startswith("[tool error] ")
    assert "device not found" in out[0]["content"]


# --- Request structure ---


@pytest.mark.asyncio
async def test_call_prepends_system_message_as_first():
    provider, create_mock = _provider_with_response(_sdk_completion(content="ok"))
    await provider.call(
        system="be helpful",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=None,
        tool_choice=ToolChoiceAuto(),
        max_tokens=100,
        timeout_s=None,
    )
    msgs = create_mock.call_args.kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "be helpful"}
    assert msgs[1]["role"] == "user"


@pytest.mark.asyncio
async def test_call_passes_max_tokens_and_model():
    provider, create_mock = _provider_with_response(_sdk_completion(content="ok"))
    await provider.call(
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=None,
        tool_choice=ToolChoiceAuto(),
        max_tokens=2048,
        timeout_s=None,
    )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["max_tokens"] == 2048
    assert kwargs["model"] == "test-model"


# --- Response translation ---


def test_response_extracts_text_and_tool_calls():
    completion = _sdk_completion(
        content="checking now",
        tool_calls=[_sdk_tool_call("call_xyz", "get_device", {"id": "dev"})],
        finish_reason="tool_calls",
        prompt=5,
        completion=7,
    )
    resp = _response_from_openai(completion, "test-model")
    assert len(resp.content) == 2
    assert isinstance(resp.content[0], TextBlock)
    assert resp.content[0].text == "checking now"
    assert isinstance(resp.content[1], ToolUseBlock)
    assert resp.content[1].id == "call_xyz"
    assert resp.content[1].name == "get_device"
    assert resp.content[1].input == {"id": "dev"}
    assert resp.stop_reason == "tool_use"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 7


def test_response_handles_null_content():
    """When the model returns only tool_calls, message.content is None."""
    completion = _sdk_completion(
        content=None,
        tool_calls=[_sdk_tool_call("call_1", "t", {})],
        finish_reason="tool_calls",
    )
    resp = _response_from_openai(completion, "m")
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], ToolUseBlock)


def test_response_handles_no_tool_calls():
    completion = _sdk_completion(content="just text", tool_calls=None, finish_reason="stop")
    resp = _response_from_openai(completion, "m")
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], TextBlock)


def test_response_raises_on_empty_choices():
    completion = SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0)
    )
    with pytest.raises(AIError):
        _response_from_openai(completion, "m")


# --- Stop reason normalization ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "other"),
        ("function_call", "tool_use"),  # legacy alias
        (None, "other"),
        ("something_new", "other"),
        ("", "other"),
    ],
)
def test_stop_reason_normalization(raw, expected):
    assert _stop_reason_from_openai(raw) == expected


# --- Tool call ID round-trip (the most critical correctness test) ---


@pytest.mark.asyncio
async def test_tool_call_id_round_trips_verbatim():
    """OpenAI rejects requests where role=tool's tool_call_id doesn't match a
    prior tool_calls[i].id from the assistant turn. Lock the contract."""
    # Simulate the loop: assistant returns a tool_call with a specific ID;
    # the orchestrator echoes it back in the next turn.
    given_id = "call_RANDOM_abc_123"
    msg = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id=given_id, content='{"ok":true}', is_error=False)],
    )
    out = _message_to_openai(msg)
    assert out[0]["tool_call_id"] == given_id


# --- Arguments JSON safety ---


def test_safe_json_loads_returns_empty_dict_on_malformed():
    assert _safe_json_loads("not { json }") == {}


def test_safe_json_loads_returns_empty_on_none():
    assert _safe_json_loads(None) == {}


def test_safe_json_loads_returns_empty_on_non_dict():
    """Tool arguments must be an object; a JSON array or scalar is treated as missing."""
    assert _safe_json_loads('["a","b"]') == {}
    assert _safe_json_loads('"just a string"') == {}


def test_safe_json_loads_decodes_well_formed_object():
    assert _safe_json_loads('{"id": "dev", "n": 1}') == {"id": "dev", "n": 1}


@pytest.mark.asyncio
async def test_malformed_tool_arguments_surface_as_empty_input():
    """The dispatcher's argument validator catches the empty-args case."""
    bad_tc = SimpleNamespace(
        id="call_x",
        type="function",
        function=SimpleNamespace(name="get_device", arguments="not { json }"),
    )
    completion = _sdk_completion(content=None, tool_calls=[bad_tc], finish_reason="tool_calls")
    resp = _response_from_openai(completion, "m")
    assert len(resp.content) == 1
    block = resp.content[0]
    assert isinstance(block, ToolUseBlock)
    assert block.input == {}
    assert block.id == "call_x"


# --- Timeout and SDK error behavior ---


@pytest.mark.asyncio
async def test_call_raises_aierror_on_timeout():
    provider = OpenAICompatProvider(api_key="sk-test", base_url=None, model="m")

    async def _hang(**_kwargs):
        import asyncio as _a

        await _a.sleep(10)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_hang))
    )
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
    class _RateLimited(Exception):
        pass

    provider = OpenAICompatProvider(api_key="sk-test", base_url=None, model="m")
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=_RateLimited("429")))
        )
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


# --- Local-server "EMPTY" api_key handling ---


def test_blank_api_key_becomes_empty_placeholder():
    """AsyncOpenAI rejects truly blank keys; vLLM/Ollama accept any non-empty string."""
    provider = OpenAICompatProvider(api_key="", base_url="http://vllm:8000/v1", model="m")
    # The SDK was constructed with api_key="EMPTY"; verify by reading what it stored.
    # We don't introspect AsyncOpenAI internals beyond confirming construction did not raise.
    assert provider._client is not None


def test_verify_tls_true_uses_default_http_client():
    """verify_tls=True (the default) passes http_client=None so the SDK verifies TLS normally."""
    with patch("app.services.providers.openai_provider.AsyncOpenAI") as mock_openai:
        OpenAICompatProvider(api_key="k", base_url="https://vllm:8000/v1", model="m")
    _, kwargs = mock_openai.call_args
    assert kwargs["http_client"] is None


def test_verify_tls_false_injects_non_verifying_http_client():
    """verify_tls=False builds an httpx client with verify=False (for self-signed on-prem
    endpoints) and hands it to AsyncOpenAI, so verification is skipped before auth."""
    sentinel = object()
    with (
        patch("app.services.providers.openai_provider.AsyncOpenAI") as mock_openai,
        patch(
            "app.services.providers.openai_provider.httpx.AsyncClient",
            return_value=sentinel,
        ) as mock_async_client,
    ):
        OpenAICompatProvider(
            api_key="k",
            base_url="https://self-signed:8000/v1",
            model="m",
            verify_tls=False,
        )
    mock_async_client.assert_called_once_with(verify=False)
    _, kwargs = mock_openai.call_args
    assert kwargs["http_client"] is sentinel


def test_ca_cert_verifies_against_bundle_and_takes_precedence():
    """ca_cert builds an SSL context from the bundle and verifies against it
    (fail-closed), overriding verify_tls."""
    client_sentinel = object()
    ctx_sentinel = object()
    with (
        patch("app.services.providers.openai_provider.AsyncOpenAI") as mock_openai,
        patch(
            "app.services.providers.openai_provider.ssl.create_default_context",
            return_value=ctx_sentinel,
        ) as mock_ctx,
        patch(
            "app.services.providers.openai_provider.httpx.AsyncClient",
            return_value=client_sentinel,
        ) as mock_async_client,
    ):
        OpenAICompatProvider(
            api_key="k",
            base_url="https://self-signed:8000/v1",
            model="m",
            verify_tls=True,
            ca_cert="/certs/spark-ca.pem",
        )
    mock_ctx.assert_called_once_with(cafile="/certs/spark-ca.pem")
    mock_async_client.assert_called_once_with(verify=ctx_sentinel)
    _, kwargs = mock_openai.call_args
    assert kwargs["http_client"] is client_sentinel
