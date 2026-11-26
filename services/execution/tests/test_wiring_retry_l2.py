"""Tests for L2 membership rows in the wiring retry channels (ADR 0009 phase 4, issue #416).

Covers direction-aware reattempt (an ACTIVE-intended FAILED membership retries add_to_vlan,
a RELEASED-intended one retries remove_from_vlan), the direction-scoped freeze (a build is
refused on a frozen reservation, a release still runs), the cross-reservation supersession
guard, the last-release allocation free, per-layer labeling in a mixed L1+L2 batch, and the
non-retryable pinned-reason classification (no driver call, issue #564).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import WIRING_UNRESOLVABLE_REASON
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
SW_L1 = str(uuid.uuid4())
DUT = str(uuid.uuid4())
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
L1_SWITCH_DATA = {**SWITCH_DATA, "id": SW_L1, "name": "L1", "connection_type": "Layer 1 Switch"}
TEMPLATE_DATA = {"id": "tmpl-1", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _fork_wire(device_a, port_a, device_b, port_b, edge_key=None):
    return {
        "device_a_id": device_a,
        "port_a": port_a,
        "device_b_id": device_b,
        "port_b": port_b,
        "layer": "L1",
        "physical_connection_id": None,
        "edge_key": edge_key,
    }


# The build-intent revalidation (issue #491) fetches cabling's intended set before any
# build-direction reattempt; the default fork keeps every seeded build intended: the
# (SW_L2, 0/0/1) membership plus an (a1, a2) L1 pair on SW_L1 for the mixed-batch test.
DEFAULT_FORK_WIRES = [
    _fork_wire(DUT, "eth0", SW_L2, "0/0/1"),
    _fork_wire(str(uuid.uuid4()), "eth0", SW_L1, "a1", edge_key="e1"),
    _fork_wire(SW_L1, "a2", str(uuid.uuid4()), "eth0", edge_key="e1"),
]


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


def _patches(execute_fn, fork_wires=None):
    async def _device(device_id, client=None):
        if str(device_id) == SW_L2:
            return SWITCH_DATA
        if str(device_id) == SW_L1:
            return L1_SWITCH_DATA
        # Wire far ends resolve as plain servers so the revalidation derivations work.
        return {"id": str(device_id), "name": "dut", "connection_type": "Server", "field_data": {}}

    from contextlib import ExitStack

    stack = ExitStack()
    for p in [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=DEFAULT_FORK_WIRES if fork_wires is None else fork_wires),
        ),
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


async def test_retry_non_retryable_pinned_reason_makes_no_driver_call():
    """A FAILED L2 membership with a pinned reason is reported not_retryable without a
    driver call (issue #564, mirroring the L1 coverage in test_wiring_retry_service.py)."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=0, err=WIRING_UNRESOLVABLE_REASON)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())
    assert calls == [], "no driver call for a non-retryable intent"
    row = await _l2_row(rid)
    assert row.status == "FAILED"
    assert result["results"] == [
        {
            "id": str(rid),
            "switch_device_id": SW_L2,
            "port": "0/0/1",
            "vlan_assignment_id": str(va),
            "outcome": "not_retryable",
            "status": "FAILED",
            "attempts": 0,
            "last_error": WIRING_UNRESOLVABLE_REASON,
            "layer": "l2",
            "vlan": None,
        }
    ]


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
                switch_device_id=uuid.UUID(SW_L1),
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


async def test_nil_allocation_build_retry_fabric_outage_fails_closed():
    """Issue #1003, decision d, on the retry channel: when cabling cannot answer the
    fabric lookup during re-resolution, nothing is allocated and nothing is driven.
    On the manual channel the error propagates (the route maps it to 503). The tick
    catches it for this reservation alone (issue #1204): the row reads back
    still_failed, counted in skipped_upstream, and stays FAILED with its attempts."""
    from app.services.nats_consumer import TransientUpstreamError

    rid = await _seed_l2_failed("0/0/1", "ACTIVE", uuid.UUID(int=0), attempts=2)
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        with patch(
            "app.services.vlan_service.fetch_fabric_id",
            new=AsyncMock(side_effect=TransientUpstreamError("fabric lookup: upstream 503")),
        ):
            with pytest.raises(TransientUpstreamError, match="upstream 503"):
                await reattempt_reservation(RES_ID, _db_session_factory())
        with patch(
            "app.services.vlan_service.fetch_fabric_id",
            new=AsyncMock(side_effect=TransientUpstreamError("fabric lookup: upstream 503")),
        ):
            stats = await run_wiring_retry_tick(_db_session_factory())

    assert calls == [], "no driver call while the fabric is unknown"
    assert stats["rows_due"] == 1, "the tick did pick the row up"
    assert stats["l2_rows_retried"] == 1
    assert stats["still_failed"] == 1
    assert stats["skipped_upstream"] == 1
    row = await _l2_row(rid)
    assert row.status == "FAILED"
    assert row.attempts == 2
    async with TestSessionLocal() as s:
        assert (await s.execute(select(VlanAssignment))).scalars().all() == []


# --- Build-intent revalidation before a build retry (issue #491) ---


async def test_l2_build_intent_gone_parks_and_makes_no_driver_call():
    from app.services.nats_consumer import WIRING_STALE_BUILD_REASON

    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=3)
    execute_fn, calls = _recorder()
    with _patches(execute_fn, fork_wires=[]):
        result = await reattempt_reservation(RES_ID, _db_session_factory())

    assert calls == [], "no driver call for a join whose intent is gone"
    row = await _l2_row(rid)
    assert row.status == "FAILED"
    assert row.intended == "RELEASED", "the direction flipped to the release channel"
    assert row.attempts == 0, "attempts reset: the release direction has not failed yet"
    assert row.last_error == WIRING_STALE_BUILD_REASON
    assert result["results"][0]["outcome"] == "still_failed"


