"""Direct-call coverage for the reservation-assistant route and its helpers.

The buffered and streaming route handlers and their shared helpers
(_bearer_token, _prepare_turn, _persist_turn, _sse) are async functions that
FastAPI dispatches in a task the default coverage tracer does not follow into.
The existing test_reservation_assistant.py drives them through the ASGI
transport and asserts the observable behaviour; this module complements it by
awaiting the same coroutines DIRECTLY from the test body so the exercised
branches (timeout, AIError, quota, 404, pending_apply, SSE done/error) are
measured, and pins the exact error and refusal wording on the failure paths.

The provider is always a hand-built stub behind the LLMProvider-shaped surface
the route uses (answer_reservation_question_with_tools /
answer_reservation_question_streaming); no real API is ever called.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from app import config as config_module
from app.database import AsyncSessionLocal, Base, engine
from app.routes import reservation_assistant as ra
from app.routes.reservation_assistant import (
    _bearer_token,
    _persist_turn,
    _prepare_turn,
    _sse,
    get_reservation_seed_dep,
    reservation_assistant,
    reservation_assistant_stream,
)
from app.schemas.assistant import AssistantRequest
from app.services import conversation_repo, usage_repo
from app.services.ai_client import (
    AIError,
    AssistantDone,
    AssistantStatus,
    AssistantToken,
    AssistantTurnResult,
    TurnSegment,
)
from app.services.llm_provider import TextBlock
from app.services.reservation_context import (
    ContextDeadlineExceededError,
    ReservationNotFoundError,
    ReservationSeed,
)
from app.services.tools import ToolCallRecord
from fastapi import HTTPException

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")


def _seed() -> ReservationSeed:
    return ReservationSeed(
        reservation={
            "id": str(RESERVATION_ID),
            "status": "ACTIVE",
            "start_time": "2026-05-19T09:00:00Z",
            "end_time": "2026-05-19T17:00:00Z",
        },
        devices=[{"id": "aaaa", "name": "fw-a", "template_name": "firewall", "status": "RESERVED"}],
    )


def _seed_gatherer(*, raises: Exception | None = None):
    async def _gather() -> ReservationSeed:
        if raises is not None:
            raise raises
        return _seed()

    return _gather


def _user(user_id: uuid.UUID | None = None) -> dict:
    return {"sub": str(user_id or uuid.uuid4()), "role": "user"}


def _turn(
    answer: str = "ok",
    *,
    input_tokens: int = 7,
    output_tokens: int = 3,
    iterations: int = 1,
    stop_reason: str = "end_turn",
) -> AssistantTurnResult:
    return AssistantTurnResult(
        answer=answer,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
        iteration=iterations,
        segments=[TurnSegment(assistant_blocks=[TextBlock(text=answer)])],
    )


class _StubAI:
    """Buffered provider surface the route depends on."""

    def __init__(
        self,
        *,
        turn: AssistantTurnResult | None = None,
        raises: Exception | None = None,
        tool_calls: list[ToolCallRecord] | None = None,
        sleep_s: float | None = None,
    ):
        self._turn = turn if turn is not None else _turn()
        self._raises = raises
        self._tool_calls = tool_calls or []
        self._sleep_s = sleep_s

    async def answer_reservation_question_with_tools(self, *, messages, dispatcher, **kw):
        if self._sleep_s is not None:
            await asyncio.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        dispatcher.call_log.extend(self._tool_calls)
        return self._turn


class _StreamStubAI:
    """Streaming provider surface: yields the canned event sequence."""

    def __init__(self, events, *, raises: Exception | None = None, sleep_s: float | None = None):
        self._events = events
        self._raises = raises
        self._sleep_s = sleep_s

    async def answer_reservation_question_streaming(self, *, messages, dispatcher, **kw):
        if self._sleep_s is not None:
            await asyncio.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        for ev in self._events:
            yield ev


async def _drain(response) -> list[tuple[str, dict]]:
    """Iterate a StreamingResponse body_iterator into (event, data) pairs."""
    out: list[tuple[str, dict]] = []
    raw = b""
    async for chunk in response.body_iterator:
        raw += chunk if isinstance(chunk, bytes) else chunk.encode()
    for block in raw.decode().strip().split("\n\n"):
        if not block.strip():
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        out.append((event, data))
    return out


# --- _bearer_token helper (line 57-59) ---


async def test_bearer_token_extracts_token():
    assert await _bearer_token(authorization="Bearer abc.def") == "abc.def"


async def test_bearer_token_case_insensitive_scheme():
    assert await _bearer_token(authorization="bEaReR xyz") == "xyz"


async def test_bearer_token_rejects_non_bearer():
    with pytest.raises(HTTPException) as exc:
        await _bearer_token(authorization="Basic abc")
    assert exc.value.status_code == 401
    assert exc.value.detail == "Bearer token required"


# --- get_reservation_seed_dep wrapper: 404 and 504 mappings (lines 72-83) ---


async def test_seed_dep_maps_not_found_to_404():
    gatherer = get_reservation_seed_dep(reservation_id=RESERVATION_ID, token="t")

    async def boom(token, reservation_id):
        raise ReservationNotFoundError(str(reservation_id))

    ra.gather_reservation_seed = boom  # type: ignore[assignment]
    try:
        with pytest.raises(HTTPException) as exc:
            await gatherer()
    finally:
        import app.services.reservation_context as rc

        ra.gather_reservation_seed = rc.gather_reservation_seed  # type: ignore[assignment]
    assert exc.value.status_code == 404
    assert exc.value.detail == "Reservation not found"


async def test_seed_dep_maps_deadline_to_504():
    gatherer = get_reservation_seed_dep(reservation_id=RESERVATION_ID, token="t")

    async def slow(token, reservation_id):
        raise ContextDeadlineExceededError("too slow")

    ra.gather_reservation_seed = slow  # type: ignore[assignment]
    try:
        with pytest.raises(HTTPException) as exc:
            await gatherer()
    finally:
        import app.services.reservation_context as rc

        ra.gather_reservation_seed = rc.gather_reservation_seed  # type: ignore[assignment]
    assert exc.value.status_code == 504
    assert exc.value.detail == "Reservation seed gather exceeded its deadline"


# --- _prepare_turn: new conversation, then append on existing (lines 110-156) ---


async def test_prepare_turn_creates_then_appends():
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        conv, messages = await _prepare_turn(
            reservation_id=RESERVATION_ID,
            body=AssistantRequest(question="first"),
            user_id=user_id,
            db=db,
            seed_gatherer=_seed_gatherer(),
        )
        # New conversation: a single seed-with-question user message.
        assert len(messages) == 1
        await db.commit()
        conv_id = conv.id

    async with AsyncSessionLocal() as db:
        conv2, messages2 = await _prepare_turn(
            reservation_id=RESERVATION_ID,
            body=AssistantRequest(question="second", conversation_id=conv_id),
            user_id=user_id,
            db=db,
            seed_gatherer=_seed_gatherer(raises=AssertionError("seed must not refetch")),
        )
        # Existing conversation: append path. Only the seed message exists in the
        # DB (turn 1's assistant reply was never persisted), so messages is
        # seed + appended question = 2.
        assert conv2.id == conv_id
        assert len(messages2) == 2


async def test_prepare_turn_unknown_conversation_raises_404():
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await _prepare_turn(
                reservation_id=RESERVATION_ID,
                body=AssistantRequest(question="x", conversation_id=uuid.uuid4()),
                user_id=uuid.uuid4(),
                db=db,
                seed_gatherer=_seed_gatherer(),
            )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Conversation not found"


# --- _persist_turn: scheduled_apply side-effect surfaces pending_apply (187-195) ---


async def test_persist_turn_surfaces_pending_apply_from_side_effect():
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    version_id = uuid.uuid4()
    device_id = uuid.uuid4()
    scheduled_for = "2026-05-19T10:00:00+00:00"
    async with AsyncSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=user_id, reservation_id=RESERVATION_ID, seed_block="seed", commit=False
        )
        await conversation_repo.set_seed_with_question(db, conversation=conv, question="q")
        await db.flush()
        dispatcher = SimpleNamespace(
            side_effects=[
                {"kind": "noise"},
                {
                    "kind": "scheduled_apply",
                    "job_id": str(job_id),
                    "version_id": str(version_id),
                    "device_id": str(device_id),
                    "dry_run": True,
                    "scheduled_for": scheduled_for,
                },
            ]
        )
        pending = await _persist_turn(
            db=db,
            conversation=conv,
            turn=_turn(answer="applied"),
            user_id=user_id,
            question="q",
            dispatcher=dispatcher,
        )
    assert pending is not None
    assert pending.job_id == job_id
    assert pending.version_id == version_id
    assert pending.device_id == device_id
    assert pending.dry_run is True


async def test_persist_turn_no_side_effect_returns_none():
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        conv = await conversation_repo.create(
            db, user_id=user_id, reservation_id=RESERVATION_ID, seed_block="seed", commit=False
        )
        await conversation_repo.set_seed_with_question(db, conversation=conv, question="q")
        await db.flush()
        pending = await _persist_turn(
            db=db,
            conversation=conv,
            turn=_turn(),
            user_id=user_id,
            question="q",
            dispatcher=SimpleNamespace(side_effects=[]),
        )
    assert pending is None


# --- _sse framing (line 307) ---


def test_sse_frames_event_and_json_data():
    framed = _sse("token", {"text": "hi"})
    assert framed == 'event: token\ndata: {"text": "hi"}\n\n'


# --- Buffered route handler: direct invocation (lines 209-302) ---


async def _call_buffered(*, body, user_id, ai, seed_gatherer=None):
    async with AsyncSessionLocal() as db:
        return await reservation_assistant(
            reservation_id=RESERVATION_ID,
            body=body,
            user=_user(user_id),
            token="tkn",
            ai=ai,
            db=db,
            seed_gatherer=seed_gatherer or _seed_gatherer(),
        )


async def test_buffered_route_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    with pytest.raises(HTTPException) as exc:
        await _call_buffered(
            body=AssistantRequest(question="hi"), user_id=uuid.uuid4(), ai=_StubAI()
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == "AI orchestrator is not configured"


async def test_buffered_route_happy_path_returns_response():
    resp = await _call_buffered(
        body=AssistantRequest(question="what devices?"),
        user_id=uuid.uuid4(),
        ai=_StubAI(
            turn=_turn(answer="one device", input_tokens=42, output_tokens=17),
            tool_calls=[
                ToolCallRecord(
                    name="get_device", arguments_summary="device_id=aaaa", duration_ms=9, error=None
                )
            ],
        ),
    )
    assert resp.answer == "one device"
    assert resp.input_tokens == 42
    assert resp.output_tokens == 17
    assert resp.stop_reason == "end_turn"
    assert resp.tool_iterations == 1
    assert [t.name for t in resp.tool_calls] == ["get_device"]
    assert resp.conversation_id
    uuid.UUID(resp.conversation_id)
    assert resp.pending_apply is None


async def test_buffered_route_records_usage_against_quota(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    user_id = uuid.uuid4()
    await _call_buffered(
        body=AssistantRequest(question="hi"),
        user_id=user_id,
        ai=_StubAI(turn=_turn(input_tokens=42, output_tokens=17)),
    )
    async with AsyncSessionLocal() as db:
        assert await usage_repo.get_today_total(db, user_id) == 59


async def test_buffered_route_over_quota_raises_429(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        await usage_repo.add_tokens(db, user_id, input_tokens=100, output_tokens=0)
        await db.commit()

    class ExplodingAI:
        async def answer_reservation_question_with_tools(self, **kw):
            raise AssertionError("provider must not run when over quota")

    with pytest.raises(HTTPException) as exc:
        await _call_buffered(
            body=AssistantRequest(question="hi"), user_id=user_id, ai=ExplodingAI()
        )
    assert exc.value.status_code == 429
    assert exc.value.detail["limit"] == 100


async def test_buffered_route_ai_error_maps_to_502_with_generic_detail():
    with pytest.raises(HTTPException) as exc:
        await _call_buffered(
            body=AssistantRequest(question="hi"),
            user_id=uuid.uuid4(),
            ai=_StubAI(raises=AIError("backend secret stack trace")),
        )
    assert exc.value.status_code == 502
    # CWE-209: the raw exception text must never reach the client detail.
    assert exc.value.detail == "Assistant call failed"
    assert "secret" not in exc.value.detail


async def test_buffered_route_timeout_maps_to_504(monkeypatch):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.05)
    with pytest.raises(HTTPException) as exc:
        await _call_buffered(
            body=AssistantRequest(question="hi"),
            user_id=uuid.uuid4(),
            ai=_StubAI(sleep_s=1.0),
        )
    assert exc.value.status_code == 504
    assert "did not respond within" in exc.value.detail


async def test_buffered_route_surfaces_pending_apply():
    job_id = uuid.uuid4()
    version_id = uuid.uuid4()
    device_id = uuid.uuid4()

    class SchedulingAI:
        async def answer_reservation_question_with_tools(self, *, messages, dispatcher, **kw):
            dispatcher.side_effects.append(
                {
                    "kind": "scheduled_apply",
                    "job_id": str(job_id),
                    "version_id": str(version_id),
                    "device_id": str(device_id),
                    "dry_run": True,
                    "scheduled_for": "2026-05-19T10:00:00+00:00",
                }
            )
            return _turn(answer="scheduled")

    resp = await _call_buffered(
        body=AssistantRequest(question="apply it"),
        user_id=uuid.uuid4(),
        ai=SchedulingAI(),
    )
    assert resp.pending_apply is not None
    assert resp.pending_apply.job_id == job_id
    assert resp.pending_apply.dry_run is True


# --- Streaming route handler: direct invocation (lines 330-433) ---


async def _call_stream(*, body, user_id, ai, seed_gatherer=None):
    async with AsyncSessionLocal() as db:
        response = await reservation_assistant_stream(
            reservation_id=RESERVATION_ID,
            body=body,
            user=_user(user_id),
            token="tkn",
            ai=ai,
            db=db,
            seed_gatherer=seed_gatherer or _seed_gatherer(),
        )
        return await _drain(response)


async def test_stream_route_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    with pytest.raises(HTTPException) as exc:
        await _call_stream(
            body=AssistantRequest(question="hi"),
            user_id=uuid.uuid4(),
            ai=_StreamStubAI([]),
        )
    assert exc.value.status_code == 503


async def test_stream_route_emits_status_token_done():
    done = AssistantDone(result=_turn(answer="eth1/6 is down.", output_tokens=8))
    events = await _call_stream(
        body=AssistantRequest(question="what's wrong?"),
        user_id=uuid.uuid4(),
        ai=_StreamStubAI(
            [
                AssistantStatus(message="analyzing", tools=["get_device"], interim=False),
                AssistantToken(text="eth1/6 "),
                AssistantToken(text="is down."),
                done,
            ]
        ),
    )
    types = [e for e, _ in events]
    assert types == ["status", "token", "token", "done"]
    status_data = events[0][1]
    assert status_data["message"] == "analyzing"
    assert status_data["tools"] == ["get_device"]
    assert status_data["interim"] is False
    assert [d["text"] for e, d in events if e == "token"] == ["eth1/6 ", "is down."]
    done_data = events[-1][1]
    assert done_data["answer"] == "eth1/6 is down."
    assert done_data["output_tokens"] == 8
    assert done_data["conversation_id"]


async def test_stream_route_no_done_event_emits_error_no_answer():
    events = await _call_stream(
        body=AssistantRequest(question="hi"),
        user_id=uuid.uuid4(),
        ai=_StreamStubAI([AssistantStatus(message="analyzing")]),
    )
    assert events[-1][0] == "error"
    assert events[-1][1]["message"] == "Assistant produced no answer"


async def test_stream_route_ai_error_emits_generic_error_event():
    events = await _call_stream(
        body=AssistantRequest(question="hi"),
        user_id=uuid.uuid4(),
        ai=_StreamStubAI([], raises=AIError("model exploded internals")),
    )
    assert events[-1][0] == "error"
    # CWE-209: generic message only, raw exception text must not leak.
    assert events[-1][1]["message"] == "Assistant call failed"
    assert "exploded" not in events[-1][1]["message"]


async def test_stream_route_timeout_emits_error_event(monkeypatch):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.05)
    events = await _call_stream(
        body=AssistantRequest(question="hi"),
        user_id=uuid.uuid4(),
        ai=_StreamStubAI([], sleep_s=1.0),
    )
    assert events[-1][0] == "error"
    assert "did not respond within" in events[-1][1]["message"]


async def test_stream_route_done_surfaces_pending_apply():
    job_id = uuid.uuid4()

    class SchedulingStreamAI:
        async def answer_reservation_question_streaming(self, *, messages, dispatcher, **kw):
            dispatcher.side_effects.append(
                {
                    "kind": "scheduled_apply",
                    "job_id": str(job_id),
                    "version_id": str(uuid.uuid4()),
                    "device_id": str(uuid.uuid4()),
                    "dry_run": False,
                    "scheduled_for": "2026-05-19T10:00:00+00:00",
                }
            )
            yield AssistantDone(result=_turn(answer="scheduled"))

    events = await _call_stream(
        body=AssistantRequest(question="apply it"),
        user_id=uuid.uuid4(),
        ai=SchedulingStreamAI(),
    )
    done_data = events[-1][1]
    assert done_data["pending_apply"] is not None
    assert done_data["pending_apply"]["job_id"] == str(job_id)
    assert done_data["pending_apply"]["dry_run"] is False
