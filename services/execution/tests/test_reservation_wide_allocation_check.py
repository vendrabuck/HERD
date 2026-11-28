"""The reservation-wide VLAN allocation check (issue #1497, ADR 0018 D11, WIRE-VLAN-4).

`_apply_l2_memberships` releases only the allocations its own removes touched, at its
last statement. A pass that raises between two switches (a switch or template read for
the second one failing after the first switch's membership was removed) never gets
there, and the redelivered pass no longer sees the removed membership, so before #1497
its allocation stayed ACTIVE with no member for good. Every full L2 reconcile (its
no-delta early return included, where the check runs before the define step), the
terminal teardown, and, on a frozen reservation, each retry pass's apply now end with
`_release_reservation_orphaned_allocations`, which looks at every ACTIVE allocation of
the reservation.

The release condition, per allocation A of reservation R
(`vlan_service.held_by_a_failed_membership` false):
  - no ACTIVE membership references A;
  - no FAILED membership of R, of either direction, names A (a build's add may have
    landed on its number; a release's removal from it is pending);
  - R is frozen, or R has no FAILED membership that is intended ACTIVE or owes the
    removal of a lost join's add (JOIN_UNDO_PENDING_REMOVAL, issue #1413);
  - no FAILED intended-ACTIVE membership of R holds a live drive claim (a retry pass
    that claimed it before the freeze may still land its join; the teardown leaves
    such a row alone, WIRE-TEARDOWN-6).

What this file pins:
  - the issue's two-switch raise-then-redeliver interleaving, for the reconcile and
    for the teardown (both fail without the check), and for a retry pass on a frozen
    reservation, through the tick and the manual channel (review finding 2);
  - the condition matrix {reconcile with delta, reconcile no-delta, teardown} x
    {frozen, not} x {FAILED join on A, on the nil allocation, on another allocation of
    R; FAILED removal from A; a row owing a lost join's removal; none} x {ACTIVE
    member, none}, with another reservation's
    rows seeded (frozen, with a FAILED join, an orphaned ACTIVE allocation) and
    asserted untouched;
  - the guard inside the release's own UPDATE (a failed join committed after the
    candidate read keeps the allocation), the held-claim hold on a frozen reservation
    and what a held build's ending releases afterwards (review finding 3), the
    no-delta order (review finding 5), that a retry pass on a live reservation does not
    run the check, and fail-closed reads;
  - the functional path: `process_reservation_message` with a real wiring_changed
    payload and the real inventory fetch helpers behind an httpx MockTransport, first
    delivery NAKed by a 503, redelivery leaving no memberless ACTIVE allocation.

Matrix seam: the FAILED join, the ACTIVE member, and the freeze are written by a
wrapper around the real check, right before it runs: rows a pass did not see when it
read the ledger (a join that failed or committed meanwhile, a terminal event that froze
the reservation mid-pass). Seeding them before the pass would let the pass's own
stale-build settlement or teardown park change them first, and the cell would test that
code instead of the check. The wrapper also counts calls, so a site that stops calling
the check fails every cell of its path.
"""

import json
import uuid
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import nats_consumer
from app.services.nats_consumer import (
    TransientUpstreamError,
    _FetchContext,
    _teardown_from_ledgers,
    handle_reservation_event,
    handle_wiring_changed,
    process_reservation_message,
)
from app.services.vlan_service import _derive_vlan_id
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
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


RES = uuid.uuid4()
RES_ID = str(RES)
OTHER = uuid.uuid4()
S1 = str(uuid.uuid4())
S2 = str(uuid.uuid4())
DUT1 = str(uuid.uuid4())
DUT2 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
FABRIC_1 = uuid.uuid4()
FABRIC_2 = uuid.uuid4()
NIL = uuid.UUID(int=0)
# A number the reconcile never derives for RES, so a seeded allocation never meets a
# same-number rival created by the pass itself.
SEEDED_VLAN = 100 if _derive_vlan_id(RES_ID) != 100 else 101
OTHER_VLAN = 300 if _derive_vlan_id(RES_ID) != 300 else 301

SUCCESS = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}


def _dev(device_id, ctype, template_id):
    return {
        "id": device_id,
        "name": "d",
        "template_id": template_id,
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": ctype,
        "field_data": {"ip": "10.0.0.1"},
    }


