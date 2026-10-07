"""Tests for the iteration-2 reservation-assistant route."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.routes.reservation_assistant import get_reservation_seed_dep
from app.services import usage_repo
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    AI_PROVIDER_UNREACHABLE_DETAIL,
    INCOMPLETE_AFTER_TOOLS_ANSWER,
    AIError,
    AIProviderUnavailableError,
    AssistantTurnResult,
    TurnSegment,
    get_ai_client,
)
from app.services.llm_provider import TextBlock, ToolResultBlock, ToolUseBlock
from app.services.reservation_context import (
    ReservationNotFoundError,
    ReservationSeed,
)
from app.services.tools import ToolCallRecord
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker

_TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _decode_sub(token: str) -> uuid.UUID:
    payload = jwt.decode(
        token,
        config_module.settings.secret_key,
        algorithms=[config_module.settings.algorithm],
    )
    return uuid.UUID(payload["sub"])


@pytest.fixture(autouse=True)
async def setup_db():
    """Create the conversation tables in the per-test in-memory sqlite.

    Branch 3 added DB persistence to the route; without these tables the
    route's first DB write raises. drop_all after each test keeps state
    isolated.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _user_token(role: str = "user") -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload,
        config_module.settings.secret_key,
        algorithm=config_module.settings.algorithm,
    )


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
    yield
    app.dependency_overrides.clear()


def _seed(device_count: int = 1) -> ReservationSeed:
    return ReservationSeed(
        reservation={
            "id": str(RESERVATION_ID),
            "status": "ACTIVE",
            "start_time": "2026-05-19T09:00:00Z",
            "end_time": "2026-05-19T17:00:00Z",
        },
        devices=[{"id": "aaaa", "name": "fw-a", "template_name": "firewall", "status": "RESERVED"}][
            :device_count
        ],
    )


def _override_seed(seed: ReservationSeed | None = None, *, raises: Exception | None = None):
    resolved = seed if seed is not None else _seed()

    def factory():
        async def gather() -> ReservationSeed:
            if raises is not None:
                raise raises
            return resolved

        return gather

    app.dependency_overrides[get_reservation_seed_dep] = factory


def _override_ai(
    answer: str = "Your reservation has 1 device.",
    *,
    raises: Exception | None = None,
    tool_calls: list[ToolCallRecord] | None = None,
    iterations: int = 1,
    input_tokens: int = 42,
    output_tokens: int = 17,
    pre_raise_tool_calls: list[ToolCallRecord] | None = None,
    pre_raise_side_effects: list[dict] | None = None,
    pre_raise_segments: list[TurnSegment] | None = None,
):
    """`pre_raise_*` (issue #871) simulate whatever the real loop would have
    already dispatched/recorded on an EARLIER iteration before a LATER
    provider call raises: they populate the dispatcher and the route's shared
    `segments` list before the stub raises, so a route test can exercise the
    side-effect-present branch without a real AIClient. All three default to
    empty, so every existing `raises=` caller is unaffected.
    """

    class StubAI:
        async def answer_reservation_question_with_tools(
            self,
            *,
            messages,
            dispatcher,
            max_iterations=8,
            per_call_timeout_s=20.0,
            segments=None,
            usage=None,
        ):
            if raises is not None:
                dispatcher.call_log.extend(pre_raise_tool_calls or [])
                dispatcher.side_effects.extend(pre_raise_side_effects or [])
                if segments is not None:
                    segments.extend(pre_raise_segments or [])
                raise raises
            # Mirror the real dispatcher's call_log API so the route can read it.
            dispatcher.call_log.extend(tool_calls or [])
            result_usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
            return AssistantTurnResult(
                answer=answer,
                usage=result_usage,
                stop_reason="end_turn",
                iteration=iterations,
                segments=[TurnSegment(assistant_blocks=[TextBlock(text=answer)])],
            )

    app.dependency_overrides[get_ai_client] = lambda: StubAI()


def _url() -> str:
    return f"/reservations/{RESERVATION_ID}/assistant"


# --- Auth / config gates ---


async def test_requires_auth(async_client):
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"})
    assert resp.status_code == 401


