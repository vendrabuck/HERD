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

import json
import logging
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import (
    TransientUpstreamError,
    _derive_l2_definition_scope,
    _derive_l2_memberships,
    _FetchContext,
    _release_orphaned_allocations,
    handle_wiring_changed,
)
from herd_common.logging import JSONFormatter
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
SW_L2_C = str(uuid.uuid4())
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
    SW_L2_C: _dev(SW_L2_C, "Layer 2 Switch", "L2-C"),
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


# --- supersession judged by CURRENT reachability (issue #1065) ---


def _fabric_map(mapping):
    """A fetch_fabric_id stand-in answering each switch's CURRENT fabric id."""

    async def _fetch(device_id):
        return mapping[str(device_id)]

    return AsyncMock(side_effect=_fetch)


async def _seed_allocation(reservation_id, fabric_id, vlan_id, switches, defined):
    async with TestSessionLocal() as s:
        row = VlanAssignment(
            reservation_id=uuid.UUID(reservation_id),
            fabric_id=fabric_id,
            vlan_id=vlan_id,
            switch_device_ids=list(switches),
            defined_switch_ids=list(defined),
            status="ACTIVE",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _release(va_ids, fetch, factory=None):
    """Run _release_orphaned_allocations with the CURRENT-fabric lookup `fetch`."""
    execute_fn, calls = _recorder()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(execute_fn, [])[:-1]:
            stack.enter_context(p)
        stack.enter_context(patch("app.services.vlan_service.fetch_fabric_id", new=fetch))
        await _release_orphaned_allocations(
            set(va_ids), factory or _db_session_factory(), _FetchContext(None)
        )
    return calls


async def test_delete_skipped_when_a_cable_joined_the_switch_to_the_winners_component(caplog):
    """Our allocation was made while SW_L2 stood alone (stored fabric F_OLD); a cable
    then joined SW_L2 to SW_MID, re-keying the component to F_NEW, and another
    reservation holds the same number there. The stored ids differ, but SW_L2 now
    reaches the winner, so deleting the VLAN on SW_L2 would strip the winner's
    definition: the delete is superseded."""
    f_old, f_new = uuid.uuid4(), uuid.uuid4()
    mine = await _seed_allocation(RES_ID, f_old, 100, [SW_L2], [SW_L2])
    # The winner sits on SW_MID only; the partial-unique index allows it because the
    # stored fabric ids differ (the limit WIRE-VLAN-2 records).
    await _seed_allocation(OTHER_RES, f_new, 100, [SW_MID], [SW_MID])
    fetch = _fabric_map({SW_L2: f_new, SW_MID: f_new})

    with caplog.at_level(logging.WARNING, logger="app.services.nats_consumer"):
        calls = await _release([mine], fetch)

    assert calls == [], "no delete_vlan: the number is live in SW_L2's current component"
    released = await _allocation("RELEASED")
    assert [r.id for r in released] == [mine]
    assert released[0].defined_switch_ids == [SW_L2], "the skipped switch stays listed"
    assert any("re-allocated" in rec.message for rec in caplog.records)


async def test_delete_runs_when_the_components_split_after_the_winner_was_allocated():
    """Our allocation and the winner were both made while SW_L2 and SW_L2_B were one
    component (stored fabric F12, ours released before the winner took the number).
    A cable was then removed: SW_L2 is now in F_A, the winner's SW_L2_B in F_B. The
    stored ids still match, but the winner no longer reaches SW_L2, so the delete on
    SW_L2 runs."""
    f12, f_a, f_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mine = await _seed_allocation(RES_ID, f12, 100, [SW_L2], [SW_L2])

    # The winner allocates the same (fabric, number) after our row flips RELEASED and
    # before the supersession check, the only order the partial-unique index allows.
    real_factory = _db_session_factory()
    opened = {"n": 0}

    class _HookedCtx:
        def __init__(self):
            self._inner = real_factory()

        async def __aenter__(self):
            opened["n"] += 1
            if opened["n"] == 2:
                await _seed_allocation(OTHER_RES, f12, 100, [SW_L2_B], [SW_L2_B])
            return await self._inner.__aenter__()

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    fetch = _fabric_map({SW_L2: f_a, SW_L2_B: f_b})
    calls = await _release([mine], fetch, factory=lambda: _HookedCtx())

    assert [(c[0], c[1], c[3]) for c in calls if c[0] == "delete_vlan"] == [
        ("delete_vlan", SW_L2, 100)
    ]
    async with TestSessionLocal() as s:
        after = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.id == mine))
        ).scalar_one()
    assert after.status == "RELEASED"
    assert after.defined_switch_ids == []