DEVICES = {
    S1: _dev(S1, "Layer 2 Switch", "tmpl-s1"),
    S2: _dev(S2, "Layer 2 Switch", "tmpl-s2"),
    DUT1: _dev(DUT1, "Server", "tmpl-dut"),
    DUT2: _dev(DUT2, "Server", "tmpl-dut"),
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


WIRE_S1 = _wire(DUT1, "eth0", S1, "0/0/1")
WIRE_S2 = _wire(DUT2, "eth0", S2, "0/0/2")


class _Upstreams:
    """The patched upstreams of one test: inventory reads, the driver sandbox, the
    fork's intended wires, and cabling's fabric lookup (S1 and S2 in two fabrics,
    every other switch in a fabric of its own)."""

    def __init__(self, wires=None):
        self.wires = list(wires or [])
        self.calls: list[tuple[str, str | None, int | None]] = []
        self.template_reads = 0
        self.fail_template_read: int | None = None
        self._fabrics: dict[str, uuid.UUID] = defaultdict(uuid.uuid4)
        self._fabrics[S1] = FABRIC_1
        self._fabrics[S2] = FABRIC_2

    def _execute(self, driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        self.calls.append((action, mk.get("port"), mk.get("vlan_id")))
        return SUCCESS

    async def _device(self, device_id, client=None):
        return DEVICES.get(str(device_id))

    async def _template(self, template_id, client=None):
        self.template_reads += 1
        if self.template_reads == self.fail_template_read:
            raise TransientUpstreamError("inventory answered 503")
        return {"id": template_id, "name": "T", "sections": []}

    async def _fork(self, reservation_id, client=None):
        return list(self.wires)

    async def _fabric(self, device_id):
        return self._fabrics[str(device_id)]

    def enter(self, stack: ExitStack, *, inventory=True):
        if inventory:
            stack.enter_context(
                patch(
                    "app.services.nats_consumer._fetch_device",
                    new=AsyncMock(side_effect=self._device),
                )
            )
            stack.enter_context(
                patch(
                    "app.services.nats_consumer._fetch_template",
                    new=AsyncMock(side_effect=self._template),
                )
            )
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/d"))
        )
        stack.enter_context(
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=self._execute)
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_fork_intended_wires",
                new=AsyncMock(side_effect=self._fork),
            )
        )
        stack.enter_context(
            patch(
                "app.services.vlan_service.fetch_fabric_id",
                new=AsyncMock(side_effect=self._fabric),
            )
        )

    def actions(self, action: str) -> list:
        return [(port, vlan) for a, port, vlan in self.calls if a == action]


async def _wiring_changed(up: _Upstreams, fork_version: int) -> None:
    with ExitStack() as stack:
        up.enter(stack)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": fork_version}, _db_session_factory()
        )


async def _teardown(up: _Upstreams) -> None:
    with ExitStack() as stack:
        up.enter(stack)
        await _teardown_from_ledgers(RES_ID, _FetchContext(None), _db_session_factory())


async def _freeze(reservation=RES) -> None:
    from app.services.l1_assignment_service import freeze_reservation_wiring

    async with TestSessionLocal() as s:
        await freeze_reservation_wiring(s, reservation)


async def _add_allocation(reservation, vlan, fabric, switches, defined=()) -> uuid.UUID:
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=reservation,
            fabric_id=fabric,
            vlan_id=vlan,
            switch_device_ids=list(switches),
            defined_switch_ids=list(defined),
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        return va.id


async def _add_membership(
    reservation,
    allocation,
    switch,
    port,
    *,
    status="ACTIVE",
    intended="ACTIVE",
    claimed_until=None,
    last_error=None,
) -> uuid.UUID:
    async with TestSessionLocal() as s:
        row = L2PortAssignment(
            reservation_id=reservation,
            vlan_assignment_id=allocation,
            switch_device_id=uuid.UUID(str(switch)),
            port=port,
            status=status,
            intended=intended,
            attempts=2 if status == "FAILED" else 0,
            last_error=last_error
            if last_error is not None
            else ("driver timeout" if status == "FAILED" else None),
            claimed_until=claimed_until,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _allocations(reservation=RES) -> dict[uuid.UUID, VlanAssignment]:
    async with TestSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(VlanAssignment).where(VlanAssignment.reservation_id == reservation)
                )
            )
            .scalars()
            .all()
        )
    return {r.id: r for r in rows}


async def _memberless_active(reservation=RES) -> set[uuid.UUID]:
    """ACTIVE allocations of `reservation` that no ACTIVE membership references."""
    async with TestSessionLocal() as s:
        allocs = (
            (
                await s.execute(
                    select(VlanAssignment.id).where(
                        VlanAssignment.reservation_id == reservation,
                        VlanAssignment.status == "ACTIVE",
                    )
                )
            )
            .scalars()
            .all()
        )
        members = set(
            (
                await s.execute(
                    select(L2PortAssignment.vlan_assignment_id).where(
                        L2PortAssignment.status == "ACTIVE"
                    )
                )
            )
            .scalars()
            .all()
        )
    return set(allocs) - members


async def _snapshot(reservation) -> tuple:
    """Every column of every allocation and membership row of `reservation`."""
    async with TestSessionLocal() as s:
        out = []
        for model in (VlanAssignment, L2PortAssignment):
            rows = (
                (
                    await s.execute(
                        select(model).where(model.reservation_id == reservation).order_by(model.id)
                    )
                )
                .scalars()
                .all()
            )
            out.append(
                [tuple(getattr(r, c.key) for c in model.__mapper__.column_attrs) for r in rows]
            )
    return tuple(map(tuple, out))


async def _seed_foreign() -> tuple:
    """Another reservation, frozen, with a FAILED intended-ACTIVE join, the orphaned
    ACTIVE allocation that join names (no member), an ACTIVE allocation with a member,
    and an orphaned ACTIVE allocation nothing holds (one the check would release for
    OTHER). If the check read the freeze or the failed rows of the wrong reservation, or
    looked beyond RES's own allocations, one of the matrix cells or this snapshot would
    change."""
    other_switch = str(uuid.uuid4())
    orphan = await _add_allocation(OTHER, OTHER_VLAN, uuid.uuid4(), [other_switch])
    held = await _add_allocation(OTHER, OTHER_VLAN + 1, uuid.uuid4(), [other_switch])
    await _add_allocation(OTHER, OTHER_VLAN + 2, uuid.uuid4(), [other_switch])
    await _add_membership(OTHER, held, other_switch, "0/0/1")
    await _add_membership(OTHER, orphan, other_switch, "0/0/2", status="FAILED")
    await _freeze(OTHER)
    return await _snapshot(OTHER)


