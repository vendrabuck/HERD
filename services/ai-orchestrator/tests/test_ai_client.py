"""Orchestration tests for AIClient.

Targets the AIClient public surface (propose_topology,
suggest_template_identity, answer_reservation_question_with_tools) through a
_FakeProvider that satisfies the LLMProvider Protocol. SDK-translation
correctness lives in test_anthropic_provider.py.
"""

from typing import Any

import pytest
from app.services.ai_client import AIClient, AIError
from app.services.llm_provider import (
    LLMProvider,
    Message,
    ProviderResponse,
    TextBlock,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceTool,
    ToolUseBlock,
    Usage,
)
from app.services.tools import ToolCallRecord


class _FakeProvider:
    """In-test LLMProvider. Returns each response in sequence; records all calls."""

    def __init__(
        self,
        *,
        responses: list[ProviderResponse] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> ProviderResponse:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            raise AssertionError("FakeProvider response sequence exhausted")
        return self._responses.pop(0)


def _resp(
    content: list,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 20,
) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        raw_model="test-model",
    )


def _make_client(provider: LLMProvider, max_tokens: int = 4096) -> AIClient:
    return AIClient(provider=provider, max_tokens=max_tokens)


def _tool_use(name: str, args: dict, block_id: str = "toolu_001") -> ToolUseBlock:
    return ToolUseBlock(id=block_id, name=name, input=args)


def _msgs(seed_block: str, question: str) -> list[Message]:
    """Build the opening messages list the route would persist on first turn:
    seed + wrapped question as a single user message. Matches the Branch 3
    contract where the route owns message construction and AIClient takes the
    pre-built list.
    """
    return [
        Message(
            role="user",
            content=[TextBlock(text=f"{seed_block}\n\n<question>\n{question}\n</question>")],
        )
    ]


# --- propose_topology ---


@pytest.mark.asyncio
async def test_propose_topology_returns_tool_input():
    proposal = {"purpose": "demo", "devices": [], "edges": []}
    provider = _FakeProvider(responses=[_resp([_tool_use("propose_topology", proposal)])])
    client = _make_client(provider)
    out, usage = await client.propose_topology(inventory_block="- Alpha: 1", user_prompt="hi")
    assert out == proposal
    assert (usage.input_tokens, usage.output_tokens) == (10, 20)


@pytest.mark.asyncio
async def test_propose_topology_falls_back_to_text_json_block():
    """If the provider returns a text block (tool_choice not honored), JSON is parsed."""
    payload = '{"purpose":"t","devices":[],"edges":[]}'
    provider = _FakeProvider(responses=[_resp([TextBlock(text=payload)])])
    client = _make_client(provider)
    out, usage = await client.propose_topology(inventory_block="", user_prompt="hi")
    assert out == {"purpose": "t", "devices": [], "edges": []}
    assert (usage.input_tokens, usage.output_tokens) == (10, 20)


@pytest.mark.asyncio
async def test_propose_topology_prefers_tool_use_over_text():
    proposal = {"purpose": "tool-wins", "devices": [], "edges": []}
    provider = _FakeProvider(
        responses=[
            _resp(
                [
                    TextBlock(text='{"purpose":"text","devices":[],"edges":[]}'),
                    _tool_use("propose_topology", proposal),
                ]
            )
        ]
    )
    client = _make_client(provider)
    out, _usage = await client.propose_topology(inventory_block="", user_prompt="hi")
    assert out == proposal


@pytest.mark.asyncio
async def test_propose_topology_raises_when_no_usable_content():
    provider = _FakeProvider(responses=[_resp([])])
    client = _make_client(provider)
    with pytest.raises(AIError):
        await client.propose_topology(inventory_block="", user_prompt="hi")


@pytest.mark.asyncio
async def test_propose_topology_raises_on_malformed_text_json():
    provider = _FakeProvider(responses=[_resp([TextBlock(text="not { json }")])])
    client = _make_client(provider)
    with pytest.raises(AIError):
        await client.propose_topology(inventory_block="", user_prompt="hi")


@pytest.mark.asyncio
async def test_propose_topology_wraps_provider_exceptions_as_aierror():
    """A non-AIError from the provider (rate limit, auth, network, model-not-found)
    is wrapped as AIError at the AIClient boundary, so the routes map it to a clean
    502 instead of an opaque 500. The original exception is preserved as the cause.
    The provider itself stays a thin pass-through (see the provider-level
    test_call_propagates_sdk_exceptions); the error boundary lives here."""

    class _RateLimited(Exception):
        pass

    provider = _FakeProvider(raises=_RateLimited("429"))
    client = _make_client(provider)
    with pytest.raises(AIError) as exc:
        await client.propose_topology(inventory_block="", user_prompt="hi")
    assert isinstance(exc.value.__cause__, _RateLimited)


