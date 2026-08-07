"""Unit tests for the VLAN definition lifecycle (issue #442, Option B).

Covers the transit-inclusive definition-scope derivation (membership switches plus
trunk-transit switches, through-L1 hops, a transit-only switch), define-before-first-add
ordering, create-failure parking the dependent membership build (retryable, correct
last_error, allocation kept), delete_vlan on last-free per switch, the supersession
guard (delete skipped when the number was re-allocated on the fabric), delete-failure
log-and-continue (allocation stays RELEASED, nothing parked), and idempotent
redelivery/heal of the whole apply. Same harness as test_nats_consumer_l2_reconcile:
in-memory SQLite, mocked sandbox, driven through handle_wiring_changed.
"""

import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import (
    _derive_l2_definition_scope,
    _derive_l2_memberships,
    _FetchContext,
    _release_orphaned_allocations,
    handle_wiring_changed,
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
SW_MID = str(uuid.uuid4())
SW_L2_B = str(uuid.uuid4())
SW_L1 = str(uuid.uuid4())
DUT1 = str(uuid.uuid4())
DUT2 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
OTHER_RES = str(uuid.uuid4())
FABRIC = uuid.uuid4()

TEMPLATE_DATA = {"id": "tmpl-1", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


def _dev(device_id, ctype, name="d"):
    return {
        "id": device_id,
        "name": name,
        "template_id": "tmpl-1",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": ctype,
        "field_data": {"ip": "10.0.0.1"},
    }


DEVICES = {
    SW_L2: _dev(SW_L2, "Layer 2 Switch", "L2-A"),
    SW_MID: _dev(SW_MID, "Layer 2 Switch", "L2-MID"),
    SW_L2_B: _dev(SW_L2_B, "Layer 2 Switch", "L2-B"),
    SW_L1: _dev(SW_L1, "Layer 1 Switch", "L1"),
    DUT1: _dev(DUT1, "Server", "dut1"),
    DUT2: _dev(DUT2, "Server", "dut2"),
}


def _wire(device_a, port_a, device_b, port_b):
    return {
        "device_a_id": device_a,
        "port_a": port_a,
        "device_b_id": device_b,
        "port_b": port_b,
        "layer": "L1",
        "physical_connection_id": str(uuid.uuid4()),
        "edge_key": None,
    }


# DUT1 - SW_L2 - (trunk) - SW_MID - (trunk) - SW_L2_B - DUT2. Memberships land only
# on the terminal ports of SW_L2 and SW_L2_B; SW_MID is transit-only.
CHAIN_WIRES = [
    _wire(DUT1, "eth0", SW_L2, "0/0/1"),
    _wire(SW_L2, "0/0/9", SW_MID, "0/0/1"),
    _wire(SW_MID, "0/0/2", SW_L2_B, "0/0/9"),
    _wire(SW_L2_B, "0/0/1", DUT2, "eth0"),
]


async def _device_fetch(device_id, client=None):
    return DEVICES.get(str(device_id))


def _recorder(fail=None, fail_on_switch=None):
    """(execute_fn, calls): calls records (action, device_id, port, vlan_id).

    `fail` fails every call whose action is in the set; `fail_on_switch` restricts the
    failure to calls whose context HERD_device_id matches the given switch id.
    """
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        device_id = context.get("HERD_device_id")
        calls.append((action, device_id, mk.get("port"), mk.get("vlan_id")))
        if action in fail and (fail_on_switch is None or device_id == fail_on_switch):
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn, fork_wires):
    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=fork_wires),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]


async def _reconcile(fork_wires, fork_version=1, execute_fn=None, calls=None):
    if execute_fn is None:
        execute_fn, calls = _recorder()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(execute_fn, fork_wires):
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": fork_version},
            _db_session_factory(),
        )
    return calls


async def _allocation(status="ACTIVE"):
    async with TestSessionLocal() as s:
        rows = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.status == status)))
            .scalars()
            .all()
        )
        return rows


async def _membership_rows():
    async with TestSessionLocal() as s:
        return (await s.execute(select(L2PortAssignment))).scalars().all()


# --- definition-scope derivation (transit-inclusive) ---


async def test_scope_membership_only_switch():
    """A DUT-to-L2 hop puts the switch in BOTH the membership set and the scope."""
    ctx = _FetchContext(None)
    wires = [_wire(DUT1, "eth0", SW_L2, "0/0/1")]
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        scope = await _derive_l2_definition_scope(wires, ctx)
        memberships = await _derive_l2_memberships(wires, ctx)
    assert scope == {SW_L2}
    assert memberships == {(SW_L2, "0/0/1")}