# --- the issue's interleaving: a pass that raises between two switches ---


async def _two_switch_reservation(up: _Upstreams) -> dict[str, uuid.UUID]:
    """R joined S1 (fabric 1) and S2 (fabric 2) through a first reconcile: two
    allocations, each with its one ACTIVE member and defined on its switch."""
    up.wires = [WIRE_S1, WIRE_S2]
    await _wiring_changed(up, 1)
    by_switch = {}
    async with TestSessionLocal() as s:
        rows = await s.execute(
            select(L2PortAssignment).where(L2PortAssignment.reservation_id == RES)
        )
        for row in rows.scalars().all():
            by_switch[str(row.switch_device_id)] = row.vlan_assignment_id
    assert set(by_switch) == {S1, S2}
    assert by_switch[S1] != by_switch[S2]
    up.calls.clear()
    up.template_reads = 0
    return by_switch


async def test_a_reconcile_that_raised_between_switches_leaks_no_allocation_on_redelivery():
    """Issue #1497. A save removes both wires; the first switch's membership is removed,
    then the template read for the second raises. The pass raises (the message would be
    NAKed, the version is not stamped) and the first allocation is ACTIVE with no member.
    The redelivered pass sees only the second membership; the reservation-wide check at
    its end releases the first allocation too and deletes its VLAN definition."""
    other = await _seed_foreign()
    up = _Upstreams()
    alloc = await _two_switch_reservation(up)

    up.wires = []
    up.fail_template_read = 2
    with pytest.raises(TransientUpstreamError):
        await _wiring_changed(up, 2)
    leaked = await _memberless_active()
    assert len(leaked) == 1, "the precondition: one allocation was left with no member"
    (leaked_id,) = leaked
    assert len(up.actions("remove_from_vlan")) == 1

    up.fail_template_read = None
    up.calls.clear()
    await _wiring_changed(up, 2)

    rows = await _allocations()
    assert {rows[a].status for a in alloc.values()} == {"RELEASED"}
    assert await _memberless_active() == set()
    leaked_vlan = rows[leaked_id].vlan_id
    assert any(vlan == leaked_vlan for _port, vlan in up.actions("delete_vlan")), (
        "the leaked allocation's VLAN definition is deleted from its switch"
    )
    assert rows[leaked_id].defined_switch_ids == []
    assert await _snapshot(OTHER) == other


async def test_a_teardown_that_raised_between_switches_leaks_no_allocation_on_redelivery():
    """The same raise in the terminal teardown: the redelivered teardown builds its
    removes from the ACTIVE memberships (only the second switch's), and its
    reservation-wide check releases the first allocation as well."""
    other = await _seed_foreign()
    up = _Upstreams()
    alloc = await _two_switch_reservation(up)
    await _freeze()

    up.fail_template_read = 2
    with pytest.raises(TransientUpstreamError):
        await _teardown(up)
    assert len(await _memberless_active()) == 1

    up.fail_template_read = None
    await _teardown(up)

    rows = await _allocations()
    assert {rows[a].status for a in alloc.values()} == {"RELEASED"}
    assert await _memberless_active() == set()
    assert all(r.defined_switch_ids == [] for r in rows.values())
    assert await _snapshot(OTHER) == other


async def test_a_teardown_with_no_l2_rows_left_still_releases_the_leaked_allocation():
    """The redelivered teardown can find no ACTIVE or FAILED L2 row at all: the leaked
    allocation's only membership is already RELEASED (an earlier delivery removed it
    and raised before its release). Before #1497 the teardown then skipped its L2 step
    entirely; the check runs anyway and releases the allocation and its definition."""
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    await _add_membership(RES, alloc, S1, "0/0/1", status="RELEASED", intended="RELEASED")
    await _freeze()
    up = _Upstreams()
    await _teardown(up)
    assert (await _allocations())[alloc].status == "RELEASED"
    assert up.actions("remove_from_vlan") == []
    assert up.actions("delete_vlan") == [(None, SEEDED_VLAN)]


# --- the condition matrix ---

PATHS = ("reconcile_delta", "reconcile_no_delta", "teardown")