async def test_503_when_api_key_blank(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 503


async def test_503_on_unknown_provider(async_client, monkeypatch):
    """AI_PROVIDER set to an unrecognized value degrades to 503 at the
    get_ai_client dependency, not the 500 issue #245 reported. The AIClient
    dependency is NOT overridden here, so the real dependency-resolution path
    (which runs before the route's ai_is_configured gate) is exercised."""
    monkeypatch.setattr(config_module.settings, "ai_provider", "athropic")
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_NOT_CONFIGURED_DETAIL


async def test_buffered_provider_unreachable_returns_503(async_client):
    """A configured provider whose endpoint is unreachable surfaces as 503 with
    the pinned unreachable detail, not the 502 used for a live-provider failure
    (issue #280, aligned with the issue #131 standardization)."""
    _override_seed()
    _override_ai(raises=AIProviderUnavailableError("connection refused"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_PROVIDER_UNREACHABLE_DETAIL


async def test_over_quota_returns_429_without_calling_ai(async_client, monkeypatch):
    """At/over quota, the assistant returns 429 and never invokes the AI provider."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    token = _user_token()
    async with _TestSessionLocal() as db:
        await usage_repo.add_tokens(db, _decode_sub(token), input_tokens=100, output_tokens=0)

    class ExplodingAI:
        async def answer_reservation_question_with_tools(self, **kwargs):
            raise AssertionError("provider must not be called when over quota")

    app.dependency_overrides[get_ai_client] = lambda: ExplodingAI()
    _override_seed()
    headers = {"Authorization": f"Bearer {token}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["limit"] == 100


async def test_assistant_records_usage_when_quota_enabled(async_client, monkeypatch):
    """A successful assistant turn books the turn's reported tokens against the quota."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    token = _user_token()
    _override_seed()
    _override_ai(input_tokens=42, output_tokens=17)
    headers = {"Authorization": f"Bearer {token}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 200, resp.text
    async with _TestSessionLocal() as db:
        assert await usage_repo.get_today_total(db, _decode_sub(token)) == 59


# --- Happy path ---


async def test_happy_path_returns_answer(async_client):
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(
            _url(), json={"question": "What devices are reserved?"}, headers=headers
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "device" in body["answer"]
    assert body["input_tokens"] == 42
    assert body["output_tokens"] == 17
    assert body["stop_reason"] == "end_turn"
    # Branch 3: every response carries a non-null conversation_id; first turn
    # creates it, follow-up turns echo the supplied id.
    assert body["conversation_id"]
    uuid.UUID(body["conversation_id"])
    assert body["tool_calls"] == []
    assert body["tool_iterations"] == 1


async def test_response_includes_tool_calls_array(async_client):
    _override_seed()
    _override_ai(
        tool_calls=[
            ToolCallRecord(
                name="get_device",
                arguments_summary="device_id=aaaa",
                duration_ms=42,
                error=None,
            ),
            ToolCallRecord(
                name="list_executions_for_reservation",
                arguments_summary="limit=5",
                duration_ms=18,
                error=None,
            ),
        ],
        iterations=2,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "status?"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_iterations"] == 2
    assert [t["name"] for t in body["tool_calls"]] == [
        "get_device",
        "list_executions_for_reservation",
    ]
    assert body["tool_calls"][0]["duration_ms"] == 42
    assert body["tool_calls"][0]["error"] is None


async def test_response_propagates_tool_error_in_summary(async_client):
    _override_seed()
    _override_ai(
        tool_calls=[
            ToolCallRecord(
                name="get_device",
                arguments_summary="device_id=bad",
                duration_ms=5,
                error="device not found",
            ),
        ],
        iterations=2,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "?"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["tool_calls"][0]["error"] == "device not found"


# --- Input validation ---


async def test_empty_question_rejected(async_client):
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": ""}, headers=headers)
    assert resp.status_code == 422


async def test_oversize_question_rejected(async_client):
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "x" * 4001}, headers=headers)
    assert resp.status_code == 422


# --- Downstream failures ---


async def test_reservation_not_found_returns_404(async_client, monkeypatch):
    """Real dep wrapper runs, real gatherer is stubbed to raise, real 404 mapping fires."""

    async def stub_gather(token, reservation_id):
        raise ReservationNotFoundError(str(reservation_id))

    monkeypatch.setattr("app.routes.reservation_assistant.gather_reservation_seed", stub_gather)
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 404


async def test_ai_error_returns_502(async_client):
    _override_seed()
    _override_ai(raises=AIError("boom"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 502


async def test_overall_timeout_returns_504(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.05)
    _override_seed()

    class SlowAI:
        async def answer_reservation_question_with_tools(self, **kwargs):
            await asyncio.sleep(1.0)
            return AssistantTurnResult(
                answer="",
                usage=SimpleNamespace(input_tokens=0, output_tokens=0),
                stop_reason="end_turn",
                iteration=1,
                segments=[TurnSegment(assistant_blocks=[TextBlock(text="")])],
            )

    app.dependency_overrides[get_ai_client] = lambda: SlowAI()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 504


async def test_iteration_cap_exceeded_still_returns_200(async_client):
    """The loop's own iteration-cap fallback returns an answer to the route;
    the route should pass it through as a 200 with high iteration count."""
    _override_seed()
    _override_ai(
        answer="Best guess after exhausting budget.",
        tool_calls=[
            ToolCallRecord(name="get_device", arguments_summary="", duration_ms=1, error=None)
        ]
        * 8,
        iterations=8,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "?"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_iterations"] == 8
    assert len(body["tool_calls"]) == 8
    assert "exhausting" in body["answer"]


# --- Observability ---


async def test_question_text_not_logged(async_client, caplog):
    """The 'ai_reservation_assistant' log entry must not include the question text."""
    _override_seed()
    _override_ai()
    secret = "the-very-secret-customer-name-do-not-leak"
    headers = {"Authorization": f"Bearer {_user_token()}"}
    with caplog.at_level(logging.INFO):
        async with async_client as client:
            resp = await client.post(_url(), json={"question": secret}, headers=headers)
    assert resp.status_code == 200
    matching = [r for r in caplog.records if r.message == "ai_reservation_assistant"]
    assert matching, "expected ai_reservation_assistant log record"
    for record in matching:
        for value in record.__dict__.values():
            assert secret not in str(value)


async def test_tool_call_names_logged_but_result_bodies_not_logged(async_client, caplog):
    """Tool names + arg summaries are loggable; tool result bodies must never appear."""
    secret_result_body = "INTERNAL-CONFIG-PAYLOAD-DO-NOT-LEAK"
    _override_seed()

    class StubAI:
        async def answer_reservation_question_with_tools(self, *, dispatcher, **kwargs):
            # Simulate the dispatcher running a tool whose result body is sensitive.
            # We populate call_log with the metadata; the body itself is what the
            # model would see, NOT what the route is allowed to log.
            dispatcher.call_log.append(
                ToolCallRecord(
                    name="get_device_current_config",
                    arguments_summary="device_id=aaaa",
                    duration_ms=12,
                    error=None,
                )
            )
            usage = SimpleNamespace(input_tokens=1, output_tokens=1)
            return AssistantTurnResult(
                answer="ok",
                usage=usage,
                stop_reason="end_turn",
                iteration=2,
                segments=[TurnSegment(assistant_blocks=[TextBlock(text="ok")])],
            )

    app.dependency_overrides[get_ai_client] = lambda: StubAI()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    with caplog.at_level(logging.INFO):
        async with async_client as client:
            resp = await client.post(
                _url(),
                json={"question": "show me the config"},
                headers=headers,
            )
    assert resp.status_code == 200
    matching = [r for r in caplog.records if r.message == "ai_reservation_assistant"]
    assert matching
    for record in matching:
        for value in record.__dict__.values():
            assert secret_result_body not in str(value)
        # Positive check: tool_call_count IS logged.
        assert record.__dict__.get("tool_call_count") == 1


# --- Multi-turn chat (Branch 3) ---


async def test_first_turn_creates_conversation_and_returns_id(async_client):
    _override_seed()
    _override_ai(answer="first turn answer")
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"]
    # Returned id parses as a UUID.
    uuid.UUID(body["conversation_id"])


async def test_second_turn_with_provided_conversation_id_appends_to_existing(async_client):
    """Re-using the conversation_id from turn 1 must NOT call seed_gatherer
    again (the seed is read from the DB), and the route must accept the
    request with a 200.
    """
    _override_seed()
    _override_ai(answer="first")
    token_str = _user_token()
    headers = {"Authorization": f"Bearer {token_str}"}
    async with async_client as client:
        first = await client.post(_url(), json={"question": "turn one"}, headers=headers)
        assert first.status_code == 200
        conv_id = first.json()["conversation_id"]

        # Swap in a second-turn StubAI that asserts messages has grown.
        seen_message_counts: list[int] = []

        class TurnTwoStub:
            async def answer_reservation_question_with_tools(self, *, messages, **kw):
                seen_message_counts.append(len(messages))
                return AssistantTurnResult(
                    answer="second turn answer",
                    usage=SimpleNamespace(input_tokens=5, output_tokens=5),
                    stop_reason="end_turn",
                    iteration=1,
                    segments=[TurnSegment(assistant_blocks=[TextBlock(text="second turn answer")])],
                )

        app.dependency_overrides[get_ai_client] = lambda: TurnTwoStub()

        second = await client.post(
            _url(),
            json={"question": "turn two", "conversation_id": conv_id},
            headers=headers,
        )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conv_id
    assert "second turn" in second.json()["answer"]
    # Turn one persisted: seed-with-question (1 msg) + assistant (1 msg) = 2.
    # Turn two then appends a new user message before the AI call, so the
    # AI sees 3 messages.
    assert seen_message_counts == [3]


async def test_second_turn_with_unknown_conversation_id_returns_404(async_client):
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    bogus = str(uuid.uuid4())
    async with async_client as client:
        resp = await client.post(
            _url(),
            json={"question": "hi", "conversation_id": bogus},
            headers=headers,
        )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


async def test_second_turn_with_other_users_conversation_id_returns_404(async_client):
    """Ownership leak protection: a conversation created by user A must not
    be loadable by user B even with the right reservation_id. Returns 404
    (not 403) to avoid leaking conversation existence.
    """
    _override_seed()
    _override_ai()
    user_a_token = _user_token()
    user_b_token = _user_token()  # different `sub` (random uuid in helper)
    async with async_client as client:
        # User A creates a conversation.
        a_resp = await client.post(
            _url(),
            json={"question": "private question"},
            headers={"Authorization": f"Bearer {user_a_token}"},
        )
        assert a_resp.status_code == 200
        conv_id = a_resp.json()["conversation_id"]

        # User B tries to continue it.
        b_resp = await client.post(
            _url(),
            json={"question": "give me your data", "conversation_id": conv_id},
            headers={"Authorization": f"Bearer {user_b_token}"},
        )
    assert b_resp.status_code == 404


# --- Streaming endpoint (SSE) ---


def _override_streaming_ai(
    events,
    *,
    raises: Exception | None = None,
    pre_raise_tool_calls: list[ToolCallRecord] | None = None,
    pre_raise_side_effects: list[dict] | None = None,
    pre_raise_segments: list[TurnSegment] | None = None,
):
    """Override get_ai_client with a stub whose streaming method yields `events`.

    `events` is a list of AssistantStatus/AssistantToken/AssistantDone instances
    (built in-test), letting a route test assert the exact SSE framing without a
    real provider. `pre_raise_*` (issue #871) mirror `_override_ai`'s: see that
    docstring.
    """

    class StreamStubAI:
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
            if raises is not None:
                dispatcher.call_log.extend(pre_raise_tool_calls or [])
                dispatcher.side_effects.extend(pre_raise_side_effects or [])
                if segments is not None:
                    segments.extend(pre_raise_segments or [])
                raise raises
            for ev in events:
                yield ev

    app.dependency_overrides[get_ai_client] = lambda: StreamStubAI()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, json-data) pairs."""
    out = []
    for chunk in text.strip().split("\n\n"):
        if not chunk.strip():
            continue
        event = None
        data = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        out.append((event, data))
    return out


def _stream_url() -> str:
    return f"/reservations/{RESERVATION_ID}/assistant/stream"


async def test_stream_emits_status_tokens_then_done(async_client):
    from app.services.ai_client import AssistantDone, AssistantStatus, AssistantToken

    done = AssistantDone(
        result=AssistantTurnResult(
            answer="eth1/6 is down.",
            usage=SimpleNamespace(input_tokens=12, output_tokens=8),
            stop_reason="end_turn",
            iteration=1,
            segments=[TurnSegment(assistant_blocks=[TextBlock(text="eth1/6 is down.")])],
        )
    )
    _override_seed()
    _override_streaming_ai(
        [
            AssistantStatus(message="analyzing"),
            AssistantToken(text="eth1/6 "),
            AssistantToken(text="is down."),
            done,
        ]
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "what's wrong?"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert types == ["status", "token", "token", "done"]
    tokens = [d["text"] for e, d in events if e == "token"]
    assert tokens == ["eth1/6 ", "is down."]
    done_data = events[-1][1]
    assert done_data["answer"] == "eth1/6 is down."
    assert done_data["conversation_id"]
    assert done_data["output_tokens"] == 8


async def test_stream_status_frame_carries_interim_flag(async_client):
    """The route copies AssistantStatus.interim straight onto the wire `status`
    frame. A post-tool-narration status (interim=True) tells the client to
    DISCARD the provisional tokens streamed before the tool turn; assert both the
    default-False and the True frames serialize their flag verbatim."""
    from app.services.ai_client import AssistantDone, AssistantStatus, AssistantToken

    done = AssistantDone(
        result=AssistantTurnResult(
            answer="eth1/6 is down.",
            usage=SimpleNamespace(input_tokens=12, output_tokens=8),
            stop_reason="end_turn",
            iteration=2,
            segments=[TurnSegment(assistant_blocks=[TextBlock(text="eth1/6 is down.")])],
        )
    )
    _override_seed()
    _override_streaming_ai(
        [
            # Generic thinking phase: interim defaults to False.
            AssistantStatus(message="analyzing"),
            # A provisional token streamed before the model resolves to a tool.
            AssistantToken(text="let me check "),
            # Post-narration status: interim=True, telling the client to discard
            # the provisional token above before showing tool progress.
            AssistantStatus(message="running list_ports", tools=["list_ports"], interim=True),
            AssistantToken(text="eth1/6 is down."),
            done,
        ]
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "what's wrong?"}, headers=headers)
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    status_frames = [d for e, d in events if e == "status"]
    assert len(status_frames) == 2
    # First status is the generic thinking phase: interim False, no tools.
    assert status_frames[0]["interim"] is False
    assert status_frames[0]["tools"] == []
    # Second status is the post-narration interim discard signal: True, with the
    # about-to-dispatch tool name passed through.
    assert status_frames[1]["interim"] is True
    assert status_frames[1]["tools"] == ["list_ports"]
    assert status_frames[1]["message"] == "running list_ports"


async def test_stream_persists_conversation(async_client):
    """After the stream completes, the conversation exists and a follow-up turn
    on the same conversation_id is accepted (proves persistence ran)."""
    from app.services.ai_client import AssistantDone, AssistantToken

    done = AssistantDone(
        result=AssistantTurnResult(
            answer="Answer one.",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
            stop_reason="end_turn",
            iteration=1,
            segments=[TurnSegment(assistant_blocks=[TextBlock(text="Answer one.")])],
        )
    )
    _override_seed()
    _override_streaming_ai([AssistantToken(text="Answer one."), done])
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q1"}, headers=headers)
        conv_id = _parse_sse(resp.text)[-1][1]["conversation_id"]
        # Buffered follow-up on the same conversation must resolve (404 would mean
        # the streamed turn did not persist).
        _override_ai(answer="Answer two.")
        follow = await client.post(
            _url(),
            json={"question": "q2", "conversation_id": conv_id},
            headers=headers,
        )
    assert follow.status_code == 200


async def test_stream_emits_error_event_on_ai_failure(async_client):
    _override_seed()
    _override_streaming_ai([], raises=AIError("model exploded"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q"}, headers=headers)
    # The HTTP status is 200 (stream opened); the failure rides an error event.
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1][0] == "error"
    # The client-facing message is generic; the raw exception text must NOT leak
    # (CWE-209), it is logged server-side instead.
    assert events[-1][1]["message"] == "Assistant call failed"
    assert "model exploded" not in events[-1][1]["message"]


async def test_stream_provider_unreachable_emits_error_event(async_client):
    """A configured-but-unreachable provider that fails once the stream has
    opened cannot set a 503 status line, so the failure rides an SSE `error`
    event carrying the diagnosable unreachable message (issue #280). The stream
    ends cleanly rather than dropping the connection."""
    _override_seed()
    _override_streaming_ai([], raises=AIProviderUnavailableError("connection refused"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "q"}, headers=headers)
    # HTTP 200 (stream opened); the failure is an in-band error event.
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["message"] == AI_PROVIDER_UNREACHABLE_DETAIL


async def test_stream_requires_auth(async_client):
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "hello"})
    assert resp.status_code == 401


# --- Streaming endpoint setup gates ---
#
# Auth, quota, and conversation resolution run in the endpoint body BEFORE the
# StreamingResponse is returned, so these failures must surface as real HTTP
# statuses the client can branch on, NOT as in-band SSE `error` frames. The
# streaming endpoint duplicates these gates from the buffered one without sharing
# the code path, so each is pinned here independently. Only failures AFTER the
# stream opens (see the timeout test below) become `error` events.


async def test_stream_503_when_api_key_blank(async_client, monkeypatch):
    """Unconfigured AI: the stream endpoint 503s as a real HTTP status, before
    any stream is opened (not a 200 event-stream carrying an error frame)."""
    monkeypatch.setattr(config_module.settings, "ai_api_key", "")
    monkeypatch.setattr(config_module.settings, "ai_base_url", "")
    _override_seed()
    _override_streaming_ai([])
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 503
    assert not resp.headers["content-type"].startswith("text/event-stream")


async def test_stream_over_quota_returns_429_without_calling_ai(async_client, monkeypatch):
    """At/over quota, the stream endpoint 429s as a real HTTP status and never
    opens the stream or invokes the provider (enforce_quota runs in _prepare_turn,
    before the StreamingResponse is returned). Mirrors the buffered 429 gate."""
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 100)
    token = _user_token()
    async with _TestSessionLocal() as db:
        await usage_repo.add_tokens(db, _decode_sub(token), input_tokens=100, output_tokens=0)

    class ExplodingStreamAI:
        async def answer_reservation_question_streaming(self, **kwargs):
            raise AssertionError("provider must not be called when over quota")
            yield  # unreachable; present only so this is an async generator

    app.dependency_overrides[get_ai_client] = lambda: ExplodingStreamAI()
    _override_seed()
    headers = {"Authorization": f"Bearer {token}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["limit"] == 100
    assert not resp.headers["content-type"].startswith("text/event-stream")


async def test_stream_unknown_conversation_id_returns_404(async_client):
    """A non-existent conversation_id 404s as a real HTTP status before the
    stream opens. Mirrors the buffered unknown-conversation gate."""
    _override_seed()
    _override_streaming_ai([])
    headers = {"Authorization": f"Bearer {_user_token()}"}
    bogus = str(uuid.uuid4())
    async with async_client as client:
        resp = await client.post(
            _stream_url(),
            json={"question": "hi", "conversation_id": bogus},
            headers=headers,
        )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


async def test_stream_other_users_conversation_id_returns_404(async_client):
    """Ownership-leak protection on the stream path: a conversation created by
    user A must not be loadable by user B. Returns 404 (not 403) so existence is
    not leaked, as a real HTTP status before the stream opens."""
    _override_seed()
    _override_ai()  # user A creates the conversation via the buffered endpoint
    user_a_token = _user_token()
    user_b_token = _user_token()  # different `sub` (random uuid in helper)
    async with async_client as client:
        a_resp = await client.post(
            _url(),
            json={"question": "private question"},
            headers={"Authorization": f"Bearer {user_a_token}"},
        )
        assert a_resp.status_code == 200
        conv_id = a_resp.json()["conversation_id"]

        # User B must be rejected before reaching the stream.
        _override_streaming_ai([])
        b_resp = await client.post(
            _stream_url(),
            json={"question": "give me your data", "conversation_id": conv_id},
            headers={"Authorization": f"Bearer {user_b_token}"},
        )
    assert b_resp.status_code == 404


async def test_stream_overall_timeout_emits_error_and_leaves_no_orphan(async_client, monkeypatch):
    """The stream opens (200), then the overall deadline fires mid-generation:
    an HTTP status can no longer be set, so the timeout becomes an SSE `error`
    frame, and the flushed-but-uncommitted user turn is rolled back so no orphan
    persists. Streaming twin of the buffered 504 timeout."""
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.05)

    class SlowStreamAI:
        async def answer_reservation_question_streaming(self, **kwargs):
            await asyncio.sleep(1)
            yield  # never reached: the deadline fires during the sleep above

    app.dependency_overrides[get_ai_client] = lambda: SlowStreamAI()
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "slow"}, headers=headers)
    # The stream opened, so the failure rides an error event, not an HTTP status.
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["message"].startswith("Assistant did not respond within")

    # Atomicity: a first-turn timeout must roll back the flushed conversation and
    # user message. No conversation_id is returned on failure, so prove it at the
    # DB level: zero conversations and zero messages persisted.
    from app.models.conversation import AssistantConversation, AssistantMessage
    from sqlalchemy import func, select

    async with _TestSessionLocal() as db:
        conv_count = (
            await db.execute(select(func.count()).select_from(AssistantConversation))
        ).scalar_one()
        msg_count = (
            await db.execute(select(func.count()).select_from(AssistantMessage))
        ).scalar_one()
    assert conv_count == 0
    assert msg_count == 0


# --- Turn atomicity: a mid-turn failure must not orphan the user message ---
#
# Root cause regression: _prepare_turn used to commit the new user message
# before the AI loop ran, so a provider failure (raise, no-text, timeout) left
# a committed user message with no assistant reply. On the next turn the
# reconstructed history then had two user messages in a row, which the provider
# rejects with a 400, wedging the conversation permanently. The fix flushes the
# user turn and defers the commit to _persist_turn (and rolls back on failure),
# so the user message and the assistant reply are one atomic transaction.


async def _load_db_messages(conversation_id):
    """Return (role, blocks) tuples for a conversation, ordered by position,
    read straight from the DB so a test can assert exactly what persisted.
    """
    from app.models.conversation import AssistantMessage
    from sqlalchemy import select

    async with _TestSessionLocal() as db:
        result = await db.execute(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == uuid.UUID(conversation_id))
            .order_by(AssistantMessage.position)
        )
        rows = result.scalars().all()
        return [(row.role.value, row.content_blocks) for row in rows]


def _assert_no_orphan_trailing_user(rows):
    """The persisted history must never end on a USER turn with no ASSISTANT
    reply after it, and must never have two consecutive real USER turns (TOOL
    echoes between an assistant turn and the next user turn are fine). Either
    shape is the orphan that wedges the provider's role alternation.
    """
    real_roles = [role for role, _ in rows if role in ("USER", "ASSISTANT")]
    assert real_roles, "expected at least the seed user message"
    assert real_roles[-1] == "ASSISTANT", (
        f"history ends on a USER turn with no assistant reply: {real_roles}"
    )
    for prev, cur in zip(real_roles, real_roles[1:]):
        assert not (prev == "USER" and cur == "USER"), (
            f"two consecutive user turns persisted (orphan): {real_roles}"
        )


async def _start_conversation(client, headers):
    """Create a conversation via one successful buffered turn; return its id."""
    _override_ai(answer="turn one answer")
    resp = await client.post(_url(), json={"question": "turn one"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["conversation_id"]


async def test_buffered_failure_leaves_no_orphan_and_next_turn_succeeds(async_client):
    """Buffered: turn 2 fails in the provider; the failed turn's user message
    must NOT persist, and turn 3 on the same conversation must still succeed.
    """
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        conv_id = await _start_conversation(client, headers)

        # Turn two raises mid-loop.
        _override_ai(raises=AIError("provider exploded mid-turn"))
        failed = await client.post(
            _url(),
            json={"question": "turn two", "conversation_id": conv_id},
            headers=headers,
        )
        assert failed.status_code == 502

        # The failed turn must have left no orphan trailing user message.
        rows = await _load_db_messages(conv_id)
        _assert_no_orphan_trailing_user(rows)
        # Turn one only: seed-with-question (USER) + assistant reply (ASSISTANT).
        assert [r for r, _ in rows] == ["USER", "ASSISTANT"]

        # Turn three on the same conversation succeeds (conversation not wedged).
        _override_ai(answer="turn three answer")
        recovered = await client.post(
            _url(),
            json={"question": "turn three", "conversation_id": conv_id},
            headers=headers,
        )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["conversation_id"] == conv_id
    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    # turn one (USER, ASSISTANT) + turn three (USER, ASSISTANT); turn two
    # rolled back entirely.
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]


async def test_buffered_no_text_raises_exact_wording_and_leaves_no_orphan(async_client):
    """The all-reasoning final turn raises 'AI returned no text content'. The
    route maps it to 502, and the failed turn must leave no orphan user message.
    This exercises the real loop (not a stub) so the exact wording is pinned.
    """
    from app.services.ai_client import AIClient
    from app.services.llm_provider import ProviderResponse, Usage

    _override_seed()

    class NoTextProvider:
        async def call(self, **kwargs):
            # stop_reason is end_turn (not tool_use) with zero text blocks: the
            # loop's "no usable text" branch fires.
            return ProviderResponse(
                content=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=3, output_tokens=0),
                raw_model="test-model",
            )

    app.dependency_overrides[get_ai_client] = lambda: AIClient(
        provider=NoTextProvider(), max_tokens=64
    )

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        # First turn itself returns no text, so the whole turn (conversation
        # included) must roll back: a follow-up cannot reference it.
        resp = await client.post(_url(), json={"question": "reason only"}, headers=headers)
    assert resp.status_code == 502

    # The conversation must not have been left half-created with an orphan user
    # message. No conversation_id is returned on failure, so prove atomicity at
    # the DB level: zero conversations, zero messages persisted.
    from app.models.conversation import AssistantConversation, AssistantMessage
    from sqlalchemy import func, select

    async with _TestSessionLocal() as db:
        conv_count = (
            await db.execute(select(func.count()).select_from(AssistantConversation))
        ).scalar_one()
        msg_count = (
            await db.execute(select(func.count()).select_from(AssistantMessage))
        ).scalar_one()
    assert conv_count == 0, "failed first turn left an orphan conversation"
    assert msg_count == 0, "failed first turn left an orphan message"


async def test_buffered_no_text_exact_error_wording():
    """Pin the exact wording the loop raises on an all-reasoning final turn."""
    from app.services.ai_client import AIClient
    from app.services.llm_provider import Message, ProviderResponse, TextBlock, Usage
    from app.services.tools import ToolDispatcher

    class NoTextProvider:
        async def call(self, **kwargs):
            return ProviderResponse(
                content=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=0),
                raw_model="test-model",
            )

    client = AIClient(provider=NoTextProvider(), max_tokens=64)
    async with ToolDispatcher(
        token="t", reservation_id=RESERVATION_ID, char_cap=1000
    ) as dispatcher:
        with pytest.raises(AIError, match=r"^AI returned no text content$"):
            await client.answer_reservation_question_with_tools(
                messages=[Message(role="user", content=[TextBlock(text="hi")])],
                dispatcher=dispatcher,
                max_iterations=4,
            )


# --- No text after tools ran: fallback instead of rollback (issue #848) ---
#
# A turn that dispatches a tool and then ends with no text must NOT roll back:
# the tool's side effect (an inventory write) already happened, so a 502 here
# would tell the user the call failed while the write persists. This exercises
# the real loop through a real AIClient + real ToolDispatcher (never a stub),
# so the route-level persistence and 200 response are proven end to end.


async def test_buffered_no_text_after_tool_dispatch_returns_fallback_and_persists(async_client):
    """A provider that dispatches one tool and then ends the turn with no text
    must return 200 with the fixed fallback answer, list the dispatched tool,
    and persist the turn (no rollback, no orphan). The dispatched tool name is
    deliberately not a real tool so dispatch fails closed with no network call
    (ToolDispatcher's "unknown tool" path): the same fallback rule applies
    whether the dispatch succeeded or errored, and this keeps the test
    hermetic.
    """
    from app.services.ai_client import NO_SUMMARY_FALLBACK_ANSWER, AIClient
    from app.services.llm_provider import ProviderResponse, ToolUseBlock, Usage

    _override_seed()

    class ToolThenNoTextProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    content=[ToolUseBlock(id="toolu_1", name="not_a_real_tool", input={})],
                    stop_reason="tool_use",
                    usage=Usage(input_tokens=6, output_tokens=4),
                    raw_model="test-model",
                )
            return ProviderResponse(
                content=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=3, output_tokens=0),
                raw_model="test-model",
            )

    app.dependency_overrides[get_ai_client] = lambda: AIClient(
        provider=ToolThenNoTextProvider(), max_tokens=64
    )

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "act on it"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == NO_SUMMARY_FALLBACK_ANSWER
    assert [t["name"] for t in body["tool_calls"]] == ["not_a_real_tool"]

    conv_id = body["conversation_id"]
    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    # The tool-dispatch iteration persists as ASSISTANT (the tool_use block)
    # plus TOOL (the echoed tool_result); the closing fallback iteration
    # persists as its own ASSISTANT row.
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


async def test_buffered_no_text_after_tool_dispatch_surfaces_pending_apply(async_client):
    """The fallback-answer turn still surfaces pending_apply when a
    schedule_config_apply call succeeded in the same turn, so the confirmation
    modal opens exactly as it would for a normal text answer. Route-level: a
    stub AI (as the rest of this file's tool_calls/pending_apply tests do)
    records the scheduled_apply side effect directly on the dispatcher, since
    driving a real schedule_config_apply tool call would need a live inventory
    service this suite does not have. The AssistantTurnResult it returns is
    exactly the shape the real AIClient now produces for this case (answer ==
    NO_SUMMARY_FALLBACK_ANSWER, a well-formed final TextBlock segment), so the
    route's handling of that shape is what's under test here.
    """
    job_id = uuid.uuid4()
    version_id = uuid.uuid4()
    device_id = uuid.uuid4()

    from app.services.ai_client import NO_SUMMARY_FALLBACK_ANSWER

    class SchedulingNoTextAI:
        async def answer_reservation_question_with_tools(self, *, messages, dispatcher, **kw):
            dispatcher.call_log.append(
                ToolCallRecord(
                    name="schedule_config_apply",
                    arguments_summary="device_id=... dry_run=True",
                    duration_ms=12,
                    error=None,
                )
            )
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
            return AssistantTurnResult(
                answer=NO_SUMMARY_FALLBACK_ANSWER,
                usage=SimpleNamespace(input_tokens=6, output_tokens=0),
                stop_reason="end_turn",
                iteration=2,
                segments=[
                    TurnSegment(assistant_blocks=[TextBlock(text=NO_SUMMARY_FALLBACK_ANSWER)])
                ],
            )

    _override_seed()
    app.dependency_overrides[get_ai_client] = lambda: SchedulingNoTextAI()

    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "apply it"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == NO_SUMMARY_FALLBACK_ANSWER
    assert body["pending_apply"] is not None
    assert body["pending_apply"]["job_id"] == str(job_id)
    assert body["pending_apply"]["dry_run"] is True

    rows = await _load_db_messages(body["conversation_id"])
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT"]


async def test_stream_failure_leaves_no_orphan_and_next_turn_succeeds(async_client):
    """Streaming: turn 2 fails after the stream opened; the error event must not
    leave the user message orphaned, and turn 3 must still succeed.
    """
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        conv_id = await _start_conversation(client, headers)

        # Turn two raises inside the streaming generator: surfaces as an error
        # event (HTTP 200 since the stream had opened) and must roll back.
        _override_streaming_ai([], raises=AIError("stream blew up"))
        failed = await client.post(
            _stream_url(),
            json={"question": "turn two", "conversation_id": conv_id},
            headers=headers,
        )
        assert failed.status_code == 200
        events = _parse_sse(failed.text)
        assert events[-1][0] == "error"

        rows = await _load_db_messages(conv_id)
        _assert_no_orphan_trailing_user(rows)
        assert [r for r, _ in rows] == ["USER", "ASSISTANT"]

        # Turn three (buffered) on the same conversation succeeds.
        _override_ai(answer="turn three answer")
        recovered = await client.post(
            _url(),
            json={"question": "turn three", "conversation_id": conv_id},
            headers=headers,
        )
    assert recovered.status_code == 200, recovered.text
    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]


async def test_stream_no_answer_leaves_no_orphan(async_client):
    """Streaming: the generator yields no done event (turn is None). The route
    emits 'Assistant produced no answer' and must roll back the user turn.
    """
    from app.services.ai_client import AssistantStatus

    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        conv_id = await _start_conversation(client, headers)

        # Stream yields a status but never a done event.
        _override_streaming_ai([AssistantStatus(message="analyzing")])
        failed = await client.post(
            _stream_url(),
            json={"question": "turn two", "conversation_id": conv_id},
            headers=headers,
        )
        assert failed.status_code == 200
        events = _parse_sse(failed.text)
        assert events[-1][0] == "error"
        assert events[-1][1]["message"] == "Assistant produced no answer"

        rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT"]


# --- Turn survives a LATER failure once a write tool already ran (issue #871) ---
#
# #848 fixed the case where the model itself ends a turn with no text after a
# tool ran. At least three other exits rolled a turn back the same way even
# though a write tool's side effect (dispatcher.side_effects) was already
# real: a per-call timeout, an unreachable provider, and any other AIError
# including the tool-iteration budget being exhausted. The fix lives in the
# route (not ai_client.py): when dispatcher.side_effects is non-empty at the
# moment one of these exceptions surfaces, the turn is persisted and the
# route returns 200 with the real tool_calls/pending_apply, a fixed answer
# (INCOMPLETE_AFTER_TOOLS_ANSWER), and `incomplete` set to a reason code,
# instead of rolling back and returning an error status. A turn that fails
# with NO side effect keeps today's behavior unchanged; that half of the
# matrix is already covered by the existing, unmodified tests
# test_ai_error_returns_502, test_overall_timeout_returns_504,
# test_buffered_provider_unreachable_returns_503 (buffered) and
# test_stream_emits_error_event_on_ai_failure,
# test_stream_overall_timeout_emits_error_and_leaves_no_orphan,
# test_stream_provider_unreachable_emits_error_event (streaming).

# {TimeoutError, AIProviderUnavailableError, AIError, AIError(iteration cap)}:
# the route only branches on exception TYPE, so the two AIError cases (a
# generic failure and the iteration-cap wording ai_client.py raises at
# ai_client.py:896/:1090) exercise the identical except clause; both are
# parametrized to prove the route does not accidentally special-case the
# message.
_INCOMPLETE_CASES = [
    pytest.param(TimeoutError("overall deadline"), "timeout", id="timeout"),
    pytest.param(
        AIProviderUnavailableError("connection refused"),
        "provider_unavailable",
        id="provider_unavailable",
    ),
    pytest.param(AIError("provider exploded mid-turn"), "ai_error", id="ai_error"),
    pytest.param(
        AIError("AI exhausted 3 tool iterations and returned no text"),
        "ai_error",
        id="ai_error_iteration_cap",
    ),
]


def _pre_raise_write_tool_state(job_id: uuid.UUID, version_id: uuid.UUID, device_id: uuid.UUID):
    """A one-tool-ran-before-the-failure fixture (issue #871): the dispatcher
    recorded a successful schedule_config_apply call and its side effect, and
    the loop recorded the matching completed TurnSegment, exactly as the real
    loop would have on an earlier iteration before a LATER provider call
    raises. Returns (tool_calls, side_effects, segments) for
    `_override_ai`/`_override_streaming_ai`'s `pre_raise_*` kwargs.
    """
    tool_calls = [
        ToolCallRecord(
            name="schedule_config_apply",
            arguments_summary=f"device_id={device_id} dry_run=True",
            duration_ms=15,
            error=None,
        )
    ]
    side_effects = [
        {
            "kind": "scheduled_apply",
            "job_id": str(job_id),
            "version_id": str(version_id),
            "device_id": str(device_id),
            "dry_run": True,
            "scheduled_for": "2026-05-19T10:00:00+00:00",
        }
    ]
    segments = [
        TurnSegment(
            assistant_blocks=[ToolUseBlock(id="toolu_1", name="schedule_config_apply", input={})],
            tool_result_blocks=[
                ToolResultBlock(
                    tool_use_id="toolu_1",
                    content='{"job_id": "scheduled"}',
                    is_error=False,
                )
            ],
        )
    ]
    return tool_calls, side_effects, segments


@pytest.mark.parametrize("exc, reason", _INCOMPLETE_CASES)
async def test_buffered_incomplete_turn_with_side_effect_persists_and_returns_200(
    async_client, exc, reason
):
    job_id, version_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tool_calls, side_effects, segments = _pre_raise_write_tool_state(job_id, version_id, device_id)
    _override_seed()
    _override_ai(
        raises=exc,
        pre_raise_tool_calls=tool_calls,
        pre_raise_side_effects=side_effects,
        pre_raise_segments=segments,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "apply it"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == INCOMPLETE_AFTER_TOOLS_ANSWER
    assert body["incomplete"] == reason
    assert body["stop_reason"] == "incomplete"
    assert [t["name"] for t in body["tool_calls"]] == ["schedule_config_apply"]
    assert body["pending_apply"] is not None
    assert body["pending_apply"]["job_id"] == str(job_id)
    assert body["pending_apply"]["dry_run"] is True
    # The raw exception text must never reach the client (CWE-209).
    assert str(exc) not in json.dumps(body)

    rows = await _load_db_messages(body["conversation_id"])
    _assert_no_orphan_trailing_user(rows)
    # The completed tool round-trip persists (ASSISTANT tool_use + TOOL
    # result), then the closing incomplete-answer message (ASSISTANT); no
    # dangling tool_use without its result, no empty assistant message.
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


@pytest.mark.parametrize("exc, reason", _INCOMPLETE_CASES)
async def test_stream_incomplete_turn_with_side_effect_returns_done_event(
    async_client, exc, reason
):
    job_id, version_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tool_calls, side_effects, segments = _pre_raise_write_tool_state(job_id, version_id, device_id)
    _override_seed()
    _override_streaming_ai(
        [],
        raises=exc,
        pre_raise_tool_calls=tool_calls,
        pre_raise_side_effects=side_effects,
        pre_raise_segments=segments,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "apply it"}, headers=headers)

    # The stream opened (200); the incomplete turn rides the normal `done`
    # event, not an `error` event, so the client can open the confirmation
    # modal exactly as it would for an ordinary completed turn.
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[-1][0] == "done"
    data = events[-1][1]
    assert data["answer"] == INCOMPLETE_AFTER_TOOLS_ANSWER
    assert data["incomplete"] == reason
    assert data["stop_reason"] == "incomplete"
    assert [t["name"] for t in data["tool_calls"]] == ["schedule_config_apply"]
    assert data["pending_apply"]["job_id"] == str(job_id)
    assert str(exc) not in json.dumps(data)

    rows = await _load_db_messages(data["conversation_id"])
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


async def test_buffered_incomplete_turn_next_turn_replays_without_error(async_client):
    """After an incomplete turn persists, the conversation must not be wedged:
    a follow-up turn on the same conversation_id succeeds normally, proving
    the persisted history (ending on a real TextBlock) replays cleanly.
    """
    job_id, version_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tool_calls, side_effects, segments = _pre_raise_write_tool_state(job_id, version_id, device_id)
    _override_seed()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        _override_ai(
            raises=AIProviderUnavailableError("connection refused"),
            pre_raise_tool_calls=tool_calls,
            pre_raise_side_effects=side_effects,
            pre_raise_segments=segments,
        )
        incomplete = await client.post(_url(), json={"question": "apply it"}, headers=headers)
        assert incomplete.status_code == 200, incomplete.text
        conv_id = incomplete.json()["conversation_id"]

        _override_ai(answer="all clear now")
        follow_up = await client.post(
            _url(),
            json={"question": "did it work?", "conversation_id": conv_id},
            headers=headers,
        )
    assert follow_up.status_code == 200, follow_up.text
    assert follow_up.json()["answer"] == "all clear now"

    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == [
        "USER",
        "ASSISTANT",
        "TOOL",
        "ASSISTANT",
        "USER",
        "ASSISTANT",
    ]


async def test_buffered_incomplete_turn_with_no_side_effect_still_rolls_back(async_client):
    """Sanity check on the discriminator itself: the SAME exception type that
    triggers the incomplete-turn path above must fall through to today's
    rollback-and-502 behavior when dispatcher.side_effects is empty (no
    pre_raise_* given). Duplicates test_ai_error_returns_502's assertion from
    the issue #871 matrix's other axis so both halves of the matrix are
    visible together.
    """
    _override_seed()
    _override_ai(raises=AIProviderUnavailableError("connection refused"))
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == AI_PROVIDER_UNREACHABLE_DETAIL

    from app.models.conversation import AssistantConversation, AssistantMessage
    from sqlalchemy import func, select

    async with _TestSessionLocal() as db:
        conv_count = (
            await db.execute(select(func.count()).select_from(AssistantConversation))
        ).scalar_one()
        msg_count = (
            await db.execute(select(func.count()).select_from(AssistantMessage))
        ).scalar_one()
    assert conv_count == 0
    assert msg_count == 0


# --- Mid-dispatch cancellation gap: a landed side effect whose iteration
# never reached `segments` (issue #871 review follow-up) ---
#
# `asyncio.gather` over a multi-tool dispatch (answer_reservation_question_
# with_tools's per-iteration dispatch) can be cancelled by the overall
# deadline AFTER one sibling call already recorded its side effect and
# BEFORE a concurrent sibling completes: that whole iteration then never
# reaches segments.append (see ToolDispatcher.dispatch's docstring), so the
# response is still correct (tool_calls/pending_apply read straight off
# dispatcher.call_log/side_effects, not off segments), but the persisted
# closing message must name what actually landed instead of silently
# referring to nothing. These tests reproduce the real race (a genuine
# asyncio.gather cancelled mid-flight, not a hand-built stub of the outcome)
# by monkeypatching the ToolDispatcher the route constructs with a fake
# whose "slow_tool" dispatch never returns before the short overall
# deadline, while "schedule_config_apply" completes immediately.

_GAP_JOB_ID = uuid.uuid4()
_GAP_VERSION_ID = uuid.uuid4()
_GAP_DEVICE_ID = uuid.uuid4()


class _GatherGapFakeDispatcher:
    """Route-level test double standing in for the real ToolDispatcher
    (monkeypatched into app.routes.reservation_assistant.ToolDispatcher for
    these tests only, matching its constructor keywords and the async
    context manager + dispatch() interface the route and AIClient use):
    dispatch("schedule_config_apply", ...) completes immediately and records
    a side effect plus a call_log entry exactly like the real
    _tool_schedule_config_apply handler; dispatch("slow_tool", ...) never
    returns within the test's short overall deadline, so a concurrent
    asyncio.gather over both is still awaiting the slow one when the
    deadline cancels it.
    """

    def __init__(self, *, token, reservation_id, char_cap=8000, **_kwargs):
        self.call_log: list[ToolCallRecord] = []
        self.side_effects: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def dispatch(self, tool_name, tool_input):
        if tool_name == "schedule_config_apply":
            self.call_log.append(
                ToolCallRecord(
                    name=tool_name,
                    arguments_summary=str(tool_input)[:50],
                    duration_ms=1,
                    error=None,
                )
            )
            self.side_effects.append(
                {
                    "kind": "scheduled_apply",
                    "tool": "schedule_config_apply",
                    "job_id": str(_GAP_JOB_ID),
                    "version_id": str(_GAP_VERSION_ID),
                    "device_id": str(_GAP_DEVICE_ID),
                    "dry_run": True,
                    "scheduled_for": "2026-05-19T10:00:00+00:00",
                }
            )
            return {"content": '{"job_id": "scheduled"}', "is_error": False}
        # slow_tool: has no internal await that finishes before the overall
        # deadline, so the deadline always cancels this one, never the fast
        # sibling above (which has no await at all and completes on its
        # first scheduling turn).
        await asyncio.sleep(999)
        raise AssertionError("unreachable: the overall deadline must cancel this first")


class _GatherGapStubAI:
    """Mirrors one iteration of the real tool loop (both tools from a single
    tool_use turn dispatched concurrently) closely enough to reproduce the
    real cancellation race, without needing a real LLM response to drive it.
    """

    async def answer_reservation_question_with_tools(
        self,
        *,
        messages,
        dispatcher,
        max_iterations=8,
        per_call_timeout_s=20.0,
        segments=None,
        usage=None,
    ):
        await asyncio.gather(
            dispatcher.dispatch("schedule_config_apply", {"device_id": str(_GAP_DEVICE_ID)}),
            dispatcher.dispatch("slow_tool", {}),
        )
        raise AssertionError("unreachable: the overall deadline must cancel the gather first")


class _GatherGapStreamStubAI:
    """Streaming twin of _GatherGapStubAI."""

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
        from app.services.ai_client import AssistantStatus

        yield AssistantStatus(message="running tools", tools=["schedule_config_apply", "slow_tool"])
        await asyncio.gather(
            dispatcher.dispatch("schedule_config_apply", {"device_id": str(_GAP_DEVICE_ID)}),
            dispatcher.dispatch("slow_tool", {}),
        )
        raise AssertionError("unreachable: the overall deadline must cancel the gather first")


async def test_buffered_mid_dispatch_gap_names_landed_tool_in_closing_message(
    async_client, monkeypatch
):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.05)
    monkeypatch.setattr("app.routes.reservation_assistant.ToolDispatcher", _GatherGapFakeDispatcher)
    _override_seed()
    app.dependency_overrides[get_ai_client] = lambda: _GatherGapStubAI()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "apply it"}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["incomplete"] == "timeout"
        assert body["stop_reason"] == "incomplete"
        assert body["pending_apply"] is not None
        assert body["pending_apply"]["job_id"] == str(_GAP_JOB_ID)
        # The landed tool has no matching tool_use in any persisted segment
        # (its iteration never reached segments.append), so the closing
        # message names it explicitly, past the plain pinned prefix.
        assert body["answer"].startswith(INCOMPLETE_AFTER_TOOLS_ANSWER)
        assert "schedule_config_apply" in body["answer"]

        conv_id = body["conversation_id"]
        rows = await _load_db_messages(conv_id)
        _assert_no_orphan_trailing_user(rows)
        # No tool round-trip persisted (the interrupted iteration never
        # reached segments.append): just the user turn and the closing
        # message naming what actually landed.
        assert [r for r, _ in rows] == ["USER", "ASSISTANT"]
        assert len(rows[-1][1]) == 1
        assert "schedule_config_apply" in rows[-1][1][0]["text"]

        # Next turn on the same conversation must replay cleanly (no wedge).
        _override_ai(answer="all clear now")
        follow_up = await client.post(
            _url(),
            json={"question": "did it work?", "conversation_id": conv_id},
            headers=headers,
        )
    assert follow_up.status_code == 200, follow_up.text
    assert follow_up.json()["answer"] == "all clear now"

    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]


async def test_stream_mid_dispatch_gap_names_landed_tool_in_closing_message(
    async_client, monkeypatch
):
    # 0.5 s, not the 0.05 s the buffered twin uses: this stub yields a `status`
    # frame BEFORE it dispatches, and time spent handing that frame to the client
    # counts against the deadline (issue #946). A 50 ms deadline could already be
    # spent by a slow handoff on a loaded runner, so the tool never landed and the
    # turn ended in `error`; that is what failed in CI. The deadline only has to
    # fire while `slow_tool` hangs, so a wider one costs nothing but wall time.
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.5)
    monkeypatch.setattr("app.routes.reservation_assistant.ToolDispatcher", _GatherGapFakeDispatcher)
    _override_seed()
    app.dependency_overrides[get_ai_client] = lambda: _GatherGapStreamStubAI()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "apply it"}, headers=headers)
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        # The incomplete turn rides the normal `done` event, not `error`.
        assert events[-1][0] == "done"
        data = events[-1][1]
        assert data["incomplete"] == "timeout"
        assert data["stop_reason"] == "incomplete"
        assert data["pending_apply"] is not None
        assert data["pending_apply"]["job_id"] == str(_GAP_JOB_ID)
        assert data["answer"].startswith(INCOMPLETE_AFTER_TOOLS_ANSWER)
        assert "schedule_config_apply" in data["answer"]

        conv_id = data["conversation_id"]
        rows = await _load_db_messages(conv_id)
        _assert_no_orphan_trailing_user(rows)
        assert [r for r, _ in rows] == ["USER", "ASSISTANT"]
        assert len(rows[-1][1]) == 1
        assert "schedule_config_apply" in rows[-1][1][0]["text"]

        # Next turn on the same conversation must replay cleanly (no wedge).
        _override_ai(answer="all clear now")
        follow_up = await client.post(
            _url(),
            json={"question": "did it work?", "conversation_id": conv_id},
            headers=headers,
        )
    assert follow_up.status_code == 200, follow_up.text
    assert follow_up.json()["answer"] == "all clear now"

    rows = await _load_db_messages(conv_id)
    _assert_no_orphan_trailing_user(rows)
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]


# --- Issues #903, #904, #906 ---


def _propose_side_effect_state(version_id: uuid.UUID, device_id: uuid.UUID):
    """Like _pre_raise_write_tool_state but for propose_config_change: a
    config_version_created side effect (issue #903) with its completed segment."""
    tool_calls = [
        ToolCallRecord(
            name="propose_config_change",
            arguments_summary=f"device_id={device_id}",
            duration_ms=9,
            error=None,
        )
    ]
    side_effects = [
        {
            "kind": "config_version_created",
            "tool": "propose_config_change",
            "device_id": str(device_id),
            "version_id": str(version_id),
            "version_number": 3,
        }
    ]
    segments = [
        TurnSegment(
            assistant_blocks=[ToolUseBlock(id="toolu_p", name="propose_config_change", input={})],
            tool_result_blocks=[
                ToolResultBlock(tool_use_id="toolu_p", content='{"validation": "ok"}')
            ],
        )
    ]
    return tool_calls, side_effects, segments


async def test_buffered_timeout_after_propose_persists_turn_without_pending_apply(async_client):
    tool_calls, side_effects, segments = _propose_side_effect_state(uuid.uuid4(), uuid.uuid4())
    _override_seed()
    _override_ai(
        raises=TimeoutError("overall deadline"),
        pre_raise_tool_calls=tool_calls,
        pre_raise_side_effects=side_effects,
        pre_raise_segments=segments,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "propose"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["incomplete"] == "timeout"
    assert body["answer"] == INCOMPLETE_AFTER_TOOLS_ANSWER
    assert body["pending_apply"] is None
    assert [t["name"] for t in body["tool_calls"]] == ["propose_config_change"]
    rows = await _load_db_messages(body["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


async def test_stream_timeout_after_propose_emits_done_without_pending_apply(async_client):
    tool_calls, side_effects, segments = _propose_side_effect_state(uuid.uuid4(), uuid.uuid4())
    _override_seed()
    _override_streaming_ai(
        [],
        raises=TimeoutError("overall deadline"),
        pre_raise_tool_calls=tool_calls,
        pre_raise_side_effects=side_effects,
        pre_raise_segments=segments,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "propose"}, headers=headers)
    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["done"]
    data = events[-1][1]
    assert data["incomplete"] == "timeout"
    assert data["pending_apply"] is None
    rows = await _load_db_messages(data["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


def _slow_persist(monkeypatch, delay_s: float = 0.3):
    """Wrap the real _persist_turn so it outlasts the overall deadline (#904)."""
    from app.routes import reservation_assistant as route_module

    real = route_module._persist_turn
    calls = {"n": 0}

    async def slow(**kwargs):
        calls["n"] += 1
        await asyncio.sleep(delay_s)
        return await real(**kwargs)

    monkeypatch.setattr(route_module, "_persist_turn", slow)
    return calls


async def test_buffered_deadline_during_persist_does_not_interrupt_or_duplicate(
    async_client, monkeypatch
):
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.1)
    calls = _slow_persist(monkeypatch)
    _override_seed()
    _override_ai(answer="done fine")
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hi"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "done fine"
    assert body["incomplete"] is None
    assert calls["n"] == 1
    rows = await _load_db_messages(body["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT"]


async def test_buffered_deadline_during_persist_with_side_effect_persists_once(
    async_client, monkeypatch
):
    """The double-append shape from issue #904: a side-effect turn whose
    persistence outlasts the deadline must store each row exactly once."""
    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.1)
    _slow_persist(monkeypatch)
    _override_seed()
    tool_calls, side_effects, segments = _pre_raise_write_tool_state(
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )

    class OkAI:
        async def answer_reservation_question_with_tools(self, *, dispatcher, segments, **kw):
            dispatcher.call_log.extend(tool_calls)
            dispatcher.side_effects.extend(side_effects)
            done = TurnSegment(assistant_blocks=[TextBlock(text="all set")])
            segments.extend(_pre_raise_write_tool_state(*[uuid.uuid4()] * 3)[2])
            segments.append(done)
            return AssistantTurnResult(
                answer="all set",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
                iteration=2,
                segments=list(segments),
            )

    app.dependency_overrides[get_ai_client] = lambda: OkAI()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "apply"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["incomplete"] is None
    assert body["pending_apply"] is not None
    rows = await _load_db_messages(body["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT", "TOOL", "ASSISTANT"]


async def test_stream_deadline_during_persist_emits_done_not_error(async_client, monkeypatch):
    from app.services.ai_client import AssistantDone

    monkeypatch.setattr(config_module.settings, "assistant_overall_deadline_s", 0.1)
    calls = _slow_persist(monkeypatch)
    _override_seed()
    _override_streaming_ai(
        [
            AssistantDone(
                result=AssistantTurnResult(
                    answer="streamed ok",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    stop_reason="end_turn",
                    iteration=1,
                    segments=[TurnSegment(assistant_blocks=[TextBlock(text="streamed ok")])],
                )
            )
        ]
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_stream_url(), json={"question": "hi"}, headers=headers)
    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["done"]
    assert events[0][1]["answer"] == "streamed ok"
    assert events[0][1]["incomplete"] is None
    assert calls["n"] == 1
    rows = await _load_db_messages(events[0][1]["conversation_id"])
    assert [r for r, _ in rows] == ["USER", "ASSISTANT"]


def test_closing_text_names_landed_call_when_earlier_same_tool_call_errored():
    """Issue #906.1: a refused first schedule_config_apply (error result, no
    side effect) must not make the landed second call look recorded."""
    from app.routes.reservation_assistant import (
        INCOMPLETE_AFTER_TOOLS_ANSWER,
        INCOMPLETE_LANDED_ACTIONS_HEADER,
        _closing_incomplete_text,
    )

    segments = [
        TurnSegment(
            assistant_blocks=[ToolUseBlock(id="toolu_a", name="schedule_config_apply", input={})],
            tool_result_blocks=[
                ToolResultBlock(tool_use_id="toolu_a", content="refused", is_error=True)
            ],
        )
    ]
    dispatcher = SimpleNamespace(
        side_effects=[{"kind": "scheduled_apply", "tool": "schedule_config_apply"}]
    )
    text = _closing_incomplete_text(segments=segments, dispatcher=dispatcher)
    assert text == (
        f"{INCOMPLETE_AFTER_TOOLS_ANSWER}\n\n{INCOMPLETE_LANDED_ACTIONS_HEADER}\n"
        "- schedule_config_apply"
    )


def test_closing_text_omits_call_recorded_by_successful_tool_use():
    from app.routes.reservation_assistant import (
        INCOMPLETE_AFTER_TOOLS_ANSWER,
        _closing_incomplete_text,
    )

    segments = [
        TurnSegment(
            assistant_blocks=[ToolUseBlock(id="toolu_a", name="schedule_config_apply", input={})],
            tool_result_blocks=[ToolResultBlock(tool_use_id="toolu_a", content="{}")],
        )
    ]
    dispatcher = SimpleNamespace(
        side_effects=[{"kind": "scheduled_apply", "tool": "schedule_config_apply"}]
    )
    assert _closing_incomplete_text(segments=segments, dispatcher=dispatcher) == (
        INCOMPLETE_AFTER_TOOLS_ANSWER
    )


def test_closing_text_uses_kind_fallback_name_for_config_version_created():
    from app.routes.reservation_assistant import _closing_incomplete_text

    dispatcher = SimpleNamespace(side_effects=[{"kind": "config_version_created"}])
    assert "- propose_config_change" in _closing_incomplete_text(segments=[], dispatcher=dispatcher)


async def test_incomplete_log_carries_reason_as_its_own_key(async_client, caplog):
    """Issue #906.2: `reason` is a queryable field, not text in the message."""
    from herd_common.logging import JSONFormatter

    tool_calls, side_effects, segments = _propose_side_effect_state(uuid.uuid4(), uuid.uuid4())
    _override_seed()
    _override_ai(
        raises=TimeoutError("overall deadline"),
        pre_raise_tool_calls=tool_calls,
        pre_raise_side_effects=side_effects,
        pre_raise_segments=segments,
    )
    headers = {"Authorization": f"Bearer {_user_token()}"}
    with caplog.at_level(logging.WARNING):
        async with async_client as client:
            await client.post(_url(), json={"question": "x"}, headers=headers)
    records = [r for r in caplog.records if r.getMessage() == "ai_assistant_incomplete_after_tools"]
    assert len(records) == 1
    formatted = json.loads(JSONFormatter("ai-orchestrator").format(records[0]))
    assert formatted["message"] == "ai_assistant_incomplete_after_tools"
    assert formatted["reason"] == "timeout"


# --- A rolled-back turn is still metered (issue #1034) ---


def _override_spending_then_raising_ai(exc: Exception, *, spent: int = 25):
    """The loop's first provider call answered (and spent tokens, recorded on
    the shared `usage` object exactly as the real loop does), then a later
    step raised with no side effect, so the route rolls the turn back."""

    class SpendThenRaise:
        async def answer_reservation_question_with_tools(self, *, usage=None, **_kw):
            usage.input_tokens += spent
            usage.output_tokens += spent
            raise exc

        async def answer_reservation_question_streaming(self, *, usage=None, **_kw):
            usage.input_tokens += spent
            usage.output_tokens += spent
            raise exc
            yield  # pragma: no cover  (makes this an async generator)

    app.dependency_overrides[get_ai_client] = lambda: SpendThenRaise()


async def _today_total_for(token: str) -> int:
    async with _TestSessionLocal() as db:
        return await usage_repo.get_today_total(db, _decode_sub(token))


@pytest.mark.parametrize(
    "exc, status_code",
    [
        (AIError("boom"), 502),
        (AIProviderUnavailableError("refused"), 503),
        (TimeoutError("deadline"), 504),
    ],
)
async def test_buffered_rolled_back_turn_meters_tokens_spent(
    async_client, monkeypatch, exc, status_code
):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    _override_seed()
    _override_spending_then_raising_ai(exc)
    token = _user_token()
    async with async_client as client:
        resp = await client.post(
            _url(), json={"question": "q"}, headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == status_code
    # The turn is gone, the tokens its provider call spent are not.
    assert await _today_total_for(token) == 50


async def test_stream_rolled_back_turn_meters_tokens_spent(async_client, monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_daily_token_quota", 1000)
    _override_seed()
    _override_spending_then_raising_ai(AIError("boom"))
    token = _user_token()
    async with async_client as client:
        resp = await client.post(
            _stream_url(), json={"question": "q"}, headers={"Authorization": f"Bearer {token}"}
        )
    events = _parse_sse(resp.text)
    assert [e for e, _ in events] == ["error"]
    assert await _today_total_for(token) == 50


# --- AI-CONV-5 (issue #1040): later turns do not re-read the reservation ---


async def test_later_turn_reads_neither_the_reservation_nor_its_devices(async_client):
    """Only a first turn gathers the seed; a turn with a conversation_id
    replays the stored seed. No turn is refused for the reservation's status
    (a COMPLETED reservation still gets answers)."""
    gathers = {"n": 0}
    completed_seed = _seed()
    completed_seed.reservation["status"] = "COMPLETED"

    def factory():
        async def gather() -> ReservationSeed:
            gathers["n"] += 1
            return completed_seed

        return gather

    app.dependency_overrides[get_reservation_seed_dep] = factory
    _override_ai(answer="answer")
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        first = await client.post(_url(), json={"question": "q1"}, headers=headers)
        assert first.status_code == 200, first.text
        assert gathers["n"] == 1
        conv_id = first.json()["conversation_id"]
        for question in ("q2", "q3"):
            later = await client.post(
                _url(), json={"question": question, "conversation_id": conv_id}, headers=headers
            )
            assert later.status_code == 200, later.text
    assert gathers["n"] == 1
