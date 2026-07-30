"""Router tests for the internal wiring-status and wiring-retry endpoints.

ADR 0007 Decision 6 (issue #345 P3b phase 4); layered wiring-status since ADR 0009
phase 8 (Decision 7, issue #416). Covers the X-Internal-Token gate on both endpoints,
the wiring-status response shape (including the reservation_wiring_state markers, the
retryable flag, and the layered L2 membership and L3 route-pin rows) and its empty
case, and the retry endpoint's relay of the service outcomes plus its frozen 409 and
upstream 503 mappings. The retry worker is patched so no real driver machinery runs;
wiring-status reads the request-scoped test DB.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
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


async def _seed_l2(port, *, status="ACTIVE", intended="ACTIVE", attempts=0, last_error=None):
    """Seed a VLAN allocation plus one membership row against it; returns the vlan id."""
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=uuid.uuid4(),
            vlan_id=101,
            switch_device_ids=[SWITCH_ID],
            status="ACTIVE",
        )
        s.add(va)
        await s.flush()
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va.id,
                switch_device_id=uuid.UUID(SWITCH_ID),
                port=port,
                status=status,
                intended=intended,
                attempts=attempts,
                last_error=last_error,
            )
        )
        await s.commit()
        return va.id


async def _seed_l3(routes, *, status="ACTIVE", intended="ACTIVE", attempts=0, last_error=None):
    async with TestSessionLocal() as s:
        s.add(
            RouteAssignment(
                reservation_id=uuid.UUID(RES_ID),
                device_id=uuid.UUID(SWITCH_ID),
                routes=routes,
                status=status,
                intended=intended,
                attempts=attempts,
                last_error=last_error,
            )
        )
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
    # Every field the surface promises is present, and an L1 row carries its layer tag
    # (ADR 0009 phase 8) with the pre-layered fields unchanged.
    for c in body["connections"]:
        assert c["layer"] == "l1"
        assert set(c) >= {
            "id",
            "switch_device_id",
            "layer",
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
async def test_wiring_status_layered_l2_and_l3_rows(client):
    """The status surface carries L2 membership and L3 route-pin rows (ADR 0009 phase 8).

    One row per wiring-ledger entry across the three layers, each tagged with `layer`
    and carrying its layer-specific identity: an L2 row names its switch port, its
    vlan_assignment_id, and the allocated fabric VLAN number; an L3 row summarizes its
    pinned set as route_count. retryable follows the same driver-failure rule as L1.
    """
    await _seed_state(frozen=False, last_applied=4)
    await _seed_failed("0/0/1", "0/0/2", attempts=2, last_error="l1 boom")
    va_id = await _seed_l2(
        "p7", status="FAILED", intended="ACTIVE", attempts=3, last_error="add_to_vlan boom"
    )
    await _seed_l3(
        [{"destination": "10.0.0.0/24", "next_hop": "10.0.1.1"}],
        status="FAILED",
        intended="RELEASED",
        attempts=1,
        last_error=WIRING_UNRESOLVABLE_REASON,
    )

    resp = await client.get(f"/internal/reservations/{RES_ID}/wiring-status", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    by_layer = {c["layer"]: c for c in body["connections"]}
    assert set(by_layer) == {"l1", "l2", "l3"}

    l2 = by_layer["l2"]
    assert l2["port"] == "p7"
    assert l2["vlan_assignment_id"] == str(va_id)
    assert l2["vlan"] == 101
    assert l2["status"] == "FAILED"
    assert l2["intended"] == "ACTIVE"
    assert l2["attempts"] == 3
    assert l2["retryable"] is True
    # The L1 pair fields do not apply to a membership row.
    assert l2["port_a"] is None and l2["port_b"] is None

    l3 = by_layer["l3"]
    assert l3["switch_device_id"] == SWITCH_ID
    assert l3["route_count"] == 1
    assert l3["intended"] == "RELEASED"
    # A pinned unresolvable reason is not hardware-retryable, same rule as L1.
    assert l3["retryable"] is False
    assert l3["port"] is None and l3["port_a"] is None


@pytest.mark.asyncio
async def test_wiring_status_l2_unresolvable_allocation_reports_null_vlan(client):
    """A membership row parked against the nil-UUID placeholder reports vlan null.

    The legacy path parks a row with the nil vlan_assignment_id when its allocation
    readback failed; the display surface must not invent a VLAN number for it.
    """
    async with TestSessionLocal() as s:
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=uuid.UUID(int=0),
                switch_device_id=uuid.UUID(SWITCH_ID),
                port="p9",
                status="FAILED",
                intended="ACTIVE",
                attempts=1,
                last_error="no allocation",
            )
        )
        await s.commit()

    resp = await client.get(f"/internal/reservations/{RES_ID}/wiring-status", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    (row,) = resp.json()["connections"]
    assert row["layer"] == "l2"
    assert row["vlan"] is None


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