@pytest.mark.parametrize("member", [True, False], ids=["member", "no_member"])
@pytest.mark.parametrize(
    "failed", ["real", "nil", "other_real", "release_row", "owed_row", "absent"]
)
@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "not_frozen"])
@pytest.mark.parametrize("path", PATHS)
async def test_the_check_releases_exactly_when_the_condition_holds(
    path, frozen, failed, member, caplog
):
    """At each site, allocation A of R (ACTIVE, defined on S1, VLAN SEEDED_VLAN) is
    released by the check iff no ACTIVE membership references it and
    held_by_a_failed_membership is false: no FAILED row of R names A (`real`, a FAILED
    join on A; `release_row`, a FAILED removal from A), and either R is frozen or R has
    no FAILED membership that is intended ACTIVE or owes a lost join's removal (`nil`,
    a FAILED join on the nil allocation; `other_real`, one on another allocation of R;
    `owed_row`, a row of R owing the removal of a lost join's add from a RELEASED
    allocation, JOIN_UNDO_PENDING_REMOVAL; each holds A only while R is not frozen).
    The FAILED row, A's member, and the freeze are written just before the check runs
    (module docstring). Another reservation's rows never change, and the
    per-pass check of the site's own work still runs (the delta's new allocation keeps
    its member; the teardown's removed allocation is released). The candidate read
    already applies the condition, so no release loses a race here (no
    `vlan_release_lost_race` line)."""
    import logging

    other = await _seed_foreign()
    target = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    up = _Upstreams()
    worked = None
    if path == "reconcile_delta":
        up.wires = [WIRE_S2]
    elif path == "teardown":
        worked = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2])
        await _add_membership(RES, worked, S2, "0/0/3")

    real = nats_consumer._release_reservation_orphaned_allocations
    seen: list[str] = []

    async def _seed_then_check(reservation_id, ctx, get_db_session):
        seen.append(reservation_id)
        if member:
            await _add_membership(RES, target, S1, "0/0/1")
        if failed in ("real", "nil"):
            await _add_membership(
                RES, target if failed == "real" else NIL, S1, "0/0/7", status="FAILED"
            )
        elif failed == "other_real":
            other_alloc = await _add_allocation(RES, SEEDED_VLAN + 5, uuid.uuid4(), [S1])
            await _add_membership(RES, other_alloc, S1, "0/0/6")
            await _add_membership(RES, other_alloc, S1, "0/0/7", status="FAILED")
        elif failed == "release_row":
            await _add_membership(RES, target, S1, "0/0/7", status="FAILED", intended="RELEASED")
        elif failed == "owed_row":
            from app.services.l2_membership_service import JOIN_UNDO_PENDING_REMOVAL

            released = await _add_allocation(RES, SEEDED_VLAN + 6, uuid.uuid4(), [S1])
            async with TestSessionLocal() as s:
                (await s.get(VlanAssignment, released)).status = "RELEASED"
                await s.commit()
            await _add_membership(
                RES,
                released,
                S1,
                "0/0/7",
                status="FAILED",
                intended="RELEASED",
                last_error=JOIN_UNDO_PENDING_REMOVAL,
            )
        if frozen:
            await _freeze()
        await real(reservation_id, ctx, get_db_session)

    with (
        patch(
            "app.services.nats_consumer._release_reservation_orphaned_allocations",
            new=_seed_then_check,
        ),
        caplog.at_level(logging.INFO, logger="app.services.nats_consumer"),
    ):
        if path == "teardown":
            await _teardown(up)
        else:
            await _wiring_changed(up, 1)

    assert seen == [RES_ID], "the site runs the reservation-wide check once"
    expect_released = not member and (
        failed == "absent" or (frozen and failed in ("nil", "other_real", "owed_row"))
    )
    rows = await _allocations()
    assert rows[target].status == ("RELEASED" if expect_released else "ACTIVE")
    assert ((None, SEEDED_VLAN) in up.actions("delete_vlan")) is expect_released
    assert rows[target].defined_switch_ids == ([] if expect_released else [S1])
    if path == "reconcile_delta":
        seeded = (SEEDED_VLAN + 5, SEEDED_VLAN + 6)
        (new_id,) = {a for a in rows if a != target and rows[a].vlan_id not in seeded}
        assert rows[new_id].status == "ACTIVE", "the delta's own join keeps its allocation"
    if path == "teardown":
        assert rows[worked].status == "RELEASED", "the per-pass check still runs"
    assert [
        r for r in caplog.records if getattr(r, "action", None) == "vlan_release_lost_race"
    ] == []
    assert await _snapshot(OTHER) == other


# --- the guard inside the release's own UPDATE, held claims, fail-closed reads ---


async def test_a_failed_join_committed_after_the_candidate_read_keeps_the_allocation(caplog):
    """The candidate read finds A orphaned and R with no failed join. A join of R then
    fails and commits before the release's UPDATE: the guard in that UPDATE sees it,
    the release loses like any lost race (one INFO line, no delete_vlan) and A stays
    ACTIVE with its definition."""
    import logging

    from app.services import vlan_service

    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    real = vlan_service.release_allocation_if_orphaned
    outcomes = []

    async def _join_fails_first(db, allocation_id, **kwargs):
        await _add_membership(RES, NIL, S2, "0/0/9", status="FAILED")
        won = await real(db, allocation_id, **kwargs)
        outcomes.append((won, kwargs))
        return won

    up = _Upstreams()
    with (
        ExitStack() as stack,
        caplog.at_level(logging.INFO, logger="app.services.nats_consumer"),
    ):
        up.enter(stack)
        stack.enter_context(
            patch(
                "app.services.vlan_service.release_allocation_if_orphaned",
                new=_join_fails_first,
            )
        )
        await nats_consumer._release_reservation_orphaned_allocations(
            RES_ID, _FetchContext(None), _db_session_factory()
        )

    assert outcomes == [(False, {"reservation_wide": True})]
    row = (await _allocations())[alloc]
    assert (row.status, row.defined_switch_ids) == ("ACTIVE", [S1])
    assert up.calls == []
    assert [r for r in caplog.records if getattr(r, "action", None) == "vlan_release_lost_race"]