async def test_l2_parked_stale_join_settles_and_frees_allocation_on_next_pass():
    """The #479 chain shape for L2: pass 1 parks the stale join; pass 2 drives the
    remove_from_vlan with the row's recorded VLAN, the row converges RELEASED, and the
    last-free releases the allocation."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=3)
    execute_fn, calls = _recorder()
    with _patches(execute_fn, fork_wires=[]):
        await reattempt_reservation(RES_ID, _db_session_factory())
    with _patches(execute_fn, fork_wires=[]):
        result = await reattempt_reservation(RES_ID, _db_session_factory())

    assert ("remove_from_vlan", "0/0/1") in calls
    assert not any(a == "add_to_vlan" for a, _p in calls)
    row = await _l2_row(rid)
    assert row.status == "RELEASED"
    assert result["results"][0]["outcome"] == "released"
    async with TestSessionLocal() as s:
        va_row = await s.get(VlanAssignment, va)
    assert va_row.status == "RELEASED", "the settled join was its allocation's last member"


async def test_l2_nil_allocation_stale_build_flips_released_and_mints_no_allocation():
    """A nil-allocation build row whose intent is gone is settled BEFORE re-resolution:
    it flips RELEASED directly (no VLAN number exists to remove and the build provably
    never reached the driver), with zero driver calls and NO allocation minted for the
    dead intent (issue #491)."""
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", uuid.UUID(int=0), attempts=3)
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn, fork_wires=[]):
        with patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)):
            result = await reattempt_reservation(RES_ID, _db_session_factory())

    assert calls == [], "no driver call and no re-resolution for dead intent"
    row = await _l2_row(rid)
    assert row.status == "RELEASED"
    assert result["results"][0]["outcome"] == "released"
    async with TestSessionLocal() as s:
        vas = (await s.execute(select(VlanAssignment))).scalars().all()
    assert vas == [], "revalidation runs BEFORE re-resolution: no allocation is minted"


async def test_l2_fetch_failure_blocks_builds_but_releases_still_run():
    """An intended-set fetch failure fails closed for the build direction only: the
    build row is left untouched and reports still_failed, while the same reservation's
    release-direction row is still driven (issue #460 philosophy)."""
    from app.services.nats_consumer import TransientUpstreamError

    va = await _seed_alloc()
    build_id = await _seed_l2_failed("0/0/1", "ACTIVE", va)
    release_id = await _seed_l2_failed("0/0/2", "RELEASED", va)
    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        with patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(side_effect=TransientUpstreamError("cabling down")),
        ):
            result = await reattempt_reservation(RES_ID, _db_session_factory())

    assert ("add_to_vlan", "0/0/1") not in calls
    assert ("remove_from_vlan", "0/0/2") in calls
    outcomes = {r["id"]: r["outcome"] for r in result["results"]}
    assert outcomes[str(build_id)] == "still_failed"
    assert outcomes[str(release_id)] == "released"
    build_row = await _l2_row(build_id)
    assert build_row.intended == "ACTIVE", "an unverifiable row is left untouched, not parked"
    assert build_row.attempts == 3
    assert build_row.last_error == "boom"


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


async def test_reattempt_l2_rows_skips_id_deleted_before_refresh():
    """_reattempt_l2_rows drives the ORM row objects it is handed directly, then
    refetches by id afterward. A row deleted in that window is skipped in the
    returned outcomes rather than raising, even though its driver call still fired
    (the L2 analogue of the L1 _reattempt_rows race)."""
    from app.services.wiring_retry_service import _reattempt_l2_rows

    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=0)
    async with TestSessionLocal() as db:
        rows = (
            (await db.execute(select(L2PortAssignment).where(L2PortAssignment.id == rid)))
            .scalars()
            .all()
        )
    async with TestSessionLocal() as s:
        victim = await s.get(L2PortAssignment, rid)
        await s.delete(victim)
        await s.commit()

    execute_fn, calls = _recorder()
    with _patches(execute_fn):
        outcomes = await _reattempt_l2_rows(rows, _db_session_factory())

    assert outcomes == [], "a row missing on refresh contributes no outcome, not an error"
    # Issue #817 changed this deliberately: the per-row drive claim is a
    # compare-and-swap on (id, status FAILED), so a row DELETED before its claim can
    # never be claimed and is never driven. Before the claim the apply keyed off the
    # port pair alone and fired the driver for a ledger row that no longer existed,
    # with nothing left to record the outcome against. Not driving it is the point.
    assert ("add_to_vlan", "0/0/1") not in calls, (
        "the deleted row's driver call must not fire: its claim cannot be taken"
    )


# --- a retry's join that loses to the release of its allocation (issue #1413) ---


