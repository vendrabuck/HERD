"""Consumer-level tests for the L1 applied-state projection (issue #345 P3b phase 1).

Drives handle_reservation_event through the same fixtures the existing L1 consumer
tests use (mocked resolve + sandbox), and asserts the new l1_connection_assignments
rows and the reservation_wiring_state.frozen flag, without changing driver-call
behavior.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.services.nats_consumer import handle_reservation_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())

SWITCH_DATA = {
    "id": SWITCH_ID,
    "name": "L1-Switch",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "L1 Template",
    "sections": [{"name": "Net", "fields": [{"key": "ip", "type": "string"}]}],
}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}
OPS = [{"switch_device_id": SWITCH_ID, "switch_port_a": "0/0/1", "switch_port_b": "0/0/2"}]


def _make_event(event_type):
    return {
        "event": event_type,
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": ["dut-1", "dut-2"],
    }


def _event_patches(execute_fn=None):
    if execute_fn is None:

        def execute_fn(*a, **kw):
            return SUCCESS_RESULT

    return [
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new=AsyncMock(return_value=OPS),
        ),
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(return_value=SWITCH_DATA)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch("app.services.nats_consumer._execute_l2_switch_operations", new=AsyncMock()),
        patch("app.services.nats_consumer._execute_l3_switch_operations", new=AsyncMock()),
        patch("app.services.nats_consumer._execute_dynamic_teardown", new=AsyncMock()),
    ]


async def _run_event(event_type, execute_fn=None):
    patches = _event_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await handle_reservation_event(_make_event(event_type), _db_session_factory())
    finally:
        for p in patches:
            p.stop()


async def _assignments():
    async with TestSessionLocal() as s:
        return list((await s.execute(select(L1ConnectionAssignment))).scalars().all())


async def _wiring_state(reservation_id):
    async with TestSessionLocal() as s:
        return (
            await s.execute(
                select(ReservationWiringState).where(
                    ReservationWiringState.reservation_id == uuid.UUID(reservation_id)
                )
            )
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_created_records_active_assignment():
    """A successful connect_ports on reservation.created records an ACTIVE row."""
    await _run_event("reservation.created")

    rows = await _assignments()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ACTIVE"
    assert row.reservation_id == uuid.UUID(RES_ID)
    assert row.switch_device_id == uuid.UUID(SWITCH_ID)
    assert (row.port_a, row.port_b) == ("0/0/1", "0/0/2")


@pytest.mark.asyncio
async def test_sandbox_failed_connect_records_failed_row():
    """A sandbox-level connect failure (raise/timeout: result success falsy) writes no
    ACTIVE row and lands a visible FAILED row (issue #345 phase-4 live-gate fix)."""
    fail = {"success": False, "output": None, "error": "boom", "duration_ms": 1}

    def execute_fn(driver_path, action, context, **kwargs):
        return SUCCESS_RESULT if action in ("login", "logout") else fail

    await _run_event("reservation.created", execute_fn=execute_fn)
    rows = await _assignments()
    assert [r for r in rows if r.status == "ACTIVE"] == []
    failed = [r for r in rows if r.status == "FAILED"]
    assert len(failed) == 1
    assert (failed[0].port_a, failed[0].port_b) == ("0/0/1", "0/0/2")
    assert failed[0].last_error == "boom"


@pytest.mark.asyncio
async def test_driver_result_failure_on_connect_records_failed_row():
    """The live-gate bug: a driver that RETURNS success=False without raising (sandbox
    transport success is True, the driver's own payload reports failure) must land a
    FAILED run and a FAILED assignment row, never a false SUCCESS + phantom ACTIVE row.
    """
    driver_result_fail = {
        "success": True,
        "output": {"success": False, "error": "mock injected failure on connect_ports"},
        "error": None,
        "duration_ms": 1,
    }

    def execute_fn(driver_path, action, context, **kwargs):
        return SUCCESS_RESULT if action in ("login", "logout") else driver_result_fail

    await _run_event("reservation.created", execute_fn=execute_fn)
    rows = await _assignments()
    assert [r for r in rows if r.status == "ACTIVE"] == [], "no phantom ACTIVE on a failed connect"
    failed = [r for r in rows if r.status == "FAILED"]
    assert len(failed) == 1
    assert failed[0].last_error == "mock injected failure on connect_ports"


@pytest.mark.asyncio
async def test_cancelled_releases_active_assignment_and_freezes():
    """A successful disconnect flips the reservation's ACTIVE row to RELEASED, and
    the terminal event freezes the wiring state."""
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SWITCH_ID),
                port_a="0/0/1",
                port_b="0/0/2",
                status="ACTIVE",
            )
        )
        await s.commit()

    await _run_event("reservation.cancelled")

    rows = await _assignments()
    assert len(rows) == 1
    assert rows[0].status == "RELEASED"
    assert rows[0].released_at is not None

    state = await _wiring_state(RES_ID)
    assert state is not None
    assert state.frozen is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["reservation.cancelled", "reservation.completed", "reservation.failed"]
)
async def test_each_terminal_event_sets_frozen(event_type):
    """Every terminal event handler sets reservation_wiring_state.frozen = true."""
    await _run_event(event_type)
    state = await _wiring_state(RES_ID)
    assert state is not None
    assert state.frozen is True


@pytest.mark.asyncio
async def test_created_does_not_freeze():
    """reservation.created is not terminal, so it must not set frozen."""
    await _run_event("reservation.created")
    assert await _wiring_state(RES_ID) is None