async def test_the_per_pass_release_ignores_failed_joins_as_before():
    """The per-pass check of WIRE-VLAN-4 keeps its membership test alone: called
    without reservation_wide, the compare-and-swap releases an orphan even while R has
    a FAILED join (the narrowing is the reservation-wide check's only)."""
    from app.services.vlan_service import release_allocation_if_orphaned

    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    await _add_membership(RES, alloc, S1, "0/0/7", status="FAILED")
    async with TestSessionLocal() as s:
        assert await release_allocation_if_orphaned(s, alloc) is True


@pytest.mark.parametrize("reservation_wide", [True, False])
async def test_the_guarded_release_never_touches_an_allocation_with_an_active_member(
    reservation_wide,
):
    from app.services.vlan_service import release_allocation_if_orphaned

    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    await _add_membership(RES, alloc, S1, "0/0/1")
    await _freeze()
    async with TestSessionLocal() as s:
        assert (
            await release_allocation_if_orphaned(s, alloc, reservation_wide=reservation_wide)
            is False
        )
    assert (await _allocations())[alloc].status == "ACTIVE"


@pytest.mark.parametrize("allocation", ["real", "nil"])
async def test_a_live_claim_on_a_failed_join_keeps_allocations_of_a_frozen_reservation(
    allocation,
):
    """A retry pass claimed a build row before the freeze and is inside its drive: its
    add_to_vlan may still land. The teardown leaves the row alone (WIRE-TEARDOWN-6) and
    its check keeps every allocation of the reservation, with no delete_vlan."""
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    claim = datetime.now(timezone.utc) + timedelta(minutes=5)
    await _add_membership(
        RES,
        alloc if allocation == "real" else NIL,
        S1,
        "0/0/7",
        status="FAILED",
        claimed_until=claim,
    )
    await _freeze()
    up = _Upstreams()
    await _teardown(up)
    assert (await _allocations())[alloc].status == "ACTIVE"
    assert up.calls == []


async def test_an_expired_claim_holds_nothing_on_a_frozen_reservation():
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    await _add_membership(RES, NIL, S1, "0/0/7", status="FAILED", claimed_until=expired)
    await _freeze()

    up = _Upstreams()
    with ExitStack() as stack:
        up.enter(stack)
        await nats_consumer._release_reservation_orphaned_allocations(
            RES_ID, _FetchContext(None), _db_session_factory()
        )
    assert (await _allocations())[alloc].status == "RELEASED"
    assert up.actions("delete_vlan") == [(None, SEEDED_VLAN)]


async def test_a_claimed_release_row_holds_nothing_on_a_frozen_reservation():
    """Clause 2 counts a live claim on a FAILED intended-ACTIVE row only. A release
    retry holding its claim on a FAILED intended-RELEASED row of a frozen reservation
    (here on an allocation already RELEASED) does not keep R's other memberless
    allocation."""
    from app.services.vlan_service import reservation_orphaned_allocations

    orphan = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    released = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2])
    async with TestSessionLocal() as s:
        (await s.get(VlanAssignment, released)).status = "RELEASED"
        await s.commit()
    claim = datetime.now(timezone.utc) + timedelta(minutes=5)
    await _add_membership(
        RES, released, S2, "0/0/7", status="FAILED", intended="RELEASED", claimed_until=claim
    )
    await _freeze()
    async with TestSessionLocal() as s:
        assert await reservation_orphaned_allocations(s, RES) == [orphan]


async def test_a_live_claim_on_a_reservation_that_is_not_frozen_keeps_its_allocations():
    """Not frozen, any FAILED join keeps the allocations, claimed or not."""
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    claim = datetime.now(timezone.utc) + timedelta(minutes=5)
    await _add_membership(RES, NIL, S1, "0/0/7", status="FAILED", claimed_until=claim)
    async with TestSessionLocal() as s:
        from app.services.vlan_service import reservation_orphaned_allocations

        assert await reservation_orphaned_allocations(s, RES) == []
    assert (await _allocations())[alloc].status == "ACTIVE"


@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "not_frozen"])
async def test_a_failed_release_row_holds_its_own_allocation_and_no_other(frozen):
    """A FAILED intended-RELEASED row (a removal to retry) is not a pending join, so it
    holds no other allocation of R; its own allocation is kept until its removal
    succeeds (clause 3 of held_by_a_failed_membership), frozen or not."""
    from app.services.vlan_service import reservation_orphaned_allocations

    named = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    other = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2])
    await _add_membership(RES, named, S1, "0/0/7", status="FAILED", intended="RELEASED")
    if frozen:
        await _freeze()
    async with TestSessionLocal() as s:
        assert await reservation_orphaned_allocations(s, RES) == [other]


async def test_a_released_row_naming_the_allocation_holds_nothing():
    """Only a FAILED row holds its allocation; a RELEASED row (history) does not."""
    from app.services.vlan_service import reservation_orphaned_allocations

    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    await _add_membership(RES, alloc, S1, "0/0/7", status="RELEASED", intended="RELEASED")
    async with TestSessionLocal() as s:
        assert await reservation_orphaned_allocations(s, RES) == [alloc]