@pytest.mark.asyncio
async def test_propose_topology_prepends_file_context_when_present():
    provider = _FakeProvider(
        responses=[
            _resp([_tool_use("propose_topology", {"purpose": "p", "devices": [], "edges": []})])
        ]
    )
    client = _make_client(provider)
    await client.propose_topology(
        inventory_block="- Alpha: 1",
        user_prompt="build me a fw",
        file_context="supplementary doc",
    )
    user_msg = provider.calls[0]["messages"][0]
    text = user_msg.content[0].text
    assert "supplementary doc" in text
    assert "=== USER PROMPT ===" in text
    assert "build me a fw" in text


@pytest.mark.asyncio
async def test_propose_topology_omits_file_context_wrapper_when_absent():
    provider = _FakeProvider(
        responses=[
            _resp([_tool_use("propose_topology", {"purpose": "p", "devices": [], "edges": []})])
        ]
    )
    client = _make_client(provider)
    await client.propose_topology(inventory_block="", user_prompt="just this")
    text = provider.calls[0]["messages"][0].content[0].text
    assert text == "just this"


@pytest.mark.asyncio
async def test_propose_topology_forces_tool_choice():
    proposal = {"purpose": "demo", "devices": [], "edges": []}
    provider = _FakeProvider(responses=[_resp([_tool_use("propose_topology", proposal)])])
    client = _make_client(provider)
    await client.propose_topology(inventory_block="", user_prompt="hi")
    tc = provider.calls[0]["tool_choice"]
    assert isinstance(tc, ToolChoiceTool)
    assert tc.name == "propose_topology"


# --- answer_reservation_question_with_tools (tool-use loop) ---


class _FakeDispatcher:
    """Test double for ToolDispatcher."""

    def __init__(self, returns: list[dict] | None = None) -> None:
        self._returns = list(returns or [])
        self.dispatch_calls: list[tuple[str, dict]] = []
        self.call_log: list = []

    async def dispatch(self, name: str, args: dict) -> dict:
        self.dispatch_calls.append((name, args))
        self.call_log.append(
            ToolCallRecord(name=name, arguments_summary=str(args)[:50], duration_ms=1)
        )
        if self._returns:
            return self._returns.pop(0)
        return {"content": '{"ok":true}', "is_error": False}


@pytest.mark.asyncio
async def test_tool_loop_zero_tool_calls_returns_text():
    provider = _FakeProvider(
        responses=[_resp([TextBlock(text="Reservation ends at 17:00.")], stop_reason="end_turn")]
    )
    client = _make_client(provider)
    dispatcher = _FakeDispatcher()
    turn = await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation>\n  id: r1\n</reservation>", "When does my reservation end?"),
        dispatcher=dispatcher,
    )
    answer, usage, stop_reason, iters = turn.answer, turn.usage, turn.stop_reason, turn.iteration
    calls = dispatcher.call_log
    assert answer == "Reservation ends at 17:00."
    assert stop_reason == "end_turn"
    assert iters == 1
    assert calls == []
    assert len(turn.segments) == 1
    assert turn.segments[0].tool_result_blocks == []
    assert dispatcher.dispatch_calls == []
    assert len(provider.calls) == 1
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20


@pytest.mark.asyncio
async def test_tool_loop_one_tool_call_then_text():
    provider = _FakeProvider(
        responses=[
            _resp(
                [_tool_use("get_device", {"device_id": "dev-a"}, "toolu_abc")],
                stop_reason="tool_use",
                input_tokens=12,
                output_tokens=8,
            ),
            _resp(
                [TextBlock(text="Device fw-a is RESERVED.")],
                stop_reason="end_turn",
                input_tokens=30,
                output_tokens=15,
            ),
        ]
    )
    client = _make_client(provider)
    dispatcher = _FakeDispatcher(
        returns=[{"content": '{"name":"fw-a","status":"RESERVED"}', "is_error": False}]
    )
    turn = await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation>...</reservation>", "What is the status of dev-a?"),
        dispatcher=dispatcher,
    )
    answer, usage, iters = turn.answer, turn.usage, turn.iteration
    calls = dispatcher.call_log
    assert "fw-a" in answer
    assert iters == 2
    assert len(calls) == 1
    assert calls[0].name == "get_device"
    # Branch 3: per-iteration segments are surfaced for persistence.
    assert len(turn.segments) == 2
    assert turn.segments[0].tool_result_blocks  # tool iteration
    assert turn.segments[1].tool_result_blocks == []  # final text iteration
    assert dispatcher.dispatch_calls == [("get_device", {"device_id": "dev-a"})]
    assert usage.input_tokens == 42
    assert usage.output_tokens == 23
    # The follow-up call's last message is a user-role ToolResultBlock list echoing
    # the tool_use_id verbatim.
    follow_up_messages = provider.calls[1]["messages"]
    last = follow_up_messages[-1]
    assert last.role == "user"
    assert len(last.content) == 1
    result_block = last.content[0]
    from app.services.llm_provider import ToolResultBlock

    assert isinstance(result_block, ToolResultBlock)
    assert result_block.tool_use_id == "toolu_abc"
    assert result_block.is_error is False