async def test_supersession_lookup_failure_skips_the_delete_and_continues(caplog):
    """Fail closed: when a same-number allocation exists and cabling cannot answer the
    current fabric, supersession cannot be ruled out, so no delete runs for that
    allocation (its switch stays listed as defined) and the error is logged. The pass
    continues: the next orphaned allocation, with no same-number rival, is released
    and undefined without asking cabling."""
    from app.services.nats_consumer import TransientUpstreamError

    f_old, f_other, f_lone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = await _seed_allocation(RES_ID, f_old, 100, [SW_L2], [SW_L2])
    await _seed_allocation(OTHER_RES, f_other, 100, [SW_MID], [SW_MID])
    second = await _seed_allocation(RES_ID, f_lone, 200, [SW_L2_B], [SW_L2_B])
    fetch = AsyncMock(
        side_effect=TransientUpstreamError("cabling fabric lookup for device x: upstream 503")
    )

    with caplog.at_level(logging.ERROR, logger="app.services.nats_consumer"):
        calls = await _release([first, second], fetch)

    assert [(c[0], c[1], c[3]) for c in calls if c[0] == "delete_vlan"] == [
        ("delete_vlan", SW_L2_B, 200)
    ]
    async with TestSessionLocal() as s:
        rows = {
            r.id: r
            for r in (
                await s.execute(
                    select(VlanAssignment).where(VlanAssignment.id.in_([first, second]))
                )
            )
            .scalars()
            .all()
        }
    assert rows[first].status == "RELEASED"
    assert rows[first].defined_switch_ids == [SW_L2], "the unjudged switch stays listed"
    assert rows[second].status == "RELEASED"
    assert rows[second].defined_switch_ids == []
    skipped = [r for r in caplog.records if getattr(r, "action", None) == "vlan_delete_unjudged"]
    assert len(skipped) == 1
    assert str(first) in skipped[0].getMessage()


async def test_no_same_number_allocation_asks_cabling_nothing():
    """With no other ACTIVE allocation holding the number nothing can supersede the
    delete, so the release never consults cabling (an outage there cannot block it)."""
    mine = await _seed_allocation(RES_ID, uuid.uuid4(), 300, [SW_L2], [SW_L2])
    await _seed_allocation(OTHER_RES, uuid.uuid4(), 301, [SW_L2], [SW_L2])
    fetch = AsyncMock(side_effect=AssertionError("cabling must not be asked"))

    calls = await _release([mine], fetch)

    assert [(c[0], c[1], c[3]) for c in calls if c[0] == "delete_vlan"] == [
        ("delete_vlan", SW_L2, 300)
    ]
    fetch.assert_not_awaited()


# --- an upstream read failing inside one allocation's deletes (issue #1209) ---
#
# The invariant: a TransientUpstreamError raised while one orphaned allocation's
# delete_vlan reads its switch or template from inventory never stops the pass from
# handling the allocations after it, and never leaves defined_switch_ids claiming less
# than may be on the hardware (the switches not proven deleted stay listed) or more
# (a switch whose delete succeeded is trimmed). The failing read happens before any
# driver call, so the failing switch's definition is still there.

# The text a failing read carries. It must never reach the log line.
UPSTREAM_TEXT = "inventory answered 500: db password=hunter2 at 10.9.8.7"
UPSTREAM_FAILED_ACTION = "vlan_delete_upstream_failed"


def _inventory_failing_for(failing: set[str], read: str):
    """Patches for inventory's switch and template reads. For a switch in `failing`,
    the `read` named ("device" or "template") raises TransientUpstreamError carrying
    UPSTREAM_TEXT; every other read answers from DEVICES and TEMPLATE_DATA."""

    async def _device(device_id, client=None):
        if str(device_id) in failing:
            if read == "device":
                raise TransientUpstreamError(UPSTREAM_TEXT)
            return {**DEVICES[str(device_id)], "template_id": "tmpl-unreadable"}
        return DEVICES.get(str(device_id))

    async def _template(template_id, client=None):
        if template_id == "tmpl-unreadable":
            raise TransientUpstreamError(UPSTREAM_TEXT)
        return TEMPLATE_DATA

    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(side_effect=_template)),
    ]