async def test_the_candidates_are_the_reservations_own_orphans_oldest_first():
    from app.services.vlan_service import reservation_orphaned_allocations

    await _seed_foreign()
    # Inserted newest first, so the insertion order (what SQLite returns with no ORDER
    # BY) is the reverse of the age order; two share a stamp, ordered by id.
    newest = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    tied_a = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2])
    tied_b = await _add_allocation(RES, SEEDED_VLAN + 3, uuid.uuid4(), [S1])
    oldest = await _add_allocation(RES, SEEDED_VLAN + 4, uuid.uuid4(), [S2])
    held = await _add_allocation(RES, SEEDED_VLAN + 2, uuid.uuid4(), [S2])
    await _add_membership(RES, held, S2, "0/0/1")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with TestSessionLocal() as s:
        for alloc, age in ((newest, 3), (tied_a, 2), (tied_b, 2), (oldest, 1)):
            await s.execute(
                update(VlanAssignment)
                .where(VlanAssignment.id == alloc)
                .values(created_at=base + timedelta(hours=age))
            )
        await s.commit()
    async with TestSessionLocal() as s:
        got = await reservation_orphaned_allocations(s, RES)
    assert got == [oldest, *sorted([tied_a, tied_b]), newest]


async def test_a_failed_candidate_read_releases_nothing():
    """Fail closed: when the read that decides the release fails, the pass raises before
    any release (the caller's message is NAKed and redelivered)."""
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    up = _Upstreams()
    with (
        ExitStack() as stack,
        patch(
            "app.services.vlan_service.reservation_orphaned_allocations",
            new=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("down"))),
        ),
    ):
        up.enter(stack)
        with pytest.raises(OperationalError):
            await _teardown(up)
    assert (await _allocations())[alloc].status == "ACTIVE"
    assert up.calls == []


async def test_a_stale_failed_join_settled_in_the_same_pass_no_longer_holds_the_allocation():
    """A FAILED nil-allocation join whose port left the intended set is released by the
    reconcile's stale-build settlement (WIRE-STALE-2); the check at the end of the same
    pass then finds no pending join and releases the memberless allocation."""
    alloc = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1])
    await _add_membership(RES, NIL, S1, "0/0/7", status="FAILED")
    up = _Upstreams()
    await _wiring_changed(up, 1)
    assert (await _allocations())[alloc].status == "RELEASED"


async def test_a_failed_join_in_the_same_pass_keeps_the_leaked_allocation_until_it_is_built():
    """The leak heals on the first pass with no pending join: while a join of R fails,
    the memberless allocation is kept (the coarse rule); the pass that builds the join
    releases it."""
    up = _Upstreams()
    alloc = await _two_switch_reservation(up)
    up.wires = []
    up.fail_template_read = 2
    with pytest.raises(TransientUpstreamError):
        await _wiring_changed(up, 2)
    (leaked,) = await _memberless_active()
    gone_switch = S1 if leaked == alloc[S1] else S2
    kept_switch = S2 if gone_switch == S1 else S1

    # The redelivery now also asks for a join on the switch that kept its member, on a
    # new port, and that join fails.
    new_port = "0/0/8"
    dut = DUT1 if kept_switch == S1 else DUT2
    old_wire = WIRE_S1 if kept_switch == S1 else WIRE_S2
    up.wires = [old_wire, _wire(dut, "eth5", kept_switch, new_port)]
    up.fail_template_read = None
    real_execute = up._execute

    def _join_fails(driver_path, action, context, **kwargs):
        if action == "add_to_vlan":
            up.calls.append((action, (kwargs.get("method_kwargs") or {}).get("port"), None))
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return real_execute(driver_path, action, context, **kwargs)

    up._execute = _join_fails
    await _wiring_changed(up, 2)
    assert (await _allocations())[leaked].status == "ACTIVE", "kept while a join is failed"

    up._execute = real_execute
    await _wiring_changed(up, 3)
    assert (await _allocations())[leaked].status == "RELEASED"
    assert await _memberless_active() == set()


# --- the no-delta order (review finding 5) ---


async def test_the_no_delta_check_runs_before_the_define_step():
    """A no-delta reconcile with a memberless allocation A of R that has a switch in
    scope not yet defined: the check runs first, so A is released with no create_vlan
    and no delete_vlan. Run after the define step, A would be defined on S1 and its
    definition deleted again in the same pass."""
    live = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_1, [S1], defined=[S1])
    await _add_membership(RES, live, S1, "0/0/1")
    leaked = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [], defined=[])
    up = _Upstreams(wires=[WIRE_S1])
    await _wiring_changed(up, 1)
    rows = await _allocations()
    assert (rows[leaked].status, rows[leaked].defined_switch_ids) == ("RELEASED", [])
    assert rows[live].status == "ACTIVE"
    assert up.actions("create_vlan") == []
    assert up.actions("delete_vlan") == []


# --- the retry channels on a frozen reservation (review findings 2 and 3) ---


