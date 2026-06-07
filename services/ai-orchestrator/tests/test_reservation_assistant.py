"""Tests for the iteration-2 reservation-assistant route."""

import asyncio
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
from app.services.ai_client import AIError, AssistantTurnResult, TurnSegment, get_ai_client
from app.services.llm_provider import TextBlock
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
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "sk-ant-fake")
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
):
    class StubAI:
        async def answer_reservation_question_with_tools(
            self,
            *,
            messages,
            dispatcher,
            max_iterations=8,
            per_call_timeout_s=20.0,
        ):
            if raises is not None:
                raise raises
            # Mirror the real dispatcher's call_log API so the route can read it.
            dispatcher.call_log.extend(tool_calls or [])
            usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
            return AssistantTurnResult(
                answer=answer,
                usage=usage,
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
    monkeypatch.setattr(config_module.settings, "anthropic_api_key", "")
    _override_seed()
    _override_ai()
    headers = {"Authorization": f"Bearer {_user_token()}"}
    async with async_client as client:
        resp = await client.post(_url(), json={"question": "hello"}, headers=headers)
    assert resp.status_code == 503


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
