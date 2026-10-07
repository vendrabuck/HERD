"""Issue #1037: every streamed assistant turn ends in exactly one terminal
frame, `done` or `error`, whatever raises.

Before the fix the stream route handled only a timeout, an unreachable
provider, and an AIError. Any other exception, from inside the tool loop or
from persisting a finished turn, escaped the generator after the HTTP status
was already sent, so the client saw the stream close with no terminal frame.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from app.main import app
from app.routes import reservation_assistant as route_module
from app.routes.reservation_assistant import ASSISTANT_CALL_FAILED_DETAIL
from app.services.ai_client import (
    INCOMPLETE_AFTER_TOOLS_ANSWER,
    AssistantDone,
    AssistantStatus,
    AssistantToken,
    AssistantTurnResult,
    TurnSegment,
)
from app.services.llm_provider import TextBlock
from httpx import ASGITransport, AsyncClient

from tests.test_reservation_assistant import (  # noqa: F401  (autouse fixtures)
    _assert_no_orphan_trailing_user,
    _load_db_messages,
    _override_ai,
    _override_seed,
    _override_streaming_ai,
    _parse_sse,
    _pre_raise_write_tool_state,
    _start_conversation,
    _stream_url,
    _url,
    _user_token,
    set_api_key,
    setup_db,
)

_SECRET_TEXT = "postgres://admin:hunter2@db/internal"


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _terminal_events(events):
    return [(e, d) for e, d in events if e in ("done", "error")]


class _RaiseMidStreamAI:
    """Streams a status and a token, then raises an exception class the
    route has no dedicated handler for."""

    def __init__(self, exc: Exception, *, land_write: bool = False):
        self._exc = exc
        self._land_write = land_write

    async def answer_reservation_question_streaming(
        self,
        *,
        messages,
        dispatcher,
        max_iterations=8,
        per_call_timeout_s=20.0,
        segments=None,
        usage=None,
    ):
        if self._land_write:
            tool_calls, side_effects, done_segments = _pre_raise_write_tool_state(
                uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            )
            dispatcher.call_log.extend(tool_calls)
            dispatcher.side_effects.extend(side_effects)
            segments.extend(done_segments)
        yield AssistantStatus(message="analyzing")
        yield AssistantToken(text="partial ")
        raise self._exc


def _install(ai) -> None:
    from app.services.ai_client import get_ai_client

    app.dependency_overrides[get_ai_client] = lambda: ai


async def test_unexpected_exception_mid_stream_ends_in_one_error_frame(async_client):
    _override_seed()
    _install(_RaiseMidStreamAI(RuntimeError(_SECRET_TEXT)))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q"}, headers=headers)

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["status", "token", "error"]
    assert events[-1][1] == {"message": ASSISTANT_CALL_FAILED_DETAIL}
    assert ASSISTANT_CALL_FAILED_DETAIL == "Assistant call failed"
    assert _SECRET_TEXT not in resp.text


async def test_unexpected_exception_rolls_back_and_next_turn_succeeds(async_client):
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        conv_id = await _start_conversation(client, headers)
        _install(_RaiseMidStreamAI(KeyError("boom")))
        failed = await client.post(
            _stream_url(),
            json={"question": "turn two", "conversation_id": conv_id},
            headers=headers,
        )
        assert len(_terminal_events(_parse_sse(failed.text))) == 1
        rows = await _load_db_messages(conv_id)
        _assert_no_orphan_trailing_user(rows)
        assert [r for r, _ in rows] == ["USER", "ASSISTANT"]

        _override_ai(answer="turn three answer")
        recovered = await client.post(
            _url(),
            json={"question": "turn three", "conversation_id": conv_id},
            headers=headers,
        )
    assert recovered.status_code == 200, recovered.text


async def test_unexpected_exception_after_a_landed_write_keeps_the_turn(async_client):
    """The #871 carve-out holds for an unexpected exception: the write landed,
    so the turn is kept and ends in `done` carrying `incomplete`."""
    _override_seed()
    _install(_RaiseMidStreamAI(ValueError(_SECRET_TEXT), land_write=True))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "apply"}, headers=headers)

    events = _parse_sse(resp.text)
    terminal = _terminal_events(events)
    assert [e for e, _ in terminal] == ["done"]
    data = terminal[0][1]
    assert data["answer"] == INCOMPLETE_AFTER_TOOLS_ANSWER
    assert data["incomplete"] == "ai_error"
    assert data["stop_reason"] == "incomplete"
    assert data["pending_apply"] is not None
    assert _SECRET_TEXT not in resp.text
    rows = await _load_db_messages(data["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


async def test_persist_failure_after_a_finished_answer_ends_in_one_error_frame(
    async_client, monkeypatch
):
    """The issue's trigger: the model answered, then saving the turn failed
    (a database blip). The stream must still end in exactly one `error`."""

    async def failing_persist(**_kwargs):
        raise ConnectionResetError(_SECRET_TEXT)

    monkeypatch.setattr(route_module, "_persist_turn", failing_persist)
    _override_seed()
    _override_streaming_ai(
        [
            AssistantToken(text="eth1/6 is down."),
            AssistantDone(
                result=AssistantTurnResult(
                    answer="eth1/6 is down.",
                    usage=SimpleNamespace(input_tokens=3, output_tokens=4),
                    stop_reason="end_turn",
                    iteration=1,
                    segments=[TurnSegment(assistant_blocks=[TextBlock(text="eth1/6 is down.")])],
                )
            ),
        ]
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q"}, headers=headers)

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["token", "error"]
    assert events[-1][1] == {"message": ASSISTANT_CALL_FAILED_DETAIL}
    assert _SECRET_TEXT not in resp.text


async def test_failure_inside_a_handler_still_ends_in_one_error_frame(async_client, monkeypatch):
    """An exception raised BY one of the named handlers (saving an incomplete
    turn after a timeout) is caught by the outer guard: one `error` frame."""

    async def failing_finalize(**_kwargs):
        raise OSError(_SECRET_TEXT)

    monkeypatch.setattr(route_module, "_finalize_incomplete_turn", failing_finalize)
    _override_seed()
    _override_streaming_ai([], raises=TimeoutError("deadline"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q"}, headers=headers)

    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["error"]
    assert events[0][1] == {"message": ASSISTANT_CALL_FAILED_DETAIL}
    assert _SECRET_TEXT not in json.dumps(events)