@pytest.mark.asyncio
async def test_tool_loop_parallel_tool_calls_in_one_turn():
    """Two tool_use blocks in one assistant turn dispatch in parallel; both
    results land in ONE user-message ToolResultBlock list."""
    provider = _FakeProvider(
        responses=[
            _resp(
                [
                    _tool_use("get_device", {"device_id": "dev-a"}, "toolu_1"),
                    _tool_use("get_device", {"device_id": "dev-b"}, "toolu_2"),
                ],
                stop_reason="tool_use",
            ),
            _resp([TextBlock(text="Both reserved.")], stop_reason="end_turn"),
        ]
    )
    client = _make_client(provider)
    dispatcher = _FakeDispatcher(
        returns=[
            {"content": '{"name":"fw-a"}', "is_error": False},
            {"content": '{"name":"fw-b"}', "is_error": False},
        ]
    )
    turn = await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation/>", "status of dev-a and dev-b?"),
        dispatcher=dispatcher,
    )
    answer, iters = turn.answer, turn.iteration
    calls = dispatcher.call_log
    assert iters == 2
    assert answer == "Both reserved."
    assert len(calls) == 2
    from app.services.llm_provider import ToolResultBlock

    tool_results = provider.calls[1]["messages"][-1].content
    assert all(isinstance(b, ToolResultBlock) for b in tool_results)
    assert {b.tool_use_id for b in tool_results} == {"toolu_1", "toolu_2"}


@pytest.mark.asyncio
async def test_tool_loop_iteration_cap_forces_final_answer_without_tools():
    responses = [
        _resp(
            [_tool_use("get_device", {"device_id": f"d{i}"}, f"toolu_{i}")],
            stop_reason="tool_use",
        )
        for i in range(3)
    ] + [_resp([TextBlock(text="Best guess answer.")], stop_reason="end_turn")]
    provider = _FakeProvider(responses=responses)
    client = _make_client(provider)
    dispatcher = _FakeDispatcher()
    turn = await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation/>", "Why is everything broken?"),
        dispatcher=dispatcher,
        max_iterations=3,
    )
    answer, iters = turn.answer, turn.iteration
    assert answer == "Best guess answer."
    assert iters == 3
    assert len(provider.calls) == 4
    # The final call must have tools=None.
    assert provider.calls[3]["tools"] is None
    # Last message is a user nudge from the post-cap branch.
    last_msg = provider.calls[3]["messages"][-1]
    assert last_msg.role == "user"
    assert "exhausted" in last_msg.content[0].text.lower()