def _release_at_record_time(allocation_id, *, release_row=None):
    """A stand-in for vlan_service.hold_active_allocation that first commits the release
    of `allocation_id` (and, when given, a save-driven release of the membership row
    `release_row`) from another session: the retry drove add_to_vlan on an allocation it
    read ACTIVE, and the allocation was released before the join was recorded."""
    from app.services import vlan_service
    from app.services.l2_membership_service import release_l2_membership

    real = vlan_service.hold_active_allocation

    async def _hold(db, va_id):
        async with TestSessionLocal() as s:
            row = await s.get(VlanAssignment, allocation_id)
            row.status = "RELEASED"
            await s.commit()
            if release_row is not None:
                await release_l2_membership(s, uuid.UUID(RES_ID), uuid.UUID(SW_L2), release_row)
        return await real(db, va_id)

    return patch("app.services.vlan_service.hold_active_allocation", new=_hold)


@pytest.mark.parametrize("row_released_meanwhile", [True, False])
async def test_a_retry_join_that_loses_its_allocation_writes_only_the_row_it_loaded(
    row_released_meanwhile,
):
    """Through the real retry entry: the join's failure write is the row-identity
    compare-and-swap with the pass's claims (WIRE-LEDGER-8). When a save released the
    row while add_to_vlan ran, nothing is written and no FAILED row is inserted for the
    port (the #814 zombie), and the row joins the pass's lost set. When the row is still
    the one this pass holds, the failure lands on that row id and its claim is cleared."""
    from app.services.l2_membership_service import JOIN_LOST_ALLOCATION_RELEASED
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_l2_rows

    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=2)
    async with TestSessionLocal() as s:
        rows = list(
            (await s.execute(select(L2PortAssignment).where(L2PortAssignment.id == rid)))
            .scalars()
            .all()
        )
    claims = WiringRowClaims()
    execute_fn, calls = _recorder()
    with (
        _patches(execute_fn),
        _release_at_record_time(va, release_row="0/0/1" if row_released_meanwhile else None),
    ):
        await _reattempt_l2_rows(rows, _db_session_factory(), claims=claims)

    assert ("add_to_vlan", "0/0/1") in calls
    async with TestSessionLocal() as s:
        own = (
            (
                await s.execute(
                    select(L2PortAssignment).where(
                        L2PortAssignment.reservation_id == uuid.UUID(RES_ID)
                    )
                )
            )
            .scalars()
            .all()
        )
    (row,) = own
    assert row.id == rid, "no other row is inserted for the port"
    assert row.claimed_until is None
    if row_released_meanwhile:
        assert (row.status, row.attempts, row.last_error) == ("RELEASED", 2, None)
        assert rid in claims.lost
    else:
        assert (row.status, row.intended, row.attempts, row.last_error) == (
            "FAILED",
            "ACTIVE",
            3,
            JOIN_LOST_ALLOCATION_RELEASED,
        )
        assert row.vlan_assignment_id == va


# --- a failed join on an allocation read as released is re-resolved (issue #1413) ---


async def _seed_released_alloc() -> tuple[uuid.UUID, int]:
    """A RELEASED allocation of RES_ID whose number differs from the one a fresh
    allocation of RES_ID takes (its preferred number), so a drive of the released
    number cannot pass for the fresh one."""
    from app.services.vlan_service import _derive_vlan_id

    preferred = _derive_vlan_id(RES_ID)
    released_vlan = preferred + 1 if preferred < 4094 else preferred - 1
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=released_vlan,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[],
            status="RELEASED",
        )
        s.add(va)
        await s.commit()
        return va.id, released_vlan


async def test_a_failed_join_on_a_released_allocation_is_re_resolved_before_its_drive():
    """The row a lost join leaves (and any failed join whose allocation was released
    since) is re-resolved: add_to_vlan names a fresh ACTIVE allocation's number, never
    the released one, and the row heals onto that allocation."""
    from app.services.l2_membership_service import JOIN_LOST_ALLOCATION_RELEASED

    released, released_vlan = await _seed_released_alloc()
    rid = await _seed_l2_failed(
        "0/0/1", "ACTIVE", released, attempts=1, err=JOIN_LOST_ALLOCATION_RELEASED
    )
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        with patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)):
            result = await reattempt_reservation(RES_ID, _db_session_factory())

    async with TestSessionLocal() as s:
        fresh = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE"))
        ).scalar_one()
        old = await s.get(VlanAssignment, released)
    assert fresh.id != released and old.status == "RELEASED"
    add_vlans = [c[2] for c in calls if c[0] == "add_to_vlan"]
    assert add_vlans == [fresh.vlan_id]
    assert released_vlan not in [c[2] for c in calls if c[2] is not None]
    row = await _l2_row(rid)
    assert (row.status, row.vlan_assignment_id) == ("ACTIVE", fresh.id)
    assert result["results"][0]["outcome"] == "reconnected"
    assert result["results"][0]["vlan"] == fresh.vlan_id


