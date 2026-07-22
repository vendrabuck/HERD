"""Router tests for the internal wiring-status and wiring-retry endpoints.

ADR 0007 Decision 6 (issue #345 P3b phase 4). Covers the X-Internal-Token gate on both
endpoints, the wiring-status response shape (including the reservation_wiring_state
markers and the retryable flag) and its empty case, and the retry endpoint's relay of
the service outcomes plus its frozen 409 and upstream 503 mappings. The retry worker is
patched so no real driver machinery runs; wiring-status reads the request-scoped test DB.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.routers import executions as ex_router
from app.services.nats_consumer import WIRING_UNRESOLVABLE_REASON, TransientUpstreamError
from app.services.wiring_retry_service import WiringReservationFrozen
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

SWITCH_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
TOKEN_HEADER = {"X-Internal-Token": "test-token"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_failed(port_a, port_b, *, attempts=3, last_error="boom", intended="ACTIVE"):
    async with TestSessionLocal() as s:
        row = L1ConnectionAssignment(
            reservation_id=uuid.UUID(RES_ID),
            switch_device_id=uuid.UUID(SWITCH_ID),
            port_a=port_a,
            port_b=port_b,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error=last_error,
        )
        s.add(row)
        await s.commit()


async def _seed_state(*, frozen=False, last_applied=7):
    async with TestSessionLocal() as s:
        s.add(
            ReservationWiringState(
                reservation_id=uuid.UUID(RES_ID),
                frozen=frozen,
                last_applied_fork_version=last_applied,
            )
        )
        await s.commit()


# --- Token gating -----------------------------------------------------------


@pytest.mark.asyncio
async def test_wiring_status_requires_internal_token(client):
    resp = await client.get(f"/internal/reservations/{RES_ID}/wiring-status")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_wiring_retry_requires_internal_token(client):
    resp = await client.post(f"/internal/reservations/{RES_ID}/wiring/retry")
    assert resp.status_code == 403


# --- Wiring-status shape ----------------------------------------------------


@pytest.mark.asyncio
async def test_wiring_status_shape_includes_state_and_retryable(client):
    await _seed_state(frozen=False, last_applied=7)
    await _seed_failed("0/0/1", "0/0/2", attempts=3, last_error="boom", intended="ACTIVE")
    await _seed_failed(
        "0/0/3", "0/0/4", attempts=1, last_error="disconnect boom", intended="RELEASED"
    )
    await _seed_failed("0/0/5", "0/0/6", attempts=0, last_error=WIRING_UNRESOLVABLE_REASON)

    resp = await client.get(f"/internal/reservations/{RES_ID}/wiring-status", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reservation_id"] == RES_ID
    assert body["last_applied_fork_version"] == 7
    assert body["frozen"] is False
    assert len(body["connections"]) == 3
    by_pair = {(c["port_a"], c["port_b"]): c for c in body["connections"]}
    # A driver-failure FAILED row is retryable; a pinned-reason one is not.
    assert by_pair[("0/0/1", "0/0/2")]["retryable"] is True
    assert by_pair[("0/0/1", "0/0/2")]["status"] == "FAILED"
    assert by_pair[("0/0/1", "0/0/2")]["attempts"] == 3
    assert by_pair[("0/0/1", "0/0/2")]["intended"] == "ACTIVE"
    # intended (issue #369, additive) distinguishes a failed build from a failed
    # release: both are status FAILED, but only intended names the direction.
    assert by_pair[("0/0/3", "0/0/4")]["intended"] == "RELEASED"
    assert by_pair[("0/0/5", "0/0/6")]["retryable"] is False
    # Every field the surface promises is present.
    for c in body["connections"]:
        assert set(c) >= {
            "id",
            "switch_device_id",
            "port_a",
            "port_b",
            "physical_connection_id",
            "status",
            "intended",
            "attempts",
            "last_error",
            "retryable",
            "created_at",
            "released_at",
        }


@pytest.mark.asyncio
async def test_wiring_status_empty_case(client):
    """No rows and no state row: empty connections, null version, not frozen."""
    resp = await client.get(f"/internal/reservations/{RES_ID}/wiring-status", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connections"] == []
    assert body["last_applied_fork_version"] is None
    assert body["frozen"] is False


# --- Retry relay + error mapping --------------------------------------------


@pytest.mark.asyncio
async def test_wiring_retry_relays_outcomes(client):
    canned = {
        "reservation_id": RES_ID,
        "results": [
            {
                "id": str(uuid.uuid4()),
                "switch_device_id": SWITCH_ID,
                "port_a": "0/0/1",
                "port_b": "0/0/2",
                "physical_connection_id": None,
                "outcome": "reconnected",
                "status": "ACTIVE",
                "attempts": 6,
                "last_error": None,
            }
        ],
    }
    with patch.object(ex_router, "reattempt_reservation", new=AsyncMock(return_value=canned)):
        resp = await client.post(
            f"/internal/reservations/{RES_ID}/wiring/retry", headers=TOKEN_HEADER
        )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["outcome"] == "reconnected"


@pytest.mark.asyncio
async def test_wiring_retry_frozen_maps_to_409(client):
    with patch.object(
        ex_router,
        "reattempt_reservation",
        new=AsyncMock(side_effect=WiringReservationFrozen(RES_ID)),
    ):
        resp = await client.post(
            f"/internal/reservations/{RES_ID}/wiring/retry", headers=TOKEN_HEADER
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_wiring_retry_upstream_maps_to_503(client):
    with patch.object(
        ex_router,
        "reattempt_reservation",
        new=AsyncMock(side_effect=TransientUpstreamError("inventory 503")),
    ):
        resp = await client.post(
            f"/internal/reservations/{RES_ID}/wiring/retry", headers=TOKEN_HEADER
        )
    assert resp.status_code == 503