@pytest.mark.asyncio
async def test_tool_loop_iteration_cap_with_no_text_raises_aierror():
    """Pathological: forced final call returns nothing usable."""
    responses = [
        _resp(
            [_tool_use("get_device", {"device_id": "d"}, "toolu_x")],
            stop_reason="tool_use",
        ),
        _resp([], stop_reason="end_turn"),
    ]
    provider = _FakeProvider(responses=responses)
    client = _make_client(provider)
    dispatcher = _FakeDispatcher()
    with pytest.raises(AIError) as exc:
        await client.answer_reservation_question_with_tools(
            messages=_msgs("<reservation/>", "?"),
            dispatcher=dispatcher,
            max_iterations=1,
        )
    assert "exhausted" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_tool_loop_propagates_provider_timeout_as_aierror():
    """Provider raises AIError on its own timeout; AIClient propagates."""
    provider = _FakeProvider(raises=AIError("AI call exceeded 0.05s"))
    client = _make_client(provider)
    with pytest.raises(AIError) as exc:
        await client.answer_reservation_question_with_tools(
            messages=_msgs("<reservation/>", "?"),
            dispatcher=_FakeDispatcher(),
            per_call_timeout_s=0.05,
        )
    assert "exceeded" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_tool_loop_wraps_provider_sdk_error_as_aierror():
    """A raw SDK error on the assistant tool loop (e.g. the model id is no longer
    served, as in the reservation-assistant 500) is wrapped as AIError so the route
    returns 502, not an opaque 500. This is the regression this fix closes."""

    class _ModelNotFound(Exception):
        pass

    provider = _FakeProvider(raises=_ModelNotFound("unknown model"))
    client = _make_client(provider)
    with pytest.raises(AIError) as exc:
        await client.answer_reservation_question_with_tools(
            messages=_msgs("<reservation/>", "?"),
            dispatcher=_FakeDispatcher(),
            per_call_timeout_s=None,
        )
    assert isinstance(exc.value.__cause__, _ModelNotFound)


@pytest.mark.asyncio
async def test_tool_loop_passes_timeout_to_provider():
    """Every loop call carries the per_call_timeout_s value through to the provider."""
    provider = _FakeProvider(responses=[_resp([TextBlock(text="ok")], stop_reason="end_turn")])
    client = _make_client(provider)
    await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation/>", "?"),
        dispatcher=_FakeDispatcher(),
        per_call_timeout_s=12.5,
    )
    assert provider.calls[0]["timeout_s"] == 12.5


@pytest.mark.asyncio
async def test_tool_loop_uses_auto_tool_choice_during_loop():
    provider = _FakeProvider(responses=[_resp([TextBlock(text="ok")], stop_reason="end_turn")])
    client = _make_client(provider)
    await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation/>", "?"), dispatcher=_FakeDispatcher()
    )
    assert isinstance(provider.calls[0]["tool_choice"], ToolChoiceAuto)


@pytest.mark.asyncio
async def test_tool_loop_exits_when_stop_reason_is_max_tokens_with_text():
    provider = _FakeProvider(
        responses=[_resp([TextBlock(text="Partial answer truncated.")], stop_reason="max_tokens")]
    )
    client = _make_client(provider)
    turn = await client.answer_reservation_question_with_tools(
        messages=_msgs("<reservation/>", "long question"),
        dispatcher=_FakeDispatcher(),
    )
    answer, stop_reason, iters = turn.answer, turn.stop_reason, turn.iteration
    assert answer.startswith("Partial answer")
    assert stop_reason == "max_tokens"
    assert iters == 1


@pytest.mark.asyncio
async def test_tool_loop_question_text_not_in_log_records(caplog):
    import logging as _logging

    secret_q = "TOPSECRET-question-12345"
    provider = _FakeProvider(responses=[_resp([TextBlock(text="ok")], stop_reason="end_turn")])
    client = _make_client(provider)
    with caplog.at_level(_logging.INFO):
        await client.answer_reservation_question_with_tools(
            messages=_msgs("<reservation/>", secret_q),
            dispatcher=_FakeDispatcher(),
        )
    for record in caplog.records:
        assert secret_q not in record.getMessage()
        for value in record.__dict__.values():
            assert secret_q not in str(value)


# --- suggest_template_identity ---


@pytest.mark.asyncio
async def test_suggest_template_identity_returns_parsed_tool_input():
    proposed = {
        "vendor": "Juniper Networks",
        "model": "EX4300",
        "part_number": None,
        "confidence": "high",
        "reasoning": "EX4300 is a Juniper Networks switch.",
    }
    provider = _FakeProvider(responses=[_resp([_tool_use("suggest_identity", proposed)])])
    client = _make_client(provider)
    out, usage = await client.suggest_template_identity(
        name="EX4300",
        description="Border firewall template",
        sections=None,
    )
    assert out["vendor"] == "Juniper Networks"
    assert out["model"] == "EX4300"
    assert out["confidence"] == "high"
    assert (usage.input_tokens, usage.output_tokens) == (10, 20)


@pytest.mark.asyncio
async def test_suggest_template_identity_handles_low_confidence_path():
    proposed = {
        "vendor": "Generic",
        "model": "Generic Firewall",
        "confidence": "low",
        "reasoning": "Template name is generic; no vendor inferred.",
    }
    provider = _FakeProvider(responses=[_resp([_tool_use("suggest_identity", proposed)])])
    client = _make_client(provider)
    out, _usage = await client.suggest_template_identity(name="Generic Firewall")
    assert out["confidence"] == "low"
    assert out["part_number"] is None


