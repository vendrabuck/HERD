"""Issue #946: the streaming turn must end with exactly one terminal frame
however the deadline lands, including while the generator is suspended at a
yield because the client (or the middleware) is slow to take a frame.

The tests drive the app through raw ASGI with a `send` that stalls on a chosen
message, which holds the response task at the route's yield past the (very
short) overall deadline.
"""

import asyncio
import json

import pytest
from app import config as config_module
from app.main import app
from app.services.ai_client import (
    INCOMPLETE_AFTER_TOOLS_ANSWER,
    AssistantStatus,
    AssistantToken,
    get_ai_client,
)
from starlette.responses import StreamingResponse

from tests.test_reservation_assistant import (  # noqa: F401  (autouse fixtures)
    _GAP_DEVICE_ID,
    _GAP_JOB_ID,
    _assert_no_orphan_trailing_user,
    _GatherGapFakeDispatcher,
    _load_db_messages,
    _override_seed,
    _parse_sse,
    _stream_url,
    _TestSessionLocal,
    _user_token,
    set_api_key,
    setup_db,
)

DEADLINE_S = 0.05
STALL_S = 0.3


class _LandedThenStallStreamAI:
    """Lands a write tool BEFORE the first frame (so the outcome does not depend
    on task scheduling), then emits a status and two tokens and hangs until the
    overall deadline fires. Two tokens because BaseHTTPMiddleware hands frames to
    the client through a rendezvous stream: a stall on frame N holds the route at
    the yield of frame N+1."""

    def __init__(self):
        self.closed = False

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
        try:
            await dispatcher.dispatch("schedule_config_apply", {"device_id": str(_GAP_DEVICE_ID)})
            yield AssistantStatus(message="running tools", tools=["schedule_config_apply"])
            yield AssistantToken(text="first")
            yield AssistantToken(text="second")
            await asyncio.sleep(999)
        finally:
            self.closed = True


class _NoToolStallStreamAI:
    """Same shape with no write tool landed: the timeout must be an `error`."""

    def __init__(self):
        self.closed = False

    async def answer_reservation_question_streaming(self, **_kwargs):
        try:
            yield AssistantStatus(message="analyzing", tools=[])
            yield AssistantToken(text="first")
            yield AssistantToken(text="second")
            await asyncio.sleep(999)
        finally:
            self.closed = True


async def _drive(stall_when):
    """POST to the stream endpoint over raw ASGI; `stall_when(msg, body_index)`
    returns True for the send call that should sleep STALL_S. Returns SSE events."""
    body = json.dumps({"question": "apply it"}).encode()
    path = _stream_url()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("c", 1),
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {_user_token()}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    chunks: list[bytes] = []
    body_index = 0

    async def send(msg):
        nonlocal body_index
        index = body_index
        if msg["type"] == "http.response.body" and msg.get("body"):
            body_index += 1
        if stall_when(msg, index):
            await asyncio.sleep(STALL_S)
        if msg["type"] == "http.response.body" and msg.get("body"):
            chunks.append(msg["body"])

    await asyncio.wait_for(app(scope, receive, send), 5)
    return _parse_sse(b"".join(chunks).decode())


def _at_start(msg, _i):
    return msg["type"] == "http.response.start"


def _first_chunk(msg, i):
    return msg["type"] == "http.response.body" and msg.get("body") and i == 0


def _chunk_after_token(msg, _i):
    return msg["type"] == "http.response.body" and b'"first"' in (msg.get("body") or b"")


STALLS = [
    pytest.param(_at_start, id="stall-at-response-start"),
    pytest.param(_first_chunk, id="stall-on-first-chunk"),
    pytest.param(_chunk_after_token, id="stall-on-chunk-after-token"),
]


def _terminal(events):
    return [e for e in events if e[0] in ("done", "error")]


