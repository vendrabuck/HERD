"""Tests for L2 membership rows in the wiring retry channels (ADR 0009 phase 4, issue #416).

Covers direction-aware reattempt (an ACTIVE-intended FAILED membership retries add_to_vlan,
a RELEASED-intended one retries remove_from_vlan), the direction-scoped freeze (a build is
refused on a frozen reservation, a release still runs), the cross-reservation supersession
guard, the last-release allocation free, and per-layer labeling in a mixed L1+L2 batch.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.vlan_assignment import VlanAssignment
from app.services.wiring_retry_service import (
    WiringReservationFrozen,
    reattempt_reservation,
    run_wiring_retry_tick,
)
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

    return lambda: _Ctx()


SW_L2 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
OTHER_RES = str(uuid.uuid4())
FABRIC = uuid.uuid4()

SWITCH_DATA = {
    "id": SW_L2,
    "name": "L2",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 2 Switch",
    "field_data": {"ip": "10.0.0.1"},
}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _recorder(fail=None):
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        calls.append((action, mk.get("port")))
        if (action, mk.get("port")) in fail:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn):
    async def _device(device_id, client=None):
        return SWITCH_DATA if str(device_id) == SW_L2 else None

    from contextlib import ExitStack

    stack = ExitStack()
    for p in [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]:
        stack.enter_context(p)
    return stack


async def _seed_alloc(reservation_id=RES_ID, vlan_id=100, sids=None, defined=None):
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(reservation_id),
            fabric_id=FABRIC,
            vlan_id=vlan_id,
            switch_device_ids=sids or [SW_L2],
            defined_switch_ids=defined or [],
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        await s.refresh(va)
        return va.id


async def _seed_l2_failed(port, intended, va_id, *, reservation_id=RES_ID, attempts=3, err="boom"):
    async with TestSessionLocal() as s:
        row = L2PortAssignment(
            reservation_id=uuid.UUID(reservation_id),
            vlan_assignment_id=va_id,
            switch_device_id=uuid.UUID(SW_L2),
            port=port,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error=err,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_l2_active(port, va_id, *, reservation_id=RES_ID):
    async with TestSessionLocal() as s:
        row = L2PortAssignment(
            reservation_id=uuid.UUID(reservation_id),
            vlan_assignment_id=va_id,
            switch_device_id=uuid.UUID(SW_L2),
            port=port,
            status="ACTIVE",
            intended="ACTIVE",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_state(frozen=False):
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=frozen))
        await s.commit()


async def _l2_row(row_id):
    async with TestSessionLocal() as s:
        return (
            await s.execute(select(L2PortAssignment).where(L2PortAssignment.id == row_id))
        ).scalar_one()


# --- direction-aware reattempt ---


async def test_build_direction_retry_drives_add_and_ends_active():
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert ("add_to_vlan", "0/0/1") in calls
    row = await _l2_row(rid)
    assert row.status == "ACTIVE"
    out = result["results"][0]
    assert out["outcome"] == "reconnected"
    assert out["layer"] == "l2"
    assert out["port"] == "0/0/1"
    assert out["vlan"] == 100


async def test_release_direction_retry_drives_remove_and_frees_last_allocation():
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "RELEASED", va)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert ("remove_from_vlan", "0/0/1") in calls
    row = await _l2_row(rid)
    assert row.status == "RELEASED"
    assert result["results"][0]["outcome"] == "released"
    # The last membership left, so its allocation is freed.
    async with TestSessionLocal() as s:
        active = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE")))
            .scalars()
            .all()
        )
    assert active == []


async def test_release_retry_repeat_failure_stays_failed_released():
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "RELEASED", va, attempts=2)
    execute_fn, _calls = _recorder(fail={("remove_from_vlan", "0/0/1")})
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    row = await _l2_row(rid)
    assert row.status == "FAILED"
    assert row.intended == "RELEASED"
    assert row.attempts > 2, "attempts accumulate on repeat failure"
    assert result["results"][0]["outcome"] == "still_failed"


# --- frozen direction scoping ---


async def test_frozen_pure_build_l2_raises():
    va = await _seed_alloc()
    await _seed_l2_failed("0/0/1", "ACTIVE", va)
    await _seed_state(frozen=True)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        with pytest.raises(WiringReservationFrozen):
            await reattempt_reservation(RES_ID, _db_session_factory())
    assert calls == [], "no driver call for a frozen pure-build retry"


async def test_frozen_processes_release_reports_build_frozen():
    va = await _seed_alloc()
    build_id = await _seed_l2_failed("0/0/1", "ACTIVE", va)
    release_id = await _seed_l2_failed("0/0/2", "RELEASED", va)
    await _seed_state(frozen=True)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    outcomes = {r["id"]: r["outcome"] for r in result["results"]}
    assert outcomes[str(build_id)] == "frozen"
    assert outcomes[str(release_id)] == "released"
    assert ("add_to_vlan", "0/0/1") not in calls, "the build is not driven on a frozen reservation"
    assert ("remove_from_vlan", "0/0/2") in calls


# --- supersession ---


async def test_release_superseded_settles_without_driver_call():
    va1 = await _seed_alloc()
    va2 = await _seed_alloc(reservation_id=OTHER_RES, vlan_id=200)
    rid = await _seed_l2_failed("0/0/1", "RELEASED", va1)
    # Another reservation now holds the same (switch, port) ACTIVE: the port was reclaimed.
    await _seed_l2_active("0/0/1", va2, reservation_id=OTHER_RES)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert calls == [], "no driver call: the re-wire already removed the old membership"
    assert result["results"][0]["outcome"] == "superseded"
    row = await _l2_row(rid)
    assert row.status == "RELEASED"
    # Issue #479: the superseded row was its allocation's last membership, and the
    # settlement path (not only _apply_l2_memberships) must free the allocation.
    async with TestSessionLocal() as s:
        va_row = await s.get(VlanAssignment, va1)
        assert va_row.status == "RELEASED"


# --- mixed L1 + L2 batch, per-layer labeling ---


async def test_tick_labels_layers_and_counts_per_layer(monkeypatch):
    monkeypatch.setattr("app.config.settings.wiring_retry_batch_size", 10)
    monkeypatch.setattr("app.config.settings.wiring_retry_max_attempts", 100)
    va = await _seed_alloc()
    await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=1)
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SW_L2),
                port_a="a1",
                port_b="a2",
                status="FAILED",
                intended="ACTIVE",
                attempts=1,
                last_error="boom",
            )
        )
        await s.commit()

    execute_fn, _calls = _recorder()
    with _patches(execute_fn):
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert stats["rows_due"] == 2
    assert stats["l1_rows_retried"] == 1
    assert stats["l2_rows_retried"] == 1
    assert stats["rows_retried"] == 2
    assert stats["reconnected"] == 2


# --- nil-allocation build rows re-resolve before driving the driver ---


def _vlan_recorder():
    """(execute_fn, calls): calls records (action, port, vlan_id) for every driver call."""
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        calls.append((action, mk.get("port"), mk.get("vlan_id")))
        return SUCCESS_RESULT

    return execute_fn, calls


async def test_nil_allocation_build_retry_reresolves_and_joins_real_vlan():
    """A build row parked with the nil-UUID allocation (the legacy no-readback park) is
    re-resolved on retry: the driver sees the REAL fabric VLAN, and the row's allocation
    heals to the resolved vlan_assignment (never vlan 0 pointing at the nil allocation)."""
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", uuid.UUID(int=0))
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        with patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)):
            result = await reattempt_reservation(RES_ID, _db_session_factory())

    # The fabric's VLAN was allocated fresh by the re-resolution; capture it.
    async with TestSessionLocal() as s:
        va = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE"))
        ).scalar_one()
    add_calls = [c for c in calls if c[0] == "add_to_vlan"]
    assert add_calls, "the retry never drove add_to_vlan"
    assert add_calls[0][2] == va.vlan_id, "the driver saw the resolved VLAN, not 0"
    assert add_calls[0][2] != 0

    row = await _l2_row(rid)
    assert row.status == "ACTIVE"
    assert row.vlan_assignment_id == va.id, "the nil placeholder healed to the real allocation"
    out = result["results"][0]
    assert out["outcome"] == "reconnected"
    assert out["vlan"] == va.vlan_id


async def test_nil_allocation_build_retry_resolution_failure_makes_no_driver_call():
    """When re-resolution fails again, the row is NOT driven: it re-parks FAILED with the
    no-allocation reason and reports still_failed, with zero driver calls for it."""
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", uuid.UUID(int=0), attempts=2)
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        # Force re-resolution to yield no allocation for the switch.
        with patch(
            "app.services.nats_consumer._resolve_add_allocations",
            new=AsyncMock(return_value={}),
        ):
            result = await reattempt_reservation(RES_ID, _db_session_factory())

    assert calls == [], "no driver call when the allocation cannot be resolved"
    row = await _l2_row(rid)
    assert row.status == "FAILED"
    assert row.attempts > 2, "attempts accumulate on the re-park"
    assert result["results"][0]["outcome"] == "still_failed"


async def test_superseded_settlement_undefines_provably_defined_vlan():
    """Issue #479 + #442: a superseded settlement that last-frees an allocation with
    a provable definition scope drives delete_vlan through the shared release."""
    va1 = await _seed_alloc(defined=[SW_L2])
    va2 = await _seed_alloc(reservation_id=OTHER_RES, vlan_id=200)
    await _seed_l2_failed("0/0/1", "RELEASED", va1)
    await _seed_l2_active("0/0/1", va2, reservation_id=OTHER_RES)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert result["results"][0]["outcome"] == "superseded"
    actions = [a for a, _ in calls]
    assert "delete_vlan" in actions, "the freed allocation's provable definition is undefined"
    assert not {"add_to_vlan", "remove_from_vlan"} & set(actions), "no membership ops"

    async with TestSessionLocal() as s:
        va_row = await s.get(VlanAssignment, va1)
        assert va_row.status == "RELEASED"