async def test_scope_includes_trunk_transit_switches():
    """The chain's transit-only switch is in the scope but contributes no membership:
    the trunk exclusion is correct for membership and deliberately dropped for scope."""
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        scope = await _derive_l2_definition_scope(CHAIN_WIRES, ctx)
        memberships = await _derive_l2_memberships(CHAIN_WIRES, ctx)
    assert scope == {SW_L2, SW_MID, SW_L2_B}
    assert memberships == {(SW_L2, "0/0/1"), (SW_L2_B, "0/0/1")}
    assert SW_MID not in {sid for sid, _p in memberships}, "transit-only: no membership"


async def test_scope_through_l1_hop_credits_only_l2_side():
    """DUT - L1 - L2: the L1 matrix switch is never in the L2 definition scope."""
    ctx = _FetchContext(None)
    wires = [_wire(DUT1, "eth0", SW_L1, "1/0/1"), _wire(SW_L1, "1/0/2", SW_L2, "0/0/1")]
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        scope = await _derive_l2_definition_scope(wires, ctx)
    assert scope == {SW_L2}


# --- define on allocation ---


async def test_define_runs_before_first_add_per_switch():
    calls = await _reconcile(CHAIN_WIRES)
    creates = [i for i, c in enumerate(calls) if c[0] == "create_vlan"]
    adds = [i for i, c in enumerate(calls) if c[0] == "add_to_vlan"]
    assert creates, "create_vlan was driven on the allocation's first built membership"
    assert adds, "the memberships were driven"
    assert max(creates) < min(adds), "every create_vlan precedes every add_to_vlan"
    # One definition per scope switch, transit included.
    assert {c[1] for c in calls if c[0] == "create_vlan"} == {SW_L2, SW_MID, SW_L2_B}
    assert len(creates) == 3, "exactly one create_vlan per scope switch"
    # No membership op ever targets the transit-only switch.
    assert not [c for c in calls if c[0] == "add_to_vlan" and c[1] == SW_MID]

    vas = await _allocation()
    assert len(vas) == 1
    va = vas[0]
    assert set(va.switch_device_ids) == {SW_L2, SW_MID, SW_L2_B}, "scope is transit-inclusive"
    assert set(va.defined_switch_ids) == {SW_L2, SW_MID, SW_L2_B}
    # The definition op named the allocated VLAN number.
    assert {c[3] for c in calls if c[0] == "create_vlan"} == {va.vlan_id}


async def test_create_failure_parks_membership_build_and_keeps_allocation():
    """A failed create_vlan fails the dependent membership build on that switch: the
    membership row is the FAILED parking spot (intended ACTIVE, retryable last_error),
    no add_to_vlan is driven there, and the allocation stays ACTIVE for the retry."""
    execute_fn, calls = _recorder(fail={"create_vlan"})
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=calls)
    assert not [c for c in calls if c[0] == "add_to_vlan"], (
        "the dependent add_to_vlan must not run after a create failure"
    )
    rows = await _membership_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "FAILED"
    assert row.intended == "ACTIVE"
    assert row.last_error.startswith("create_vlan failed:"), row.last_error
    assert row.attempts > 0
    vas = await _allocation()
    assert len(vas) == 1, "the allocation is kept ACTIVE (nothing built, retry pending)"


async def test_create_failure_retry_defines_then_joins():
    """The parked build retries through the existing direction-scoped channel: the
    reattempt re-drives create_vlan (still pending in defined_switch_ids) before
    add_to_vlan and lands the membership ACTIVE."""
    from contextlib import ExitStack

    from app.services.wiring_retry_service import reattempt_reservation

    execute_fn, calls = _recorder(fail={"create_vlan"})
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=calls)

    retry_fn, retry_calls = _recorder()
    # The retry's build-intent revalidation (issue #491) reads the fork, so the intended
    # wires must still carry the membership or the reattempt would (correctly) park it.
    with ExitStack() as stack:
        for p in _patches(retry_fn, [_wire(DUT1, "eth0", SW_L2, "0/0/1")]):
            stack.enter_context(p)
        result = await reattempt_reservation(RES_ID, _db_session_factory())

    actions = [c[0] for c in retry_calls]
    assert "create_vlan" in actions and "add_to_vlan" in actions
    assert actions.index("create_vlan") < actions.index("add_to_vlan")
    assert result["results"][0]["outcome"] == "reconnected"
    rows = await _membership_rows()
    assert [r.status for r in rows] == ["ACTIVE"]
    vas = await _allocation()
    assert SW_L2 in set(vas[0].defined_switch_ids)


# --- undefine on last-free ---