async def _release_in_order(va_ids, failing, read="device", execute_fn=None, calls=None):
    """Run _release_orphaned_allocations over `va_ids` IN THE ORDER GIVEN (a list: the
    function only iterates its argument, and a set would make "the allocation after
    the failing one" depend on hashing). No allocation here has a same-number rival,
    so cabling must never be asked."""
    if execute_fn is None:
        execute_fn, calls = _recorder()
    with ExitStack() as stack:
        for p in _inventory_failing_for(failing, read):
            stack.enter_context(p)
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/d"))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        stack.enter_context(
            patch(
                "app.services.vlan_service.fetch_fabric_id",
                new=AsyncMock(side_effect=AssertionError("cabling must not be asked")),
            )
        )
        await _release_orphaned_allocations(
            list(va_ids), _db_session_factory(), _FetchContext(None)
        )
    return calls


async def _allocations_by_id(va_ids):
    async with TestSessionLocal() as s:
        rows = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.id.in_(list(va_ids)))))
            .scalars()
            .all()
        )
    return {r.id: r for r in rows}


def _upstream_failed_records(caplog):
    return [r for r in caplog.records if getattr(r, "action", None) == UPSTREAM_FAILED_ACTION]


def _assert_class_name_only(record):
    """The formatted line names the exception class and carries none of its text."""
    formatted = JSONFormatter("execution").format(record)
    payload = json.loads(formatted)
    assert payload["action"] == "vlan_delete_upstream_failed"
    assert payload["error_class"] == "TransientUpstreamError"
    assert "TransientUpstreamError" in payload["message"]
    for leaked in ("hunter2", "10.9.8.7", "inventory answered"):
        assert leaked not in formatted, f"upstream text {leaked!r} reached the log line"
    return payload


@pytest.mark.parametrize("read", ["device", "template"])
@pytest.mark.parametrize("failing_index", [0, 1, 2])
async def test_release_upstream_failure_skips_only_that_allocation(failing_index, read, caplog):
    """Three orphaned allocations, each defined on its own switch; the switch (or
    template) read of the one at `failing_index` raises. Every other allocation is
    released and its delete runs and trims its switch, before and AFTER the failing
    one; the failing allocation is released with its switch still listed (no driver
    call reached it, so the definition is still there); one line is logged with the
    fixed action and the class name only; the call does not raise."""
    switches = [SW_L2, SW_MID, SW_L2_B]
    vlans = [100, 200, 300]
    ids = [
        await _seed_allocation(RES_ID, uuid.uuid4(), vlan, [sw], [sw])
        for sw, vlan in zip(switches, vlans, strict=True)
    ]
    failing = switches[failing_index]

    with caplog.at_level(logging.ERROR, logger="app.services.nats_consumer"):
        calls = await _release_in_order(ids, {failing}, read)

    expected_deletes = [
        (sw, vlan)
        for i, (sw, vlan) in enumerate(zip(switches, vlans, strict=True))
        if i != failing_index
    ]
    assert [(c[1], c[3]) for c in calls if c[0] == "delete_vlan"] == expected_deletes
    assert failing not in {c[1] for c in calls}, "no driver session opened on the failing switch"
    rows = await _allocations_by_id(ids)
    for i, va_id in enumerate(ids):
        assert rows[va_id].status == "RELEASED"
        expected_defined = [failing] if i == failing_index else []
        assert rows[va_id].defined_switch_ids == expected_defined, (
            f"allocation {i}: defined_switch_ids must list exactly what may still be defined"
        )
    (record,) = _upstream_failed_records(caplog)
    payload = _assert_class_name_only(record)
    assert payload["vlan_assignment_id"] == str(ids[failing_index])
    assert payload["reservation_id"] == RES_ID
    assert payload["vlan_id"] == vlans[failing_index]
    assert payload["switch_ids"] == [failing]
    assert not [r for r in caplog.records if getattr(r, "action", None) == "vlan_delete_unjudged"]