async def _tick(up: _Upstreams) -> dict:
    from app.services.wiring_retry_service import run_wiring_retry_tick

    with ExitStack() as stack:
        up.enter(stack)
        return await run_wiring_retry_tick(_db_session_factory())


async def _manual(up: _Upstreams) -> dict:
    from app.services.wiring_retry_service import reattempt_reservation

    with ExitStack() as stack:
        up.enter(stack)
        return await reattempt_reservation(RES, _db_session_factory())


async def _two_pending_removals() -> dict[str, uuid.UUID]:
    """Frozen R with one pending removal per switch, each on its own allocation (A1 on
    S1, A2 on S2, each defined on its switch): what a teardown whose removals failed
    leaves for the retry channels."""
    out = {}
    for switch, fabric, vlan, port in (
        (S1, FABRIC_1, SEEDED_VLAN, "0/0/1"),
        (S2, FABRIC_2, SEEDED_VLAN + 1, "0/0/2"),
    ):
        out[switch] = await _add_allocation(RES, vlan, fabric, [switch], defined=[switch])
        await _add_membership(RES, out[switch], switch, port, status="FAILED", intended="RELEASED")
    await _freeze()
    return out


@pytest.mark.parametrize("channel", ["tick", "manual"])
async def test_a_retry_pass_that_raised_between_switches_on_a_frozen_reservation_leaks_nothing(
    channel,
):
    """Review finding 2, the retry path issue #1497 names. On a frozen reservation a
    retry pass removes the port on the first switch, then the template read for the
    second switch raises: the per-pass check never runs and no reconcile or teardown
    follows. The reservation-wide check after the apply releases the first allocation
    (its VLAN deleted); the second stays ACTIVE while its pending removal names it, and
    the next pass removes the port and releases it. The tick records the reservation's
    rows as skipped; the manual channel raises the error after the check."""
    other = await _seed_foreign()
    alloc = await _two_pending_removals()
    up = _Upstreams()
    up.fail_template_read = 2
    if channel == "tick":
        stats = await _tick(up)
        assert stats["skipped_upstream"] == 1
    else:
        with pytest.raises(TransientUpstreamError):
            await _manual(up)
    assert len(up.actions("remove_from_vlan")) == 1
    rows = await _allocations()
    first = [s for s in (S1, S2) if rows[alloc[s]].status == "RELEASED"]
    assert len(first) == 1, "the allocation whose port was removed is released"
    (gone,) = first
    kept = S2 if gone == S1 else S1
    assert (None, rows[alloc[gone]].vlan_id) in up.actions("delete_vlan")
    assert rows[alloc[kept]].status == "ACTIVE", "held while its pending removal names it"

    up.fail_template_read = None
    up.calls.clear()
    if channel == "tick":
        await _tick(up)
    else:
        await _manual(up)
    rows = await _allocations()
    assert {r.status for r in rows.values()} == {"RELEASED"}
    assert await _memberless_active() == set()
    assert await _snapshot(OTHER) == other


async def test_a_retry_pass_on_a_live_reservation_does_not_run_the_check():
    """On a reservation that is not frozen the retry channels run only the per-pass
    check: a memberless allocation no removal of the pass touched waits for the next
    full reconcile or the teardown."""
    leaked = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    removed = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2], defined=[S2])
    await _add_membership(RES, removed, S2, "0/0/2", status="FAILED", intended="RELEASED")
    up = _Upstreams()
    await _tick(up)
    rows = await _allocations()
    assert rows[removed].status == "RELEASED", "the per-pass check of the removal"
    assert rows[leaked].status == "ACTIVE"


async def _held_build(claims) -> uuid.UUID:
    """A FAILED intended-ACTIVE join H of R on (S1, 0/0/1), on allocation Ah (defined on
    S1), claimed by `claims` (a retry pass) before R freezes."""
    from app.services.wiring_claim import ROW_DRIVER_ACTIONS, claim_budget

    ah = await _add_allocation(RES, SEEDED_VLAN, FABRIC_1, [S1], defined=[S1])
    row_id = await _add_membership(RES, ah, S1, "0/0/1", status="FAILED")
    async with TestSessionLocal() as s:
        assert await claims.claim(
            s,
            L2PortAssignment,
            row_id,
            intended="ACTIVE",
            budget=claim_budget(ROW_DRIVER_ACTIONS + 1),
        )
    return row_id