@pytest.mark.parametrize("stall_when", STALLS)
async def test_stalled_client_with_landed_tool_still_gets_one_done(monkeypatch, stall_when):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", DEADLINE_S)
    monkeypatch.setattr("app.routes.reservation_assistant.ToolDispatcher", _GatherGapFakeDispatcher)
    _override_seed()
    stub = _LandedThenStallStreamAI()
    app.dependency_overrides[get_ai_client] = lambda: stub

    events = await _drive(stall_when)

    assert len(_terminal(events)) == 1, events
    assert events[-1][0] == "done", events
    data = events[-1][1]
    assert data["incomplete"] == "timeout"
    assert data["stop_reason"] == "incomplete"
    assert data["pending_apply"]["job_id"] == str(_GAP_JOB_ID)
    assert data["answer"].startswith(INCOMPLETE_AFTER_TOOLS_ANSWER)
    assert "schedule_config_apply" in data["answer"]
    assert stub.closed, "inner generator was not closed"

    rows = await _load_db_messages(data["conversation_id"])
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT"]
    assert "schedule_config_apply" in rows[-1][1][0]["text"]


@pytest.mark.parametrize("stall_when", STALLS)
async def test_stalled_client_with_no_tool_gets_one_timeout_error(monkeypatch, stall_when):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", DEADLINE_S)
    _override_seed()
    stub = _NoToolStallStreamAI()
    app.dependency_overrides[get_ai_client] = lambda: stub

    events = await _drive(stall_when)

    assert len(_terminal(events)) == 1, events
    assert events[-1][0] == "error", events
    assert events[-1][1]["message"].startswith("Assistant did not respond within")
    assert stub.closed, "inner generator was not closed"

    from app.models.conversation import AssistantConversation, AssistantMessage
    from sqlalchemy import func, select

    async with _TestSessionLocal() as db:
        convs = (
            await db.execute(select(func.count()).select_from(AssistantConversation))
        ).scalar_one()
        msgs = (await db.execute(select(func.count()).select_from(AssistantMessage))).scalar_one()
    assert (convs, msgs) == (0, 0)


class _ClosingDispatcher(_GatherGapFakeDispatcher):
    exited = False

    async def __aexit__(self, *_exc):
        type(self).exited = True


async def _capture_stream_iterator(monkeypatch):
    """Call the endpoint over httpx but keep the route's body iterator so a test
    can drive it by hand (and close it while it is suspended at a yield)."""
    from httpx import ASGITransport, AsyncClient

    captured = {}

    class CapturingResponse(StreamingResponse):
        def __init__(self, content, **kwargs):
            captured["iterator"] = content
            super().__init__(iter([b""]), **kwargs)

    monkeypatch.setattr("app.routes.reservation_assistant.StreamingResponse", CapturingResponse)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            _stream_url(),
            json={"question": "apply it"},
            headers={"Authorization": f"Bearer {_user_token()}"},
        )
    assert resp.status_code == 200
    return captured["iterator"]


@pytest.mark.parametrize("how", ["aclose", "cancel"])
async def test_consumer_leaving_at_a_yield_closes_inner_generator_without_leaks(monkeypatch, how):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 30)
    monkeypatch.setattr("app.routes.reservation_assistant.ToolDispatcher", _ClosingDispatcher)
    _ClosingDispatcher.exited = False
    _override_seed()
    stub = _LandedThenStallStreamAI()
    app.dependency_overrides[get_ai_client] = lambda: stub

    iterator = await _capture_stream_iterator(monkeypatch)
    tasks_before = asyncio.all_tasks()
    first = await anext(iterator)
    assert first.startswith("event: status")
    assert not stub.closed

    if how == "aclose":
        await iterator.aclose()
    else:
        with pytest.raises(asyncio.CancelledError):
            await iterator.athrow(asyncio.CancelledError())

    assert stub.closed, "inner generator left un-closed"
    assert _ClosingDispatcher.exited, "dispatcher context was not exited"
    assert asyncio.all_tasks() <= tasks_before, "a task leaked"