async def test_a_stale_failed_join_on_a_released_allocation_is_removed_from_its_number():
    """A failed join on a released allocation whose intent is gone still has that
    allocation's number, which its add_to_vlan may have landed on: it is parked for the
    release channel (not settled with no driver call), and the next pass drives
    remove_from_vlan with the released number."""
    from app.services.l2_membership_service import JOIN_LOST_ALLOCATION_RELEASED
    from app.services.nats_consumer import WIRING_STALE_BUILD_REASON

    released, released_vlan = await _seed_released_alloc()
    rid = await _seed_l2_failed(
        "0/0/1", "ACTIVE", released, attempts=1, err=JOIN_LOST_ALLOCATION_RELEASED
    )
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn, fork_wires=[]):
        await reattempt_reservation(RES_ID, _db_session_factory())
    parked = await _l2_row(rid)
    assert (parked.status, parked.intended, parked.last_error) == (
        "FAILED",
        "RELEASED",
        WIRING_STALE_BUILD_REASON,
    )
    assert calls == []

    with _patches(execute_fn, fork_wires=[]):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert [c for c in calls if c[0] in ("add_to_vlan", "remove_from_vlan")] == [
        ("remove_from_vlan", "0/0/1", released_vlan)
    ]
    assert (await _l2_row(rid)).status == "RELEASED"


# --- a lost join's add is undone, or its removal stays owed and tracked (issue #1413) ---


def _vlan_recorder_failing(*fail_actions):
    """(execute_fn, calls) like _vlan_recorder; an action in `fail_actions` answers a
    driver failure."""
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        calls.append((action, mk.get("port"), mk.get("vlan_id")))
        if action in fail_actions:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _membership_ops(calls):
    """The membership driver calls, one entry per driven op: the in-line retry of a
    failing call (three attempts) is collapsed into one entry."""
    ops = []
    for c in calls:
        if c[0] in ("add_to_vlan", "remove_from_vlan") and (not ops or ops[-1] != c):
            ops.append(c)
    return ops


async def _first_join(va, execute_fn, *extra):
    """One add_to_vlan of (SW_L2, 0/0/1) on `va` through the shared apply, the reconcile
    shape (no retry row, no claims), with the allocation released at record time."""
    from contextlib import ExitStack

    from app.services.nats_consumer import _apply_one_vlan_action

    with ExitStack() as stack:
        stack.enter_context(_patches(execute_fn))
        stack.enter_context(_release_at_record_time(va))
        for p in extra:
            stack.enter_context(p)
        async with TestSessionLocal() as s:
            await _apply_one_vlan_action(
                s,
                "add_to_vlan",
                "/tmp/driver",
                {},
                set(),
                uuid.UUID(SW_L2),
                uuid.UUID(DRIVER_ID),
                "sha",
                uuid.UUID(RES_ID),
                "0/0/1",
                100,
                va,
            )