@pytest.mark.parametrize("first_delete", ["succeeds", "fails_at_the_driver"])
async def test_release_upstream_failure_on_a_later_switch_keeps_what_is_not_proven_deleted(
    first_delete, caplog
):
    """A multi-switch allocation defined on [SW_L2, SW_MID, SW_L2_B] (stored order),
    then a single-switch allocation on SW_L2_C. SW_MID's read raises.

    - SW_L2 was handled before the failure: when its delete succeeded it leaves
      defined_switch_ids; when its delete failed at the driver it stays listed (the
      existing delete-failure rule).
    - SW_MID (the failing read) and SW_L2_B (never attempted: the rest of THIS
      allocation's deletes stop at the failure) stay listed.
    - The next allocation is still released and its delete runs."""
    order = [SW_L2, SW_MID, SW_L2_B]
    multi = await _seed_allocation(RES_ID, uuid.uuid4(), 100, order, order)
    after = await _seed_allocation(RES_ID, uuid.uuid4(), 200, [SW_L2_C], [SW_L2_C])
    if first_delete == "succeeds":
        execute_fn, calls = _recorder()
    else:
        execute_fn, calls = _recorder(fail={"delete_vlan"}, fail_on_switch=SW_L2)

    with caplog.at_level(logging.ERROR, logger="app.services.nats_consumer"):
        await _release_in_order([multi, after], {SW_MID}, execute_fn=execute_fn, calls=calls)

    deleted_on = [c[1] for c in calls if c[0] == "delete_vlan"]
    assert SW_MID not in deleted_on and SW_L2_B not in deleted_on
    assert deleted_on[-1] == SW_L2_C, "the next allocation's delete still runs"
    assert set(deleted_on[:-1]) == {SW_L2}
    rows = await _allocations_by_id([multi, after])
    assert rows[multi].status == "RELEASED" and rows[after].status == "RELEASED"
    if first_delete == "succeeds":
        assert rows[multi].defined_switch_ids == [SW_MID, SW_L2_B], (
            "the switch whose delete succeeded is trimmed; the rest stay listed"
        )
    else:
        assert rows[multi].defined_switch_ids == [SW_L2, SW_MID, SW_L2_B]
    assert rows[after].defined_switch_ids == []
    (record,) = _upstream_failed_records(caplog)
    payload = _assert_class_name_only(record)
    assert payload["vlan_assignment_id"] == str(multi)
    assert payload["switch_ids"] == [SW_MID, SW_L2_B], "the switches the deletes did not reach"


async def test_reconcile_release_tail_upstream_failure_still_stamps_the_version(caplog):
    """The same catch through the reconcile's L2 apply tail. Version 1 defines the
    chain's VLAN on SW_L2, SW_MID, SW_L2_B (stored sorted); version 2 drops every wire,
    so the allocation is orphaned and released, and SW_MID's read (transit-only, read
    by nothing else in this delivery) fails at its delete. The deletes stop for this
    allocation at SW_MID: the switches sorted before it are deleted and trimmed, SW_MID
    and every switch after it stay listed. The reconcile completes and stamps version
    2 (before #1209 the error escaped the apply and nacked the reconcile). The switch
    ids are random, so SW_MID's position in the stored order varies by run; the
    deterministic trim case is the multi-switch test above."""
    from app.models.reservation_wiring_state import ReservationWiringState

    await _reconcile(CHAIN_WIRES, fork_version=1)
    (alloc,) = await _allocation()
    stored = [str(s) for s in alloc.defined_switch_ids]
    assert set(stored) == {SW_L2, SW_MID, SW_L2_B}
    cut = stored.index(SW_MID)

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        for p in _patches(execute_fn, []):
            stack.enter_context(p)
        for p in _inventory_failing_for({SW_MID}, "device"):
            stack.enter_context(p)
        with caplog.at_level(logging.ERROR, logger="app.services.nats_consumer"):
            await handle_wiring_changed(
                {"reservation_id": RES_ID, "fork_version": 2}, _db_session_factory()
            )

    assert [c[1] for c in calls if c[0] == "delete_vlan"] == stored[:cut]
    (released,) = await _allocation("RELEASED")
    assert released.defined_switch_ids == stored[cut:]
    assert {r.status for r in await _membership_rows()} == {"RELEASED"}
    async with TestSessionLocal() as s:
        state = (
            await s.execute(
                select(ReservationWiringState).where(
                    ReservationWiringState.reservation_id == uuid.UUID(RES_ID)
                )
            )
        ).scalar_one()
    assert state.last_applied_fork_version == 2, "the reconcile completed and stamped"
    (record,) = _upstream_failed_records(caplog)
    payload = _assert_class_name_only(record)
    assert payload["switch_ids"] == stored[cut:]