@pytest.mark.asyncio
async def test_suggest_template_identity_raises_when_no_tool_use():
    provider = _FakeProvider(responses=[_resp([TextBlock(text="Just text, no tool call.")])])
    client = _make_client(provider)
    with pytest.raises(AIError):
        await client.suggest_template_identity(name="X")


@pytest.mark.asyncio
async def test_suggest_template_identity_forces_tool_choice():
    proposed = {
        "vendor": "Cisco",
        "model": "Catalyst 9300",
        "part_number": None,
        "confidence": "high",
        "reasoning": "Catalyst 9300 is a Cisco switch.",
    }
    provider = _FakeProvider(responses=[_resp([_tool_use("suggest_identity", proposed)])])
    client = _make_client(provider)
    await client.suggest_template_identity(name="Catalyst 9300")
    tc = provider.calls[0]["tool_choice"]
    assert isinstance(tc, ToolChoiceTool)
    assert tc.name == "suggest_identity"


# --- ToolChoiceNone smoke (consumed only by the post-cap branch indirectly) ---


def test_tool_choice_none_is_instantiable():
    """Smoke test: ToolChoiceNone is wired into the algebra (even if the loop
    today never passes it; left here so future write-tool iterations have a
    test scaffolding hook)."""
    assert ToolChoiceNone().kind == "none"


# --- answer_reservation_question_streaming (SSE tool loop) ---

from app.services.llm_provider import StreamDone, TextDelta  # noqa: E402


class _StreamingFakeProvider:
    """LLMProvider with call_stream. Each turn is (deltas, final_response):
    yields a TextDelta per delta, then a StreamDone with the final response."""

    def __init__(self, turns: list[tuple[list[str], ProviderResponse]]) -> None:
        self._turns = list(turns)
        self.stream_calls = 0

    async def call(self, **kwargs: Any) -> ProviderResponse:
        raise AssertionError("streaming path must use call_stream")

    async def call_stream(self, **kwargs: Any):
        self.stream_calls += 1
        if not self._turns:
            raise AssertionError("streaming turn sequence exhausted")
        deltas, final = self._turns.pop(0)
        for d in deltas:
            yield TextDelta(text=d)
        yield StreamDone(response=final)


async def _collect_stream(client, **kwargs):
    return [ev async for ev in client.answer_reservation_question_streaming(**kwargs)]


@pytest.mark.asyncio
async def test_streaming_single_turn_streams_tokens_then_done():
    final = _resp([TextBlock(text="Reservation ends 17:00.")])
    provider = _StreamingFakeProvider(turns=[(["Reser", "vation ", "ends 17:00."], final)])
    client = _make_client(provider)
    events = await _collect_stream(
        client,
        messages=_msgs("<reservation>r1</reservation>", "When does it end?"),
        dispatcher=_FakeDispatcher(),
    )
    types = [e.type for e in events]
    # analyzing status, three live tokens, then done.
    assert types == ["status", "token", "token", "token", "done"]
    assert [e.text for e in events if e.type == "token"] == ["Reser", "vation ", "ends 17:00."]
    done = events[-1]
    assert done.result.answer == "Reservation ends 17:00."
    assert done.result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_streaming_tool_turn_marks_interim_then_final_answer():
    """A tool turn that emits narration tokens before the tool_use must flag the
    following status interim=True so the client discards the provisional tokens;
    the final turn's tokens are the real answer."""
    provider = _StreamingFakeProvider(
        turns=[
            (
                ["Let me check"],
                _resp(
                    [TextBlock(text="Let me check"), _tool_use("list_ports", {})],
                    stop_reason="tool_use",
                ),
            ),
            (["eth1/6 ", "is down."], _resp([TextBlock(text="eth1/6 is down.")])),
        ]
    )
    client = _make_client(provider)
    dispatcher = _FakeDispatcher()
    events = await _collect_stream(
        client,
        messages=_msgs("<reservation>r1</reservation>", "What's wrong?"),
        dispatcher=dispatcher,
    )
    # The tool-turn status carries interim=True and the tool name.
    statuses = [e for e in events if e.type == "status"]
    tool_status = [s for s in statuses if s.tools]
    assert tool_status and tool_status[0].tools == ["list_ports"]
    assert tool_status[0].interim is True
    # The dispatcher ran the tool, and the final answer is the second turn's text.
    assert dispatcher.dispatch_calls == [("list_ports", {})]
    done = events[-1]
    assert done.type == "done"
    assert done.result.answer == "eth1/6 is down."
    # Final-answer tokens streamed live (the second turn's deltas).
    final_tokens = [e.text for e in events if e.type == "token"]
    assert "eth1/6 " in final_tokens and "is down." in final_tokens