async def _own_rows():
    async with TestSessionLocal() as s:
        return (
            (
                await s.execute(
                    select(L2PortAssignment).where(
                        L2PortAssignment.reservation_id == uuid.UUID(RES_ID)
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_a_lost_first_join_is_undone_and_left_for_re_allocation():
    from app.services.l2_membership_service import JOIN_LOST_ALLOCATION_RELEASED

    va = await _seed_alloc()
    execute_fn, calls = _vlan_recorder()
    await _first_join(va, execute_fn)

    assert _membership_ops(calls) == [
        ("add_to_vlan", "0/0/1", 100),
        ("remove_from_vlan", "0/0/1", 100),
    ]
    (row,) = await _own_rows()
    assert (row.status, row.intended, row.vlan_assignment_id) == ("FAILED", "ACTIVE", va)
    assert (row.last_error, row.attempts) == (JOIN_LOST_ALLOCATION_RELEASED, 1)


async def test_a_lost_first_join_whose_undo_fails_owes_the_removal_on_the_old_number():
    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL

    va = await _seed_alloc()
    execute_fn, calls = _vlan_recorder_failing("remove_from_vlan")
    await _first_join(va, execute_fn)

    assert ("remove_from_vlan", "0/0/1", 100) in calls
    (row,) = await _own_rows()
    assert (row.status, row.intended, row.vlan_assignment_id) == ("FAILED", "RELEASED", va)
    assert (row.last_error, row.attempts) == (JOIN_UNDO_PENDING_REMOVAL, 1)


async def test_a_lost_first_join_whose_undo_raises_owes_the_removal_and_raises():
    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL

    class _Boom(RuntimeError):
        pass

    va = await _seed_alloc()
    execute_fn, calls = _vlan_recorder()
    with pytest.raises(_Boom):
        await _first_join(
            va,
            execute_fn,
            patch(
                "app.services.l2_membership_service.port_holds_number_live",
                new=AsyncMock(side_effect=_Boom()),
            ),
        )

    assert _membership_ops(calls) == [("add_to_vlan", "0/0/1", 100)]
    (row,) = await _own_rows()
    assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
        "FAILED",
        "RELEASED",
        va,
        JOIN_UNDO_PENDING_REMOVAL,
    )


@pytest.mark.parametrize(
    ("holder", "undone_by_driver"),
    [
        ("same_port_same_number", False),
        ("same_port_other_number", True),
        ("other_port_same_number", True),
        ("same_port_same_number_released_allocation", True),
    ],
)
async def test_the_undo_is_skipped_only_when_a_live_membership_holds_the_number_on_the_port(
    holder, undone_by_driver
):
    """The membership supersession rule: the port is left in the number only when an
    ACTIVE membership on that exact (switch, port) sits on an ACTIVE allocation holding
    that number (here another reservation's, given 100 again after the release)."""
    va = await _seed_alloc()
    number = 100 if "same_number" in holder else 200
    port = "0/0/1" if "same_port" in holder else "0/0/2"
    async with TestSessionLocal() as s:
        rival = VlanAssignment(
            reservation_id=uuid.UUID(OTHER_RES),
            fabric_id=uuid.uuid4(),
            vlan_id=number,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[],
            status="RELEASED" if holder.endswith("released_allocation") else "ACTIVE",
        )
        s.add(rival)
        await s.commit()
        other = rival.id
    await _seed_l2_active(port, other, reservation_id=OTHER_RES)
    execute_fn, calls = _vlan_recorder()
    await _first_join(va, execute_fn)

    removes = [c for c in calls if c[0] == "remove_from_vlan"]
    assert removes == ([("remove_from_vlan", "0/0/1", 100)] if undone_by_driver else [])
    (row,) = await _own_rows()
    assert (row.intended, row.vlan_assignment_id) == ("ACTIVE", va), "nothing is owed"


@pytest.mark.parametrize("undo_ok", [True, False])
async def test_a_lost_join_on_a_reservation_frozen_meanwhile_is_left_for_removal(undo_ok):
    """A freeze that lands after the record-time frozen check: the key-based write
    follows WIRE-LEDGER-17 (a pending release), and a failed undo owes the removal; in
    both cases the release channel then removes the port from the number and the row
    ends RELEASED, never a pending join."""
    from app.services import vlan_service

    va = await _seed_alloc()
    real_hold = vlan_service.hold_active_allocation

    async def _freeze_then_hold(db, va_id):
        async with TestSessionLocal() as s:
            s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=True))
            await s.commit()
        return await real_hold(db, va_id)

    execute_fn, _calls = _vlan_recorder() if undo_ok else _vlan_recorder_failing("remove_from_vlan")
    with patch("app.services.vlan_service.hold_active_allocation", new=_freeze_then_hold):
        await _first_join(va, execute_fn)
    (row,) = await _own_rows()
    assert (row.status, row.intended, row.vlan_assignment_id) == ("FAILED", "RELEASED", va)

    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", 100)]
    (row,) = await _own_rows()
    assert (row.status, row.vlan_assignment_id) == ("RELEASED", va)


async def _owed_row(va, *, attempts=1):
    """A row that owes the removal of a lost join's add from `va`'s number."""
    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL

    return await _seed_l2_failed(
        "0/0/1", "RELEASED", va, attempts=attempts, err=JOIN_UNDO_PENDING_REMOVAL
    )


@pytest.mark.parametrize("undo_ok", [True, False])
async def test_a_lost_retry_join_is_undone_and_its_failed_undo_stays_owed(undo_ok):
    """Through the real retry entry: the undo drives in the same pass, and a failed one
    leaves the row owing the removal (written by row id, claim cleared)."""
    from app.services.l2_membership_service import (
        JOIN_LOST_ALLOCATION_RELEASED,
        JOIN_UNDO_PENDING_REMOVAL,
    )
    from app.services.wiring_retry_service import _reattempt_l2_rows

    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=2)
    execute_fn, calls = _vlan_recorder() if undo_ok else _vlan_recorder_failing("remove_from_vlan")
    async with TestSessionLocal() as s:
        rows = [await s.get(L2PortAssignment, rid)]
    with _patches(execute_fn), _release_at_record_time(va):
        await _reattempt_l2_rows(rows, _db_session_factory())

    assert _membership_ops(calls) == [
        ("add_to_vlan", "0/0/1", 100),
        ("remove_from_vlan", "0/0/1", 100),
    ]
    row = await _l2_row(rid)
    assert (row.vlan_assignment_id, row.claimed_until, row.attempts) == (va, None, 3)
    if undo_ok:
        assert (row.intended, row.last_error) == ("ACTIVE", JOIN_LOST_ALLOCATION_RELEASED)
    else:
        assert (row.intended, row.last_error) == ("RELEASED", JOIN_UNDO_PENDING_REMOVAL)


async def test_an_owed_removal_converges_remove_then_re_allocate_then_add():
    """The retry tick: an owed row is never re-allocated while the removal is owed; a
    failed removal keeps the owed reason (not the driver's text) and the old number; the
    successful removal turns it into a pending join on the same old allocation, with no
    add in that pass; the next tick re-allocates and adds on the new number."""
    from app.services.l2_membership_service import (
        JOIN_LOST_ALLOCATION_RELEASED,
        JOIN_UNDO_PENDING_REMOVAL,
    )

    released, released_vlan = await _seed_released_alloc()
    rid = await _owed_row(released)

    # Tick 1: the removal fails.
    execute_fn, calls = _vlan_recorder_failing("remove_from_vlan")
    with _patches(execute_fn):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", released_vlan)]
    row = await _l2_row(rid)
    assert (row.intended, row.last_error, row.vlan_assignment_id) == (
        "RELEASED",
        JOIN_UNDO_PENDING_REMOVAL,
        released,
    )

    # Tick 2: the removal succeeds; the row is a pending join, still on the old number.
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", released_vlan)]
    row = await _l2_row(rid)
    assert (row.status, row.intended, row.last_error, row.attempts) == (
        "FAILED",
        "ACTIVE",
        JOIN_LOST_ALLOCATION_RELEASED,
        0,
    )
    assert row.vlan_assignment_id == released

    # Tick 3: re-allocated, then added on the new number.
    execute_fn, calls = _vlan_recorder()
    with (
        _patches(execute_fn),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ):
        await reattempt_reservation(RES_ID, _db_session_factory())
    async with TestSessionLocal() as s:
        fresh = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE"))
        ).scalar_one()
    assert _membership_ops(calls) == [("add_to_vlan", "0/0/1", fresh.vlan_id)]
    row = await _l2_row(rid)
    assert (row.status, row.vlan_assignment_id) == ("ACTIVE", fresh.id)


