"""Coverage for the provider-neutral type layer in llm_provider.py.

The Protocol itself has no runtime behavior, but the dataclasses, the Usage
accumulator, and the cross-provider tool_use_id round-trip invariants are
load-bearing for the orchestrator loop. ROADMAP #18 introduced these as
the seam between AIClient and any concrete LLMProvider; this file guards
the invariants that AIClient relies on.
"""

import pytest
from app.services.ai_client import _tool_definition_to_schema
from app.services.llm_provider import (
    AIError,
    LLMProvider,
    Message,
    ProviderResponse,
    TextBlock,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)

# --- Usage accumulator ---


def test_usage_default_init_is_zeroed():
    u = Usage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0


def test_usage_add_accumulates_both_fields():
    u = Usage(input_tokens=5, output_tokens=10)
    u.add(Usage(input_tokens=3, output_tokens=7))
    assert u.input_tokens == 8
    assert u.output_tokens == 17


def test_usage_add_self_doubles_in_place():
    u = Usage(input_tokens=4, output_tokens=8)
    u.add(u)
    assert u.input_tokens == 8
    assert u.output_tokens == 16


def test_usage_is_mutable_for_loop_accumulation():
    """The orchestrator relies on Usage being mutable; freezing it would silently
    break the multi-turn accumulator in answer_reservation_question_with_tools."""
    u = Usage()
    u.input_tokens = 100
    u.output_tokens = 200
    assert u.input_tokens == 100
    assert u.output_tokens == 200


def test_usage_multiple_adds_accumulate():
    """Three turns of accumulation, matching the assistant tool-use loop."""
    u = Usage()
    for i_t, o_t in [(10, 20), (5, 15), (1, 2)]:
        u.add(Usage(input_tokens=i_t, output_tokens=o_t))
    assert u.input_tokens == 16
    assert u.output_tokens == 37


# --- Content block invariants ---


def test_textblock_is_frozen():
    b = TextBlock(text="hello")
    with pytest.raises(Exception):
        b.text = "mutated"  # type: ignore[misc]


def test_tooluseblock_is_frozen():
    b = ToolUseBlock(id="t1", name="x", input={})
    with pytest.raises(Exception):
        b.id = "t2"  # type: ignore[misc]


def test_toolresultblock_default_is_error_is_false():
    """is_error defaulting to False is part of the public contract; AIClient
    constructs ToolResultBlock with just (tool_use_id, content) in the happy path."""
    b = ToolResultBlock(tool_use_id="t1", content="ok")
    assert b.is_error is False


def test_toolresultblock_explicit_is_error_true_preserved():
    b = ToolResultBlock(tool_use_id="t1", content="oops", is_error=True)
    assert b.is_error is True


def test_block_type_literals_are_set():
    """The 'type' literal field is what downstream serializers key off; verify
    each variant declares the expected value."""
    assert TextBlock(text="x").type == "text"
    assert ToolUseBlock(id="t", name="n", input={}).type == "tool_use"
    assert ToolResultBlock(tool_use_id="t", content="c").type == "tool_result"


# --- Tool choice tagged-union variants ---


def test_tool_choice_auto_default_kind():
    assert ToolChoiceAuto().kind == "auto"


def test_tool_choice_none_default_kind():
    assert ToolChoiceNone().kind == "none"


def test_tool_choice_required_default_kind():
    assert ToolChoiceRequired().kind == "required"


def test_tool_choice_tool_carries_name():
    c = ToolChoiceTool(name="get_device")
    assert c.kind == "tool"
    assert c.name == "get_device"


# --- ToolSchema and adapter ---


def test_tool_schema_is_frozen():
    s = ToolSchema(name="t", description="d", input_schema={})
    with pytest.raises(Exception):
        s.name = "other"  # type: ignore[misc]


def test_tool_definition_to_schema_one_to_one_mapping():
    """TOOL_DEFINITIONS dicts must round-trip into ToolSchema instances without
    field loss. This is a one-line shim today but is exactly the kind of
    silent rename that breaks the loop with no exception."""
    t = {
        "name": "get_device",
        "description": "fetch device fields",
        "input_schema": {"type": "object", "properties": {"device_id": {"type": "string"}}},
    }
    schema = _tool_definition_to_schema(t)
    assert isinstance(schema, ToolSchema)
    assert schema.name == "get_device"
    assert schema.description == "fetch device fields"
    assert schema.input_schema == t["input_schema"]


# --- Message and ProviderResponse ---


def test_message_is_frozen():
    m = Message(role="user", content=[TextBlock(text="hi")])
    with pytest.raises(Exception):
        m.role = "assistant"  # type: ignore[misc]


def test_provider_response_records_raw_model_for_logging():
    """raw_model is what the orchestrator logs as the actual model used. It must
    survive into the response unchanged."""
    resp = ProviderResponse(
        content=[TextBlock(text="ok")],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=2),
        raw_model="claude-opus-4-7-20250101",
    )
    assert resp.raw_model == "claude-opus-4-7-20250101"


# --- Protocol conformance ---


def test_llm_provider_protocol_is_runtime_checkable():
    """The orchestrator factory swaps providers based on settings. Even though
    duck typing is the actual check, runtime_checkable means tests can use
    isinstance() to assert wiring."""

    class _GoodProvider:
        async def call(self, *, system, messages, tools, tool_choice, max_tokens, timeout_s):
            return ProviderResponse(
                content=[],
                stop_reason="end_turn",
                usage=Usage(),
                raw_model="x",
            )

    class _BadProvider:
        # No 'call' method.
        pass

    assert isinstance(_GoodProvider(), LLMProvider)
    assert not isinstance(_BadProvider(), LLMProvider)


# --- AIError surfacing ---


def test_aierror_is_subclass_of_exception():
    """Routes catch AIError specifically and translate to HTTP 502. If the
    class accidentally inherits from BaseException instead, the catch breaks
    silently."""
    assert issubclass(AIError, Exception)


def test_aierror_carries_message():
    err = AIError("model returned no usable content")
    assert "no usable content" in str(err)