@pytest.mark.asyncio
async def test_streaming_falls_back_when_provider_lacks_call_stream():
    """A provider without call_stream (e.g. openai_compat today) still works:
    the buffered loop runs and its answer is emitted as token(s) + done."""
    provider = _FakeProvider(responses=[_resp([TextBlock(text="Buffered answer.")])])
    client = _make_client(provider)
    events = await _collect_stream(
        client,
        messages=_msgs("<reservation>r1</reservation>", "Q?"),
        dispatcher=_FakeDispatcher(),
    )
    assert events[-1].type == "done"
    assert events[-1].result.answer == "Buffered answer."
    assert any(e.type == "token" and e.text == "Buffered answer." for e in events)


@pytest.mark.asyncio
async def test_streaming_empty_final_text_raises():
    provider = _StreamingFakeProvider(turns=[([], _resp([], stop_reason="end_turn"))])
    client = _make_client(provider)
    with pytest.raises(AIError, match="no text content"):
        await _collect_stream(
            client,
            messages=_msgs("<reservation>r1</reservation>", "Q?"),
            dispatcher=_FakeDispatcher(),
        )


class _StreamingCapFakeProvider:
    """call_stream always returns a tool_use turn, so the streaming tool loop
    never returns early and runs to its iteration cap; call serves the forced
    buffered close the cap path makes with tools disabled (the streaming
    _StreamingFakeProvider.call raises, so it cannot exercise this). Records the
    buffered call kwargs so a test can assert tools=None and the nudge message.
    """

    def __init__(self, *, final: ProviderResponse) -> None:
        self._final = final
        self.stream_calls = 0
        self.buffered_calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> ProviderResponse:
        self.buffered_calls.append(kwargs)
        return self._final

    async def call_stream(self, **kwargs: Any):
        self.stream_calls += 1
        i = self.stream_calls
        yield StreamDone(
            response=_resp(
                [_tool_use("get_device", {"device_id": f"d{i}"}, f"toolu_{i}")],
                stop_reason="tool_use",
            )
        )


@pytest.mark.asyncio
async def test_streaming_iteration_cap_forces_buffered_final_answer():
    """Streaming twin of test_tool_loop_iteration_cap_forces_final_answer_without_tools.

    Every streamed turn is a tool_use turn, so the loop exhausts its iteration
    budget and forces a final buffered answer with tools disabled. The forced
    close streams its text as token(s) and the done event carries the answer.
    """
    provider = _StreamingCapFakeProvider(
        final=_resp([TextBlock(text="Best guess answer.")], stop_reason="end_turn"),
    )
    client = _make_client(provider)
    dispatcher = _FakeDispatcher()
    events = await _collect_stream(
        client,
        messages=_msgs("<reservation/>", "Why is everything broken?"),
        dispatcher=dispatcher,
        max_iterations=3,
    )
    # Three streamed tool turns, then exactly one forced buffered close.
    assert provider.stream_calls == 3
    assert len(provider.buffered_calls) == 1
    # The forced close disables tools and nudges with an "exhausted" user message.
    assert provider.buffered_calls[0]["tools"] is None
    nudge = provider.buffered_calls[0]["messages"][-1]
    assert nudge.role == "user"
    assert "exhausted" in nudge.content[0].text.lower()
    # The forced answer flushes as token(s) and the done event carries it.
    done = events[-1]
    assert done.type == "done"
    assert done.result.answer == "Best guess answer."
    assert done.result.iteration == 3
    assert any(e.type == "token" and e.text == "Best guess answer." for e in events)


@pytest.mark.asyncio
async def test_streaming_iteration_cap_no_text_raises_aierror():
    """Streaming twin of test_tool_loop_iteration_cap_with_no_text_raises_aierror.

    The forced buffered close returns no usable text, so the streaming path
    raises AIError naming the exhausted budget rather than emitting a done event.
    """
    provider = _StreamingCapFakeProvider(final=_resp([], stop_reason="end_turn"))
    client = _make_client(provider)
    with pytest.raises(AIError, match="exhausted"):
        await _collect_stream(
            client,
            messages=_msgs("<reservation/>", "?"),
            dispatcher=_FakeDispatcher(),
            max_iterations=1,
        )