async def test_a_reconcile_add_for_a_port_that_owes_a_removal_is_not_driven():
    """The apply drives no add, no define, and no build write for such a port: the row
    stays on the old number, owing the removal."""
    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL
    from app.services.nats_consumer import _apply_l2_memberships, _FetchContext

    released, _ = await _seed_released_alloc()
    rid = await _owed_row(released)
    fresh = await _seed_alloc(vlan_id=300)
    add = {"switch_device_id": SW_L2, "port": "0/0/1", "vlan_assignment_id": fresh, "vlan_id": 300}
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        await _apply_l2_memberships(RES_ID, [], [add], _FetchContext(None), _db_session_factory())
    assert calls == []
    row = await _l2_row(rid)
    assert (row.intended, row.last_error, row.vlan_assignment_id) == (
        "RELEASED",
        JOIN_UNDO_PENDING_REMOVAL,
        released,
    )


async def test_the_flip_and_the_failure_writes_keep_a_row_on_the_number_it_owes_a_removal_from():
    """The judges behind the apply's check: the FAILED-to-ACTIVE flip refuses the row,
    a key-based build failure leaves it, and a release failure keeps its reason."""
    from app.services.l2_membership_service import (
        JOIN_UNDO_PENDING_REMOVAL,
        record_l2_failed,
        record_l2_membership_active,
    )

    released, _ = await _seed_released_alloc()
    rid = await _owed_row(released)
    fresh = await _seed_alloc(vlan_id=300)
    async with TestSessionLocal() as s:
        await record_l2_membership_active(s, RES_ID, fresh, SW_L2, "0/0/1")
    async with TestSessionLocal() as s:
        await record_l2_failed(
            s, RES_ID, fresh, SW_L2, "0/0/1", 1, "create_vlan failed: x", intended="ACTIVE"
        )
    async with TestSessionLocal() as s:
        await record_l2_failed(
            s, RES_ID, released, SW_L2, "0/0/1", 1, "driver text", intended="RELEASED"
        )
    (row,) = await _own_rows()
    assert (row.id, row.status, row.intended, row.vlan_assignment_id) == (
        rid,
        "FAILED",
        "RELEASED",
        released,
    )
    assert (row.last_error, row.attempts) == (JOIN_UNDO_PENDING_REMOVAL, 2)


async def test_the_teardown_removes_a_port_that_owes_a_removal():
    from app.services.nats_consumer import _FetchContext, _teardown_from_ledgers

    released, released_vlan = await _seed_released_alloc()
    rid = await _owed_row(released)
    await _seed_state(frozen=True)
    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        await _teardown_from_ledgers(RES_ID, _FetchContext(None), _db_session_factory())
    assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", released_vlan)]
    assert (await _l2_row(rid)).status == "RELEASED"


# --- an owed removal is superseded only on its own number (issue #1413) ---


@pytest.mark.parametrize("owes_undo", [True, False])
@pytest.mark.parametrize(
    "holder",
    [
        "none",
        "same_port_other_number",
        "same_port_same_number",
        "same_port_same_number_released_allocation",
        "other_port_same_number",
    ],
)
async def test_an_owed_removal_is_superseded_only_by_a_live_holder_of_its_own_number(
    owes_undo, holder
):
    """Through the retry entry. A row that owes a lost join's removal from number N is
    settled with no driver call only when an ACTIVE membership on that (switch, port)
    sits on an ACTIVE allocation holding N; a membership in another number leaves the
    port in N (an add is per VLAN), so the removal is still driven. Any other
    release-direction row keeps WIRE-LEDGER-16 as it was: another reservation's ACTIVE
    row on the port supersedes it, whatever its number."""
    from app.services.l2_membership_service import (
        JOIN_LOST_ALLOCATION_RELEASED,
        JOIN_UNDO_PENDING_REMOVAL,
    )

    released, released_vlan = await _seed_released_alloc()
    rid = await _seed_l2_failed(
        "0/0/1",
        "RELEASED",
        released,
        attempts=1,
        err=JOIN_UNDO_PENDING_REMOVAL if owes_undo else "remove_from_vlan failed: boom",
    )
    if holder != "none":
        async with TestSessionLocal() as s:
            rival = VlanAssignment(
                reservation_id=uuid.UUID(OTHER_RES),
                fabric_id=uuid.uuid4(),
                vlan_id=released_vlan if "same_number" in holder else 777,
                switch_device_ids=[SW_L2],
                defined_switch_ids=[SW_L2],
                status="RELEASED" if holder.endswith("released_allocation") else "ACTIVE",
            )
            s.add(rival)
            await s.commit()
            rival_id = rival.id
        foreign = await _seed_l2_active(
            "0/0/1" if holder.startswith("same_port") else "0/0/2",
            rival_id,
            reservation_id=OTHER_RES,
        )

    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        result = await reattempt_reservation(RES_ID, _db_session_factory())

    if owes_undo:
        superseded = holder == "same_port_same_number"
    else:
        superseded = holder.startswith("same_port")
    row = await _l2_row(rid)
    if superseded:
        assert _membership_ops(calls) == []
        assert row.status == "RELEASED"
        assert result["results"][0]["outcome"] == "superseded"
    else:
        assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", released_vlan)]
        if owes_undo:
            assert (row.status, row.intended, row.last_error) == (
                "FAILED",
                "ACTIVE",
                JOIN_LOST_ALLOCATION_RELEASED,
            )
        else:
            assert row.status == "RELEASED"
    if holder != "none":
        kept = await _l2_row(foreign)
        assert (kept.status, kept.intended) == ("ACTIVE", "ACTIVE")


