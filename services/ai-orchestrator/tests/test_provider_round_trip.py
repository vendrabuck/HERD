"""End-to-end round-trip of tool_use_id through the AIClient loop against a
fake LLMProvider.

The orchestrator's correctness hinges on every ToolUseBlock.id flowing back
into the matching ToolResultBlock.tool_use_id on the next turn. Both
Anthropic and OpenAI reject mismatches, so the loop has zero tolerance for
ID drift. This file drives the loop against a deterministic fake provider
and asserts the ID handoff at each step.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.services.ai_client import AIClient
from app.services.llm_provider import (
    LLMProvider,
    Message,
    ProviderResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


@dataclass
class _CapturedCall:
    system: str
    messages: list[Message]
    tools: list
    tool_choice: object
    max_tokens: int
    timeout_s: float | None


class _ScriptedProvider:
    """A fake LLMProvider that returns a queued list of responses and captures
    every call's request payload for inspection."""

    def __init__(self, responses: list[ProviderResponse]):
        self._responses = list(responses)
        self.calls: list[_CapturedCall] = []

    async def call(self, *, system, messages, tools, tool_choice, max_tokens, timeout_s):
        self.calls.append(
            _CapturedCall(
                system=system,
                messages=list(messages),
                tools=list(tools) if tools is not None else None,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
            )
        )
        if not self._responses:
            raise AssertionError("ScriptedProvider exhausted")
        return self._responses.pop(0)


class _StubDispatcher:
    """Minimal stand-in for ToolDispatcher.

    Returns a deterministic content string per call so the orchestrator builds
    a normal user turn. call_log is the same shape AIClient expects so the
    return tuple's tool_call_records slot is well-formed.
    """

    def __init__(self):
        self.dispatched: list[tuple[str, dict]] = []
        self.call_log: list = []

    async def dispatch(self, name: str, payload: dict):
        self.dispatched.append((name, payload))
        return {"content": f'{{"echoed":"{name}"}}', "is_error": False}


def _conformant_provider(p) -> LLMProvider:
    """Sanity check used in fixtures so a typo in the fake breaks loudly."""
    assert isinstance(p, LLMProvider)
    return p


@pytest.mark.asyncio
async def test_tool_use_id_round_trips_into_next_turn():
    """One tool call: the ID returned by the model must appear verbatim in the
    next turn's ToolResultBlock and as a ToolUseBlock in the assistant message."""
    given_id = "toolu_unique_abc_123"
    provider = _ScriptedProvider(
        [
            ProviderResponse(
                content=[
                    TextBlock(text="checking"),
                    ToolUseBlock(id=given_id, name="get_device", input={"device_id": "d-1"}),
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=10, output_tokens=20),
                raw_model="m",
            ),
            ProviderResponse(
                content=[TextBlock(text="done")],
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=2),
                raw_model="m",
            ),
        ]
    )
    client = AIClient(provider=_conformant_provider(provider), max_tokens=1024)
    dispatcher = _StubDispatcher()

    result = await client.answer_reservation_question_with_tools(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="<reservation>seed</reservation>\n\ndescribe device d-1")],
            )
        ],
        dispatcher=dispatcher,
        max_iterations=4,
    )

    assert result.answer == "done"
    assert result.stop_reason == "end_turn"
    assert result.iteration == 2
    # Aggregated usage across both turns.
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 22

    # Turn 2 was sent with the assistant tool_use + a user ToolResultBlock matching the id.
    second_call_messages = provider.calls[1].messages
    # Walk messages: original user, assistant tool_use, user tool_result.
    assistant_msg = next(m for m in second_call_messages if m.role == "assistant")
    tool_use_blocks = [b for b in assistant_msg.content if isinstance(b, ToolUseBlock)]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0].id == given_id

    user_msgs = [m for m in second_call_messages if m.role == "user"]
    result_msg = user_msgs[-1]
    tool_results = [b for b in result_msg.content if isinstance(b, ToolResultBlock)]
    assert len(tool_results) == 1
    assert tool_results[0].tool_use_id == given_id


@pytest.mark.asyncio
async def test_parallel_tool_calls_preserve_each_id_in_order():
    """Two tool_use blocks in one assistant turn must produce two
    ToolResultBlocks with matching ids in the same order."""
    id_a = "toolu_aaa"
    id_b = "toolu_bbb"
    provider = _ScriptedProvider(
        [
            ProviderResponse(
                content=[
                    ToolUseBlock(id=id_a, name="get_device", input={"device_id": "d-a"}),
                    ToolUseBlock(id=id_b, name="get_device", input={"device_id": "d-b"}),
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=5, output_tokens=5),
                raw_model="m",
            ),
            ProviderResponse(
                content=[TextBlock(text="both fetched")],
                stop_reason="end_turn",
                usage=Usage(input_tokens=2, output_tokens=3),
                raw_model="m",
            ),
        ]
    )
    client = AIClient(provider=provider, max_tokens=1024)
    dispatcher = _StubDispatcher()

    result = await client.answer_reservation_question_with_tools(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="<reservation>seed</reservation>\n\ncompare")],
            )
        ],
        dispatcher=dispatcher,
        max_iterations=4,
    )

    assert result.answer == "both fetched"
    # Both tool calls dispatched in order.
    assert [name for name, _ in dispatcher.dispatched] == ["get_device", "get_device"]
    # ToolResultBlocks echo both ids in the order they were emitted.
    user_msgs = [m for m in provider.calls[1].messages if m.role == "user"]
    result_blocks = [b for b in user_msgs[-1].content if isinstance(b, ToolResultBlock)]
    assert [b.tool_use_id for b in result_blocks] == [id_a, id_b]