async def test_delete_on_last_free_per_switch():
    """Releasing the last membership frees the allocation AND undefines the VLAN on
    every switch it was defined on, transit included."""
    await _reconcile(CHAIN_WIRES, fork_version=1)
    calls = await _reconcile([], fork_version=2)

    deletes = [c for c in calls if c[0] == "delete_vlan"]
    assert {c[1] for c in deletes} == {SW_L2, SW_MID, SW_L2_B}, (
        "delete_vlan runs once per defined switch on last-free"
    )
    assert len(deletes) == 3
    removes = [i for i, c in enumerate(calls) if c[0] == "remove_from_vlan"]
    delete_idx = [i for i, c in enumerate(calls) if c[0] == "delete_vlan"]
    assert max(removes) < min(delete_idx), "memberships leave before the definition is deleted"

    assert await _allocation("ACTIVE") == []
    released = await _allocation("RELEASED")
    assert len(released) == 1
    assert released[0].defined_switch_ids == [], "successful deletes clear the defined set"
    assert {c[3] for c in deletes} == {released[0].vlan_id}


async def test_delete_failure_logs_and_continues(caplog):
    """A failed delete_vlan is log-and-continue: the allocation stays RELEASED, no row
    is parked, and the switch stays listed as defined (the accepted bounded lingering)."""
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    execute_fn, calls = _recorder(fail={"delete_vlan"})
    with caplog.at_level(logging.ERROR, logger="app.services.nats_consumer"):
        await _reconcile([], fork_version=2, execute_fn=execute_fn, calls=calls)

    assert [c[0] for c in calls if c[0] == "delete_vlan"], "the delete was attempted"
    assert await _allocation("ACTIVE") == []
    released = await _allocation("RELEASED")
    assert len(released) == 1, "the allocation stays RELEASED despite the delete failure"
    assert released[0].defined_switch_ids == [SW_L2], "the lingering definition stays listed"
    rows = await _membership_rows()
    assert {r.status for r in rows} == {"RELEASED"}, "no FAILED row is parked for a delete failure"
    assert any("lingers" in rec.message for rec in caplog.records), "the loud log line fired"


async def test_delete_skipped_when_vlan_reallocated_on_fabric(caplog):
    """The reuse race (the #424 supersession rule for definitions): when the VLAN number
    has been re-allocated on the fabric by the time the delete would run, the delete is
    skipped so the newer reservation's definition is never stripped."""
    # Seed our allocation as ACTIVE, defined on SW_L2, with zero ACTIVE memberships.
    async with TestSessionLocal() as s:
        mine = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(mine)
        await s.commit()
        await s.refresh(mine)
        va_id = mine.id

    # Simulate the interleaving: the winner allocates the same (fabric, vlan) AFTER our
    # row flips RELEASED (the partial-unique index forbids two ACTIVE rows, so this is
    # the only ordering the race can take) and BEFORE the delete runs. The wrapped
    # session factory injects the winner right before the supersession-check session.
    real_factory = _db_session_factory()
    opened = {"n": 0}

    class _HookedCtx:
        def __init__(self):
            self._inner = real_factory()

        async def __aenter__(self):
            opened["n"] += 1
            if opened["n"] == 2:  # session 1: flip RELEASED; session 2: winner check
                async with TestSessionLocal() as s:
                    s.add(
                        VlanAssignment(
                            reservation_id=uuid.UUID(OTHER_RES),
                            fabric_id=FABRIC,
                            vlan_id=100,
                            switch_device_ids=[SW_L2],
                            defined_switch_ids=[],
                            status="ACTIVE",
                        )
                    )
                    await s.commit()
            return await self._inner.__aenter__()

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    execute_fn, calls = _recorder()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(execute_fn, []):
            stack.enter_context(p)
        with caplog.at_level(logging.WARNING, logger="app.services.nats_consumer"):
            await _release_orphaned_allocations({va_id}, lambda: _HookedCtx(), _FetchContext(None))

    assert calls == [], "no driver call: the delete is superseded by the re-allocation"
    async with TestSessionLocal() as s:
        mine_after = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.id == va_id))
        ).scalar_one()
    assert mine_after.status == "RELEASED"
    assert any("re-allocated" in rec.message for rec in caplog.records)


# --- idempotency ---


async def test_stale_redelivery_makes_no_driver_calls():
    """Redelivering the same fork_version is a stale no-op: zero driver calls."""
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    assert calls == []


async def test_heal_after_converged_apply_is_a_no_op():
    """A delta-less heal at the next version over an already-converged state drives no
    driver calls: memberships are ACTIVE-gated, the scope is fully defined, and the
    define pre-pass has nothing pending. The whole apply is idempotent."""
    await _reconcile(CHAIN_WIRES, fork_version=1)
    calls = await _reconcile(CHAIN_WIRES, fork_version=2)
    assert calls == [], "a converged heal re-drives nothing (create_vlan included)"
    vas = await _allocation()
    assert len(vas) == 1
    assert set(vas[0].defined_switch_ids) == {SW_L2, SW_MID, SW_L2_B}
