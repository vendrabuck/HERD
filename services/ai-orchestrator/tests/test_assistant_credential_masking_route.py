"""Issue #1464, functional: through the assistant route's real tool-dispatch path.

POST /reservations/{id}/assistant runs the real AIClient loop against a scripted
provider (no model is called) and the real ToolDispatcher, whose HTTP client is a
mock transport standing in for reservations, inventory, and execution. The stored
config, the history listing, and the run list all hold CANARY; the test asserts
it is in nothing the provider was sent and in no stored assistant_messages row.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from app import config as config_module
from app.database import Base, engine
from app.main import app
from app.models.conversation import AssistantMessage
from app.routes.reservation_assistant import get_reservation_seed_dep
from app.services.ai_client import AIClient, get_ai_client
from app.services.llm_provider import (
    ProviderResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.services.reservation_context import ReservationSeed
from app.services.tools import REDACTED_NOTE, ToolDispatcher
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEVICE_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CANARY = "Canary-S3cr3t-1464"
COMMANDS = ["router bgp 65000", f" neighbor 192.0.2.1 password {CANARY}", " exit"]

_Session = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ai_api_key", "sk-ant-fake")
    yield
    app.dependency_overrides.clear()


def _token() -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "user",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload, config_module.settings.secret_key, algorithm=config_module.settings.algorithm
    )


def _upstream(request: httpx.Request) -> httpx.Response:
    """Reservations, inventory, and execution as the dispatcher reaches them."""
    path = request.url.path
    if path.endswith(f"/{RESERVATION_ID}"):
        return httpx.Response(200, json={"id": str(RESERVATION_ID), "device_ids": [str(DEVICE_A)]})
    meta = {"id": VERSION_ID, "version_number": 4, "description": f"secret {CANARY}"}
    if path.endswith(f"/devices/{DEVICE_A}/config-versions"):
        return httpx.Response(200, json={"items": [meta], "total": 1, "skip": 0, "limit": 10})
    if path.endswith(f"/devices/{DEVICE_A}/config-versions/{VERSION_ID}"):
        return httpx.Response(200, json={**meta, "config": {"commands": COMMANDS}})
    if path.endswith("/runs"):
        row = {
            "id": str(uuid.uuid4()),
            "device_id": str(DEVICE_A),
            "status": "FAILED",
            "input_params": {"method_kwargs": {"commands": COMMANDS}},
            "output": json.dumps({"applied": COMMANDS}),
            "error": f"neighbor 192.0.2.1 password {CANARY}",
        }
        return httpx.Response(200, json={"items": [row], "total": 1, "skip": 0, "limit": 20})
    return httpx.Response(404)


class _ScriptedProvider:
    """Asks for the three tools that relay stored config text, then answers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            device = {"device_id": str(DEVICE_A)}
            return ProviderResponse(
                content=[
                    ToolUseBlock(id="toolu_1", name="get_device_current_config", input=device),
                    ToolUseBlock(id="toolu_2", name="list_device_config_history", input=device),
                    ToolUseBlock(id="toolu_3", name="list_executions_for_reservation", input={}),
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=5, output_tokens=5),
                raw_model="scripted",
            )
        return ProviderResponse(
            content=[TextBlock(text="The BGP neighbor has a password set.")],
            stop_reason="end_turn",
            usage=Usage(input_tokens=5, output_tokens=5),
            raw_model="scripted",
        )


async def test_no_stored_credential_reaches_the_provider_or_the_stored_conversation(
    monkeypatch,
):
    real_init = ToolDispatcher.__init__

    def init_with_mock_upstream(self, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
        real_init(self, **kwargs)
        # The dispatcher closes the client it was handed, as it does its own.
        self._owned_client = True

    monkeypatch.setattr(ToolDispatcher, "__init__", init_with_mock_upstream)

    async def gather() -> ReservationSeed:
        return ReservationSeed(
            reservation={"id": str(RESERVATION_ID), "status": "ACTIVE"},
            devices=[{"id": str(DEVICE_A), "name": "r1", "status": "RESERVED"}],
        )

    app.dependency_overrides[get_reservation_seed_dep] = lambda: gather
    provider = _ScriptedProvider()
    app.dependency_overrides[get_ai_client] = lambda: AIClient(provider=provider, max_tokens=64)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/reservations/{RESERVATION_ID}/assistant",
            json={"question": "what is configured on r1?"},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["error"] for c in body["tool_calls"]] == [None, None, None]

    # What the provider was sent: the second call carries the three tool results.
    assert len(provider.calls) == 2
    sent = repr(provider.calls)
    assert CANARY not in sent
    results = [
        block
        for message in provider.calls[1]["messages"]
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert [r.tool_use_id for r in results] == ["toolu_1", "toolu_2", "toolu_3"]
    for result in results:
        assert result.is_error is False
        decoded = json.loads(result.content)
        assert decoded["redacted"] is True
        assert decoded["note"] == REDACTED_NOTE
    current = json.loads(results[0].content)
    assert current["config"]["commands"][0] == "router bgp 65000"
    assert current["config"]["commands"][1] == " neighbor 192.0.2.1 password [redacted]"

    # What the conversation stored.
    async with _Session() as db:
        rows = (
            await db.execute(
                select(AssistantMessage).where(
                    AssistantMessage.conversation_id == uuid.UUID(body["conversation_id"])
                )
            )
        ).scalars()
        stored = [(row.role.value, row.content_blocks) for row in rows]
    assert "TOOL" in [role for role, _ in stored]
    stored_text = json.dumps(stored)
    assert CANARY not in stored_text
    assert "[redacted]" in stored_text