@pytest.mark.parametrize("ending", ["join_fails", "join_lands"])
async def test_a_held_build_ending_after_the_teardown_releases_the_other_allocations(ending):
    """Review finding 3. A retry pass holds a live claim on build row H when R ends:
    the teardown leaves H alone and keeps every memberless allocation of R (O here).
    The holder's own pass then ends on the frozen reservation and runs the check: O is
    released. H's allocation Ah is kept while H names it: a failed join stays FAILED
    intended ACTIVE (its add may have landed, and no pass drives it on a frozen
    reservation), and a landed join is parked for removal, after which the next pass
    removes the port and releases Ah."""
    from app.services.wiring_claim import WiringRowClaims
    from app.services.wiring_retry_service import _reattempt_l2_rows

    claims = WiringRowClaims()
    row_id = await _held_build(claims)
    leaked = await _add_allocation(RES, SEEDED_VLAN + 1, FABRIC_2, [S2], defined=[S2])
    await _freeze()
    up = _Upstreams(wires=[WIRE_S1])
    await _teardown(up)
    rows = await _allocations()
    assert {r.status for r in rows.values()} == {"ACTIVE"}, "kept under the live claim"
    assert up.calls == []

    if ending == "join_fails":
        real_execute = up._execute

        def _add_fails(driver_path, action, context, **kwargs):
            if action == "add_to_vlan":
                return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
            return real_execute(driver_path, action, context, **kwargs)

        up._execute = _add_fails
    async with TestSessionLocal() as s:
        h_row = (
            await s.execute(select(L2PortAssignment).where(L2PortAssignment.id == row_id))
        ).scalar_one()
    with ExitStack() as stack:
        up.enter(stack)
        await _reattempt_l2_rows([h_row], _db_session_factory(), claims, set())

    rows = await _allocations()
    assert rows[leaked].status == "RELEASED"
    assert (None, SEEDED_VLAN + 1) in up.actions("delete_vlan")
    (ah,) = {a for a in rows if a != leaked}
    assert rows[ah].status == "ACTIVE"
    async with TestSessionLocal() as s:
        h = (
            await s.execute(select(L2PortAssignment).where(L2PortAssignment.id == row_id))
        ).scalar_one()
    if ending == "join_fails":
        assert (h.status, h.intended, h.vlan_assignment_id) == ("FAILED", "ACTIVE", ah)
        return
    assert (h.status, h.intended, h.vlan_assignment_id) == ("FAILED", "RELEASED", ah)
    up._execute = _Upstreams._execute.__get__(up)
    await _tick(up)
    assert (await _allocations())[ah].status == "RELEASED"
    assert await _memberless_active() == set()


# --- functional: process_reservation_message with a real wiring_changed payload ---


def _inventory_transport(fail_template_once: list):
    """Reservations corroborates the event (ACTIVE); inventory answers the device and
    template reads; the second template read of the first delivery answers 503."""
    from app.config import settings

    reservations_path = httpx.URL(f"{settings.reservations_service_url}/internal/{RES_ID}").path
    inventory_host = httpx.URL(settings.inventory_service_url).host
    state = {"template_reads": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == reservations_path:
            return httpx.Response(200, json={"id": RES_ID, "status": "ACTIVE"})
        if request.url.host == inventory_host:
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "devices" and parts[2] == "internal":
                if parts[1] in DEVICES:
                    return httpx.Response(200, json=DEVICES[parts[1]])
            if len(parts) == 3 and parts[0] == "templates" and parts[2] == "internal":
                state["template_reads"] += 1
                if fail_template_once and state["template_reads"] == fail_template_once[0]:
                    fail_template_once.clear()
                    return httpx.Response(503, json={"detail": "unavailable"})
                return httpx.Response(200, json={"id": parts[1], "name": "T", "sections": []})
        return httpx.Response(404, json={"detail": "unexpected request"})

    return httpx.MockTransport(handler), state


async def _deliver(up: _Upstreams, transport, fork_version: int, num_delivered: int):
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        return real_client(transport=transport)

    msg = MagicMock()
    msg.data = json.dumps(
        {
            "event": "reservation.wiring_changed",
            "event_id": str(uuid.uuid4()),
            "reservation_id": RES_ID,
            "fork_version": fork_version,
        }
    ).encode()
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    js = MagicMock()
    js.publish = AsyncMock()
    with ExitStack() as stack:
        up.enter(stack, inventory=False)
        stack.enter_context(patch("app.services.nats_consumer.httpx.AsyncClient", new=_client))
        stack.enter_context(
            patch("app.services.nats_consumer.apply_reservation_event_tiers", new=AsyncMock())
        )
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )
    js.publish.assert_not_awaited()
    return result, msg


async def test_a_redelivered_wiring_changed_leaves_no_memberless_allocation():
    """Functional (issue #1497): the real consumer entry point, the real inventory
    fetch helpers over HTTP. A save that removes both switches' wires is NAKed when the
    second switch's template read answers 503, leaving one allocation with no member;
    the redelivery acks and no ACTIVE allocation of the reservation is left without a
    member."""
    from app.services.l1_assignment_service import get_wiring_state

    other = await _seed_foreign()
    up = _Upstreams()
    transport, _state = _inventory_transport([])
    up.wires = [WIRE_S1, WIRE_S2]
    result, msg = await _deliver(up, transport, 1, 1)
    assert result == "ack"
    assert len(await _allocations()) == 2

    up.wires = []
    transport, state = _inventory_transport([2])
    result, msg = await _deliver(up, transport, 2, 1)
    assert result == "nak"
    msg.nak.assert_awaited_once()
    assert len(await _memberless_active()) == 1
    async with TestSessionLocal() as s:
        assert (await get_wiring_state(s, RES_ID)).last_applied_fork_version == 1

    result, msg = await _deliver(up, transport, 2, 2)
    assert result == "ack"
    msg.ack.assert_awaited_once()
    assert await _memberless_active() == set()
    assert {r.status for r in (await _allocations()).values()} == {"RELEASED"}
    async with TestSessionLocal() as s:
        assert (await get_wiring_state(s, RES_ID)).last_applied_fork_version == 2
    assert await _snapshot(OTHER) == other