async def test_release_does_not_catch_an_exception_other_than_transient_upstream():
    """Control: the catch is no wider than TransientUpstreamError. Any other exception
    from a switch read (here a RuntimeError) still propagates out of the pass; the
    allocation in hand is already RELEASED and keeps its switch listed, and the one
    after it is not reached and stays ACTIVE. A catch widened to Exception fails here."""
    first = await _seed_allocation(RES_ID, uuid.uuid4(), 100, [SW_L2], [SW_L2])
    second = await _seed_allocation(RES_ID, uuid.uuid4(), 200, [SW_L2_B], [SW_L2_B])

    async def _device(device_id, client=None):
        raise RuntimeError("not an upstream outage")

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn)
        )
        with pytest.raises(RuntimeError, match="not an upstream outage"):
            await _release_orphaned_allocations(
                [first, second], _db_session_factory(), _FetchContext(None)
            )

    assert calls == []
    rows = await _allocations_by_id([first, second])
    assert rows[first].status == "RELEASED"
    assert rows[first].defined_switch_ids == [SW_L2]
    assert (rows[second].status, rows[second].defined_switch_ids) == ("ACTIVE", [SW_L2_B])


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


# --- _release_orphaned_allocations: no matching ACTIVE row to release ---------


async def test_release_orphaned_allocations_no_active_row_is_a_defensive_no_op():
    """A vlan_assignment_id with zero active memberships but no matching ACTIVE row
    (already released by a racing pass, or a bogus id) is a silent no-op: no driver
    call, no exception, nothing to release."""
    va_id = uuid.uuid4()
    execute_fn, calls = _recorder()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(execute_fn, []):
            stack.enter_context(p)
        await _release_orphaned_allocations({va_id}, _db_session_factory(), _FetchContext(None))

    assert calls == [], "nothing to release: no driver call may fire"


# --- zero membership delta but the definition SCOPE still changed (issue #442) ---


async def test_scope_grows_with_no_membership_delta_defines_new_transit_switch():
    """A re-wire that inserts a new trunk-transit switch between the same two
    terminal ports produces ZERO membership delta (both terminal memberships are
    unchanged), but the definition scope grows to include the new transit switch,
    which must still get create_vlan even though add_keys/remove_keys/stale_rows are
    all empty."""
    # Version 1: direct trunk, no transit switch in the scope.
    await _reconcile(
        [
            _wire(DUT1, "eth0", SW_L2, "0/0/1"),
            _wire(SW_L2, "0/0/9", SW_L2_B, "0/0/9"),
            _wire(SW_L2_B, "0/0/1", DUT2, "eth0"),
        ],
        fork_version=1,
    )
    before = await _allocation()
    assert len(before) == 1
    assert set(before[0].defined_switch_ids) == {SW_L2, SW_L2_B}

    members_before = {
        (str(r.switch_device_id), r.port) for r in await _membership_rows() if r.status == "ACTIVE"
    }
    assert members_before == {(SW_L2, "0/0/1"), (SW_L2_B, "0/0/1")}

    # Version 2: routed through SW_MID as a transit hop, but the SAME terminal
    # ports on SW_L2 and SW_L2_B, so membership is unchanged.
    execute_fn, calls = _recorder()
    calls_result = await _reconcile(
        [
            _wire(DUT1, "eth0", SW_L2, "0/0/1"),
            _wire(SW_L2, "0/0/9", SW_MID, "0/0/1"),
            _wire(SW_MID, "0/0/2", SW_L2_B, "0/0/9"),
            _wire(SW_L2_B, "0/0/1", DUT2, "eth0"),
        ],
        fork_version=2,
        execute_fn=execute_fn,
        calls=calls,
    )

    members_after = {
        (str(r.switch_device_id), r.port) for r in await _membership_rows() if r.status == "ACTIVE"
    }
    assert members_after == members_before, "the terminal memberships must not change"

    after = await _allocation()
    assert len(after) == 1
    assert after[0].id == before[0].id, "the same allocation is reused, not recreated"
    assert set(after[0].defined_switch_ids) == {SW_L2, SW_MID, SW_L2_B}, (
        "the new transit switch must be added to the definition scope"
    )
    create_vlan_switches = {
        device_id for a, device_id, _p, _v in calls_result if a == "create_vlan"
    }
    assert SW_MID in create_vlan_switches, "the newly-scoped transit switch must be defined"