@pytest.mark.parametrize("ledger_owes_undo", [True, False])
async def test_whether_a_row_owes_its_removal_is_read_from_the_ledger_not_the_loaded_row(
    ledger_owes_undo,
):
    """The retry loads its rows before the supersession check, and another pass can
    change a row in between (a lost join's key-based write makes a release row owe the
    removal). The check reads the row as it stands: a holder in another number
    supersedes the row only when the ledger, not the loaded copy, says it owes nothing."""
    from app.services.l2_membership_service import (
        JOIN_UNDO_PENDING_REMOVAL,
        supersede_l2_release_if_reclaimed,
    )

    released, _ = await _seed_released_alloc()
    plain = "remove_from_vlan failed: boom"
    rid = await _seed_l2_failed(
        "0/0/1",
        "RELEASED",
        released,
        attempts=1,
        err=JOIN_UNDO_PENDING_REMOVAL if ledger_owes_undo else plain,
    )
    other = await _seed_alloc(reservation_id=OTHER_RES, vlan_id=777)
    await _seed_l2_active("0/0/1", other, reservation_id=OTHER_RES)
    loaded = await _l2_row(rid)
    loaded.last_error = plain if ledger_owes_undo else JOIN_UNDO_PENDING_REMOVAL

    async with TestSessionLocal() as s:
        superseded = await supersede_l2_release_if_reclaimed(s, loaded)
    assert superseded is (not ledger_owes_undo)
    assert (await _l2_row(rid)).status == ("FAILED" if ledger_owes_undo else "RELEASED")


# --- a cancelled undo still records the owed removal (issue #1413) ---


def _sandbox_blocking_on(action_name, started):
    """A stand-in for nats_consumer._run_sandbox that answers success, except that
    `action_name` sets `started` and then waits until its task is cancelled."""
    import asyncio

    async def _sandbox(driver_path, action, context, **kwargs):
        if action == action_name:
            started.set()
            await asyncio.Event().wait()
        return SUCCESS_RESULT

    return patch("app.services.nats_consumer._run_sandbox", new=_sandbox)


@pytest.mark.parametrize("path", ["reconcile", "retry"])
@pytest.mark.parametrize("cancelled_in", ["supersession_check", "driver_call"])
async def test_a_cancelled_undo_records_the_removal_as_owed_and_stays_cancelled(path, cancelled_in):
    """A cancellation that lands inside the undo, at its supersession check or in its
    remove_from_vlan (a real task.cancel), may leave the add on the port: the row is
    recorded owing the removal from the released number, by key on the reconcile and by
    row id with the pass's claims on the retry, and the CancelledError still goes on."""
    import asyncio

    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_l2_rows

    va = await _seed_alloc()
    started = asyncio.Event()
    execute_fn, _calls = _vlan_recorder()
    if cancelled_in == "driver_call":
        interrupt = _sandbox_blocking_on("remove_from_vlan", started)
    else:
        interrupt = patch(
            "app.services.l2_membership_service.port_holds_number_live",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        )

    if path == "reconcile":
        work = _first_join(va, execute_fn, interrupt)
    else:
        rid = await _seed_l2_failed("0/0/1", "ACTIVE", va, attempts=2)
        async with TestSessionLocal() as s:
            rows = [await s.get(L2PortAssignment, rid)]

        async def _retry():
            with _patches(execute_fn), _release_at_record_time(va), interrupt:
                await _reattempt_l2_rows(rows, _db_session_factory(), claims=WiringRowClaims())

        work = _retry()

    task = asyncio.create_task(work)
    if cancelled_in == "driver_call":
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await _own_rows()
    assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
        "FAILED",
        "RELEASED",
        va,
        JOIN_UNDO_PENDING_REMOVAL,
    )
    assert row.claimed_until is None
    if path == "retry":
        assert (row.id, row.attempts) == (rid, 3)
    else:
        assert row.attempts == 1