@pytest.mark.asyncio
async def test_tool_use_id_round_trips_across_three_turns():
    """Two consecutive tool-call turns: each turn's id must end up matched to
    its own ToolResultBlock; the orchestrator must not cross-wire ids."""
    id_first = "toolu_one"
    id_second = "toolu_two"
    provider = _ScriptedProvider(
        [
            ProviderResponse(
                content=[ToolUseBlock(id=id_first, name="get_device", input={"device_id": "d-1"})],
                stop_reason="tool_use",
                usage=Usage(),
                raw_model="m",
            ),
            ProviderResponse(
                content=[ToolUseBlock(id=id_second, name="get_device", input={"device_id": "d-2"})],
                stop_reason="tool_use",
                usage=Usage(),
                raw_model="m",
            ),
            ProviderResponse(
                content=[TextBlock(text="both gathered")],
                stop_reason="end_turn",
                usage=Usage(),
                raw_model="m",
            ),
        ]
    )
    client = AIClient(provider=provider, max_tokens=1024)
    dispatcher = _StubDispatcher()
    result = await client.answer_reservation_question_with_tools(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="<reservation>seed</reservation>\n\nwalk both")],
            )
        ],
        dispatcher=dispatcher,
        max_iterations=5,
    )
    assert result.answer == "both gathered"
    assert result.iteration == 3

    # Turn 2 must reference id_first (turn 1's id), not id_second.
    turn_2_msgs = provider.calls[1].messages
    turn_2_user_results = [
        b
        for m in turn_2_msgs
        if m.role == "user"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert [b.tool_use_id for b in turn_2_user_results] == [id_first]

    # Turn 3 must reference id_second.
    turn_3_msgs = provider.calls[2].messages
    turn_3_user_results = [
        b
        for m in turn_3_msgs
        if m.role == "user"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    # The last user message in turn 3 is the second tool_result.
    assert turn_3_user_results[-1].tool_use_id == id_second


@pytest.mark.asyncio
async def test_stop_reason_normalization_routes_into_loop():
    """The loop exits on stop_reason != 'tool_use'. A normalized 'end_turn' from
    either provider must take the same exit path."""
    provider = _ScriptedProvider(
        [
            ProviderResponse(
                content=[TextBlock(text="quick answer")],
                stop_reason="end_turn",
                usage=Usage(),
                raw_model="m",
            )
        ]
    )
    client = AIClient(provider=provider, max_tokens=512)
    dispatcher = _StubDispatcher()
    result = await client.answer_reservation_question_with_tools(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="<reservation>seed</reservation>\n\nhi")],
            )
        ],
        dispatcher=dispatcher,
        max_iterations=4,
    )
    assert result.answer == "quick answer"
    assert result.iteration == 1
    # Provider was called exactly once; no tool dispatching occurred.
    assert len(provider.calls) == 1
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_loop_drops_tools_in_iteration_cap_followup_call():
    """When the iteration cap is hit, the final call must be sent with no tools.
    This is the safety hatch that prevents an infinite loop, and breakage shows
    up only as a higher-than-expected token bill or runaway behavior."""
    # max_iterations=1: exactly one looped call (which keeps asking for tools),
    # then the final tools=None followup.
    tool_use_resp = ProviderResponse(
        content=[ToolUseBlock(id="toolu_x", name="get_device", input={"device_id": "d-1"})],
        stop_reason="tool_use",
        usage=Usage(),
        raw_model="m",
    )
    final_resp = ProviderResponse(
        content=[TextBlock(text="ran out of turns")],
        stop_reason="end_turn",
        usage=Usage(),
        raw_model="m",
    )
    provider = _ScriptedProvider([tool_use_resp, final_resp])
    client = AIClient(provider=provider, max_tokens=512)
    dispatcher = _StubDispatcher()
    result = await client.answer_reservation_question_with_tools(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="<reservation>seed</reservation>\n\nloop forever")],
            )
        ],
        dispatcher=dispatcher,
        max_iterations=1,
    )
    assert result.answer == "ran out of turns"
    assert result.stop_reason == "end_turn"
    assert result.iteration == 1
    # The second call (post-cap) must have tools=None.
    assert provider.calls[1].tools is None