async def test_a_failed_owed_write_after_a_cancelled_undo_is_logged_and_the_cancel_wins(
    caplog,
):
    """When the owed write itself raises after an interrupted undo, one line names the
    write's error class and the cancellation, not the write's error, is what goes on."""
    import asyncio
    import logging

    va = await _seed_alloc()
    execute_fn, _calls = _vlan_recorder()
    caplog.set_level(logging.ERROR, logger="app.services.l2_membership_service")
    with pytest.raises(asyncio.CancelledError):
        await _first_join(
            va,
            execute_fn,
            patch(
                "app.services.l2_membership_service.port_holds_number_live",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch(
                "app.services.l2_membership_service.record_l2_failed",
                new=AsyncMock(side_effect=RuntimeError("db gone")),
            ),
        )
    lines = [
        r for r in caplog.records if getattr(r, "action", None) == "l2_join_undo_owed_write_failed"
    ]
    assert len(lines) == 1
    assert lines[0].error_class == "RuntimeError"
    assert "db gone" not in lines[0].getMessage()
    assert await _own_rows() == []


# --- the lost join's rollback comes before its undo (issue #1413) ---


@pytest.mark.parametrize("undo_ok", [True, False])
async def test_a_lost_join_rolls_back_its_park_of_a_frozen_holder_before_the_undo(undo_ok):
    """The record parks a frozen other reservation's ACTIVE row on the port in the
    join's session before it learns the join lost. The undo commits in that session
    (its execution run), so the rollback must come first: the frozen holder's row is
    left ACTIVE, whether the undo succeeds or fails."""
    from app.services.nats_consumer import _apply_one_vlan_action

    va = await _seed_alloc()
    other_va = await _seed_alloc(reservation_id=OTHER_RES, vlan_id=555)
    foreign = await _seed_l2_active("0/0/1", other_va, reservation_id=OTHER_RES)
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(OTHER_RES), frozen=True))
        await s.commit()
    execute_fn, calls = _vlan_recorder() if undo_ok else _vlan_recorder_failing("remove_from_vlan")
    with (
        _patches(execute_fn),
        patch(
            "app.services.vlan_service.hold_active_allocation", new=AsyncMock(return_value=False)
        ),
    ):
        async with TestSessionLocal() as s:
            await _apply_one_vlan_action(
                s,
                "add_to_vlan",
                "/tmp/driver",
                {},
                set(),
                uuid.UUID(SW_L2),
                uuid.UUID(DRIVER_ID),
                "sha",
                uuid.UUID(RES_ID),
                "0/0/1",
                100,
                va,
            )
    assert ("remove_from_vlan", "0/0/1", 100) in _membership_ops(calls)
    kept = await _l2_row(foreign)
    assert (kept.status, kept.intended, kept.last_error) == ("ACTIVE", "ACTIVE", None)
    (own,) = await _own_rows()
    assert own.intended == ("ACTIVE" if undo_ok else "RELEASED")


# --- what one row per (reservation, switch, port) cannot track (issue #1413) ---


async def test_a_failed_undo_written_by_key_takes_a_concurrent_active_row_and_converges():
    """Known limit, pinned: pass B recorded this reservation's port ACTIVE on N' while
    pass A's join on N was in flight; A loses and its undo fails, and its key-based
    owed write takes B's row onto N (the port is in both numbers, one row names one).
    N' is then untracked until the row converges: the retry removes the port from N,
    the row becomes a pending join, and the next retry re-allocates it onto N' and adds
    it there again."""
    from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL

    va = await _seed_alloc()
    fresh = await _seed_alloc(vlan_id=300)
    b_row = await _seed_l2_active("0/0/1", fresh)
    execute_fn, _calls = _vlan_recorder_failing("remove_from_vlan")
    await _first_join(va, execute_fn)
    row = await _l2_row(b_row)
    assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
        "FAILED",
        "RELEASED",
        va,
        JOIN_UNDO_PENDING_REMOVAL,
    )

    execute_fn, calls = _vlan_recorder()
    with _patches(execute_fn):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert _membership_ops(calls) == [("remove_from_vlan", "0/0/1", 100)]

    execute_fn, calls = _vlan_recorder()
    with (
        _patches(execute_fn),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ):
        await reattempt_reservation(RES_ID, _db_session_factory())
    assert _membership_ops(calls) == [("add_to_vlan", "0/0/1", 300)]
    (row,) = await _own_rows()
    assert (row.id, row.status, row.vlan_assignment_id) == (b_row, "ACTIVE", fresh)


async def test_a_frozen_join_recorded_on_a_port_that_owes_a_removal_moves_the_row_to_its_number():
    """Known limit, pinned: a join on N' that passed the apply's check before the row
    owed a removal from N, and is recorded after the reservation froze, is parked on
    N' over the owed row (the port is in both numbers, one row names one); N is then
    untracked, and the release channel removes the port from N' only."""
    from app.services.l2_membership_service import (
        FROZEN_JOIN_PENDING_REMOVAL,
        record_l2_membership_active,
    )

    released, _ = await _seed_released_alloc()
    rid = await _owed_row(released)
    fresh = await _seed_alloc(vlan_id=300)
    await _seed_state(frozen=True)
    async with TestSessionLocal() as s:
        await record_l2_membership_active(s, RES_ID, fresh, SW_L2, "0/0/1")
    row = await _l2_row(rid)
    assert (row.status, row.intended, row.vlan_assignment_id, row.last_error) == (
        "FAILED",
        "RELEASED",
        fresh,
        FROZEN_JOIN_PENDING_REMOVAL,
    )
