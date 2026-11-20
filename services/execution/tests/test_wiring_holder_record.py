"""How a retry holder records a driver action that changed the device (issue #1479).

The invariant: a driver action that succeeded on the device is never lost from the
ledger. After a holder's connect, VLAN join, or route configure, or their removals,
the ledger records the hardware truth in a form some existing channel converges, with
one exception: another retry pass holds a LIVE claim on the row, and that pass re-drives
the row and records its own result. A claim the reconcile or a teardown cleared, or one
that expired, never makes a successful action go unrecorded.

Each test runs a layer's real per-row apply (`_apply_one_port_action`,
`_apply_one_vlan_action`) or per-switch apply (`_apply_l3_adjacency`) as the holder of
one row. The bounded driver call (`_run_driver_with_retry`) is patched so that, at the
row's own action, another writer changes the row first, on the event loop, exactly as
it would land mid-drive, and then the action answers success or failure. The other
writers are the real functions the reconcile, a teardown, and another retry pass call.
The assertions are on the resulting rows (the ledger after the record) and on whether
the pass would report the row `in_progress` (its claim ledger's `lost`).

The outcome table these tests pin is in docs/specs/provisioning-and-wiring.md
(WIRE-CLAIM-10).
"""

import uuid
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import l1_assignment_service as l1svc
from app.services import l2_membership_service as l2svc
from app.services import route_service as l3svc
from app.services.nats_consumer import (
    WIRING_STALE_BUILD_REASON,
    _apply_l3_adjacency,
    _apply_one_port_action,
    _apply_one_vlan_action,
    _FetchContext,
)
from app.services.wiring_claim import (
    REBUILT_ROW_PENDING_BUILD,
    SETTLED_ROW_PENDING_RELEASE,
    HolderRecord,
    WiringRowClaims,
    decide_holder_record,
    holder_record,
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


RES = uuid.uuid4()
SW = uuid.uuid4()
R1 = {"destination": "10.0.1.0/24", "next_hop": "10.0.0.1", "interface": "eth1"}
R2 = {"destination": "10.0.2.0/24", "next_hop": "10.0.0.1", "interface": "eth1"}
LAYERS = ("l1", "l2", "l3")
MODEL = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}
FROZEN_REASON = {
    "l1": l1svc.FROZEN_BUILD_PENDING_RELEASE,
    "l2": l2svc.FROZEN_JOIN_PENDING_REMOVAL,
    "l3": l3svc.FROZEN_PROVISION_PENDING_REMOVAL,
}


async def _seed(layer: str, intended: str) -> tuple[uuid.UUID, uuid.UUID | None]:
    """One FAILED row of the reservation; returns (row id, VLAN allocation id or None)."""
    async with TestSessionLocal() as s:
        va = None
        if layer == "l1":
            row = L1ConnectionAssignment(
                reservation_id=RES, switch_device_id=SW, port_a="0/0/1", port_b="0/0/2"
            )
        elif layer == "l2":
            va = VlanAssignment(reservation_id=RES, fabric_id=uuid.uuid4(), vlan_id=100)
            s.add(va)
            await s.flush()
            row = L2PortAssignment(
                reservation_id=RES, vlan_assignment_id=va.id, switch_device_id=SW, port="0/0/1"
            )
        else:
            row = RouteAssignment(reservation_id=RES, device_id=SW, routes=[R1])
        row.status = "FAILED"
        row.intended = intended
        row.attempts = 1
        row.last_error = "driver timeout"
        s.add(row)
        await s.commit()
        return row.id, (va.id if va is not None else None)


async def _rows(layer: str, first=None) -> list:
    """Every row of the reservation for the layer's key, detached; `first` (a row id)
    sorts first (SQLite's CURRENT_TIMESTAMP cannot order rows written in one second)."""
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(model).where(model.reservation_id == RES))).scalars().all()
    return sorted(rows, key=lambda r: (r.id != first, str(r.id)))


async def _state(layer: str, row_id) -> tuple:
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        return row.status, row.intended, row.attempts, row.last_error, row.claimed_until


# --- the other writers, as they land mid-drive ---------------------------------


async def _reconcile_failure(layer, row_id, va, intended):
    """The reconcile's key-based failure write on the same key (no claim, clears it)."""
    async with TestSessionLocal() as s:
        if layer == "l1":
            await l1svc.record_l1_failed(
                s, RES, SW, "0/0/1", "0/0/2", 1, "reconcile failure", intended=intended
            )
        elif layer == "l2":
            await l2svc.record_l2_failed(
                s, RES, va, SW, "0/0/1", 1, "reconcile failure", intended=intended
            )
        else:
            await l3svc.record_route_failed(
                s, RES, SW, None, 1, "reconcile failure", intended=intended
            )


async def _park(layer, row_id, reason=WIRING_STALE_BUILD_REASON):
    """The reconcile's stale-build settlement, or a teardown's park (L3)."""
    park = {
        "l1": l1svc.park_stale_l1_build,
        "l2": l2svc.park_stale_l2_build,
        "l3": l3svc.park_stale_route_build,
    }[layer]
    async with TestSessionLocal() as s:
        await park(s, row_id, reason)


async def _release(layer):
    """A disconnect, leave, or deprovision that succeeded: the row goes RELEASED."""
    async with TestSessionLocal() as s:
        if layer == "l1":
            await l1svc.release_l1_connection(s, RES, SW, "0/0/1", "0/0/2")
        elif layer == "l2":
            await l2svc.release_l2_membership(s, RES, SW, "0/0/1")
        else:
            await l3svc.release_route_membership(s, RES, SW)


async def _rebuild(layer, va):
    """A connect, join, or provision of the same row that succeeded: the row is ACTIVE."""
    async with TestSessionLocal() as s:
        if layer == "l1":
            await l1svc.record_l1_connect(s, RES, SW, "0/0/1", "0/0/2")
        elif layer == "l2":
            await l2svc.record_l2_membership_active(s, RES, va, SW, "0/0/1")
        else:
            await l3svc.record_route_active(s, RES, SW, [R1])


async def _freeze():
    async with TestSessionLocal() as s:
        await l1svc.freeze_reservation_wiring(s, RES)


async def _retake(layer, row_id):
    """Another retry pass claims the row (the holder's own claim is already expired)."""
    other = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await other.claim(s, MODEL[layer], row_id) is True
    return other.held[row_id]


# --- the harness ----------------------------------------------------------------


async def _hold_and_act(
    layer, *, added, succeeded, change=None, holder_budget=None, fail_routes=()
):
    """Seed a row in the action's direction, claim it as the holder, drive it.

    `change(row_id, va)` runs at the row's own driver action, before it answers.
    Returns (row id, the holder's claim ledger). For L3 the provision drives R1 and R2
    (configure R1 then R2), so a provision failure can still have configured R1;
    `fail_routes` lists the route destinations whose configure fails.
    """
    row_id, va = await _seed(layer, "ACTIVE" if added else "RELEASED")
    holder = WiringRowClaims(budget=holder_budget)
    async with TestSessionLocal() as s:
        assert await holder.claim(s, MODEL[layer], row_id) is True

    fired = {"done": False}
    row_actions = {"connect_ports", "disconnect_ports", "add_to_vlan", "remove_from_vlan"}

    async def _driver(driver_path, action, context, password_keys, method_kwargs=None):
        mk = method_kwargs or {}
        if action in row_actions or action in ("configure_route", "remove_route"):
            if change is not None and not fired["done"]:
                fired["done"] = True
                await change(row_id, va)
            if layer == "l3" and action == "configure_route":
                ok = mk.get("destination") not in fail_routes
            else:
                ok = succeeded
            return (
                (True, 1, None, {"success": True, "output": {}}) if ok else (False, 3, "boom", None)
            )
        return True, 1, None, {"success": True, "output": {}}

    switch = {
        "id": str(SW),
        "name": "sw",
        "template_id": "t",
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "sha",
        "driver_filename": "d.zip",
        "connection_type": "Layer 3 Switch",
        "field_data": {},
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch("app.services.nats_consumer._run_driver_with_retry", side_effect=_driver)
        )
        stack.enter_context(
            patch("app.services.nats_consumer._fetch_device", AsyncMock(return_value=switch))
        )
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_template",
                AsyncMock(return_value={"id": "t", "sections": []}),
            )
        )
        stack.enter_context(
            patch("app.services.driver_loader.load_driver", AsyncMock(return_value="/tmp/d"))
        )
        stack.enter_context(
            patch("app.services.driver_loader.get_driver_metadata", AsyncMock(return_value={}))
        )
        if layer == "l1":
            async with TestSessionLocal() as s:
                await _apply_one_port_action(
                    s,
                    "connect_ports" if added else "disconnect_ports",
                    "/tmp/d",
                    {},
                    set(),
                    SW,
                    uuid.uuid4(),
                    "sha",
                    RES,
                    "0/0/1",
                    "0/0/2",
                    None,
                    retry_row_id=row_id,
                    claims=holder,
                )
        elif layer == "l2":
            async with TestSessionLocal() as s:
                await _apply_one_vlan_action(
                    s,
                    "add_to_vlan" if added else "remove_from_vlan",
                    "/tmp/d",
                    {},
                    set(),
                    SW,
                    uuid.uuid4(),
                    "sha",
                    RES,
                    "0/0/1",
                    100,
                    va,
                    retry_row_id=row_id,
                    claims=holder,
                )
        else:
            item = {"device_id": str(SW), "routes": [R1, R2] if added else [R1]}
            async with httpx.AsyncClient() as client:
                await _apply_l3_adjacency(
                    str(RES),
                    [] if added else [item],
                    [item] if added else [],
                    _FetchContext(client),
                    _db_session_factory(),
                    retry_row_ids={str(SW): row_id},
                    claims=holder,
                )
    if layer != "l3":
        assert fired["done"] or change is None, "the change must land mid-drive"
    return row_id, holder


def _only(rows):
    assert len(rows) == 1, [(r.status, r.intended, r.last_error) for r in rows]
    return rows[0]


# --- the decision function: the table in one place --------------------------------


@pytest.mark.parametrize(
    ("row", "added", "succeeded", "frozen", "expected"),
    [
        # The row's own state decides only once the claim is gone and nobody else holds it.
        (dict(exists=False), True, True, False, HolderRecord.WRITE),
        (dict(mine=True, status="RELEASED"), True, True, False, HolderRecord.WRITE),
        (
            dict(held_by_other=True, status="FAILED", intended="ACTIVE"),
            True,
            True,
            False,
            HolderRecord.REFUSE,
        ),
        (dict(status="FAILED", intended="ACTIVE"), True, True, False, HolderRecord.WRITE),
        (dict(status="FAILED", intended="RELEASED"), True, True, False, HolderRecord.PARK),
        (dict(status="FAILED", intended="RELEASED"), True, True, True, HolderRecord.WRITE),
        (dict(status="FAILED", intended="RELEASED"), True, False, True, HolderRecord.PARK),
        (dict(status="RELEASED", intended="RELEASED"), True, True, False, HolderRecord.PARK),
        (dict(status="RELEASED", intended="RELEASED"), True, True, True, HolderRecord.WRITE),
        (dict(status="ACTIVE", intended="ACTIVE"), True, True, False, HolderRecord.KEEP),
        (dict(status="FAILED", intended="RELEASED"), False, True, False, HolderRecord.WRITE),
        (dict(status="FAILED", intended="ACTIVE"), False, True, False, HolderRecord.KEEP),
        (dict(status="FAILED", intended="ACTIVE"), False, True, True, HolderRecord.WRITE),
        (dict(status="FAILED", intended="ACTIVE"), False, False, True, HolderRecord.KEEP),
        (dict(status="ACTIVE", intended="ACTIVE"), False, True, False, HolderRecord.REOPEN),
        (dict(status="RELEASED", intended="RELEASED"), False, True, False, HolderRecord.KEEP),
    ],
)
def test_decide_holder_record(row, added, succeeded, frozen, expected):
    args = dict(exists=True, mine=False, held_by_other=False, status=None, intended=None)
    args.update(row)
    assert decide_holder_record(**args, frozen=frozen, added=added, succeeded=succeeded) is expected


# --- nobody, expired, and another pass's live claim -------------------------------


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("added", [True, False], ids=["build", "release"])
async def test_an_expired_claim_nobody_took_still_records_the_success(layer, added):
    row_id, holder = await _hold_and_act(
        layer, added=added, succeeded=True, holder_budget=timedelta(seconds=-60)
    )
    status = _only(await _rows(layer)).status
    assert status == ("ACTIVE" if added else "RELEASED")
    assert holder.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("added", [True, False], ids=["build", "release"])
async def test_another_pass_with_a_live_claim_keeps_the_row(layer, added):
    """The one exception: the row is that pass's now; the holder records nothing and
    the row reads in_progress. That pass re-drives the row and records its result."""

    async def change(row_id, va):
        await _retake(layer, row_id)

    row_id, holder = await _hold_and_act(
        layer, added=added, succeeded=True, change=change, holder_budget=timedelta(seconds=-60)
    )
    status, intended, attempts, last_error, claimed_until = await _state(layer, row_id)
    assert (status, attempts, last_error) == ("FAILED", 1, "driver timeout")
    assert claimed_until is not None, "the other pass's claim is untouched"
    assert holder.lost == {row_id}


# --- the reconcile's key-based failure write ----------------------------------------


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("added", [True, False], ids=["build", "release"])
async def test_a_reconcile_failure_write_does_not_lose_the_holders_success(layer, added):
    """Review finding (round 3): the reconcile drove the same row, failed, and its
    key-based failure write cleared the claim. The holder's action succeeded on the
    device, so the row records it: ACTIVE for a build, RELEASED for a release."""
    intended = "ACTIVE" if added else "RELEASED"

    async def change(row_id, va):
        await _reconcile_failure(layer, row_id, va, intended)

    row_id, holder = await _hold_and_act(layer, added=added, succeeded=True, change=change)
    assert _only(await _rows(layer)).status == ("ACTIVE" if added else "RELEASED")
    assert holder.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_reconcile_build_failure_on_a_held_release_keeps_the_build(layer):
    """The reconcile tried to build the row the holder was releasing, and failed: the
    row says FAILED intended ACTIVE. The holder's removal succeeded; the row is left for
    the build channels, which re-apply it (the device does not carry it now)."""

    async def change(row_id, va):
        await _reconcile_failure(layer, row_id, va, "ACTIVE")

    row_id, holder = await _hold_and_act(layer, added=False, succeeded=True, change=change)
    row = _only(await _rows(layer))
    assert (row.status, row.intended, row.last_error) == ("FAILED", "ACTIVE", "reconcile failure")
    assert holder.lost == set(), "nothing is running on it: it reads still_failed"


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_holders_failure_after_a_reconcile_failure_keeps_the_reconciles_record(layer):
    """L1 and L2 failure writes keep the strict stamp condition: a refused driver call
    changed nothing on the device, and the reconcile's record stands. The row is not
    reported in_progress, because nothing runs on it."""

    async def change(row_id, va):
        await _reconcile_failure(layer, row_id, va, "ACTIVE")

    row_id, holder = await _hold_and_act(layer, added=True, succeeded=False, change=change)
    row = _only(await _rows(layer))
    assert (row.status, row.intended, row.last_error) == ("FAILED", "ACTIVE", "reconcile failure")
    assert holder.lost == set()


async def test_an_l3_provision_failure_after_a_reconcile_failure_records_its_routes():
    """A failed L3 provision may have configured part of its set (issue #1001), so it is
    recorded like a success: FAILED intended ACTIVE with what may be installed."""

    async def change(row_id, va):
        await _reconcile_failure("l3", row_id, va, "ACTIVE")

    row_id, holder = await _hold_and_act(
        "l3", added=True, succeeded=False, change=change, fail_routes=("10.0.2.0/24",)
    )
    row = _only(await _rows("l3"))
    assert (row.status, row.intended, row.last_error) == ("FAILED", "ACTIVE", "boom")
    assert {r["destination"] for r in row.routes} >= {"10.0.1.0/24", "10.0.2.0/24"}
    assert holder.lost == set()


# --- the reconcile's stale park, and a release after it ----------------------------


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_that_lands_on_a_parked_row_stays_parked_for_release(layer):
    """The reconcile parked the row (its intent is gone) while the holder's build was on
    the device. The build is recorded, but never as ACTIVE over the release decision:
    FAILED intended RELEASED, so the release channels remove it."""

    async def change(row_id, va):
        await _park(layer, row_id)

    row_id, holder = await _hold_and_act(layer, added=True, succeeded=True, change=change)
    row = _only(await _rows(layer))
    assert (row.status, row.intended, row.last_error) == (
        "FAILED",
        "RELEASED",
        SETTLED_ROW_PENDING_RELEASE,
    )
    assert row.claimed_until is None
    if layer == "l3":
        assert {r["destination"] for r in row.routes} >= {"10.0.1.0/24", "10.0.2.0/24"}
    assert holder.lost == set()


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_that_lands_after_the_row_was_released_is_parked_for_release(layer):
    """The reconcile parked and then released the row while the holder's build was on
    the device; which call landed last is unknown, so the row the release left is kept
    and a new FAILED intended RELEASED row records what the build may have added."""

    async def change(row_id, va):
        await _park(layer, row_id)
        await _release(layer)

    row_id, holder = await _hold_and_act(layer, added=True, succeeded=True, change=change)
    rows = await _rows(layer, first=row_id)
    assert [r.status for r in rows] == ["RELEASED", "FAILED"]
    assert (rows[1].intended, rows[1].last_error, rows[1].attempts) == (
        "RELEASED",
        SETTLED_ROW_PENDING_RELEASE,
        0,
    )
    if layer == "l3":
        assert {r["destination"] for r in rows[1].routes} == {"10.0.1.0/24", "10.0.2.0/24"}
    assert holder.lost == set()


async def test_an_l3_provision_failure_after_a_release_parks_only_what_it_configured():
    """Only a confirmed configure added a route; R2's configure failed, so only R1 is
    parked for removal. With nothing confirmed, nothing is recorded (issue #814)."""

    async def change(row_id, va):
        await _park("l3", row_id)
        await _release("l3")

    row_id, _holder = await _hold_and_act(
        "l3", added=True, succeeded=False, change=change, fail_routes=("10.0.2.0/24",)
    )
    rows = await _rows("l3", first=row_id)
    assert [r.status for r in rows] == ["RELEASED", "FAILED"]
    assert [r["destination"] for r in rows[1].routes] == ["10.0.1.0/24"]


async def test_an_l3_provision_that_configured_nothing_records_nothing_after_a_release():
    async def change(row_id, va):
        await _park("l3", row_id)
        await _release("l3")

    await _hold_and_act(
        "l3",
        added=True,
        succeeded=False,
        change=change,
        fail_routes=("10.0.1.0/24", "10.0.2.0/24"),
    )
    assert [r.status for r in await _rows("l3")] == ["RELEASED"]


# --- teardown after the freeze --------------------------------------------------------


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_that_lands_during_teardown_is_parked_by_the_writers_freeze_branch(layer):
    """Review finding (round 3, HIGH): the reservation ended while the holder's build
    was on the device; teardown parked and then released the row. The writer's own
    record-time freeze branch (issue #461) runs and parks what the build added, so the
    release channels remove it; before this round the record was refused and the
    build stayed on the device with no ledger row."""

    async def change(row_id, va):
        await _freeze()
        await _park(layer, row_id, l3svc.TEARDOWN_PENDING_REMOVAL)
        await _release(layer)

    row_id, _holder = await _hold_and_act(layer, added=True, succeeded=True, change=change)
    rows = await _rows(layer, first=row_id)
    assert [r.status for r in rows] == ["RELEASED", "FAILED"]
    assert (rows[1].intended, rows[1].last_error) == ("RELEASED", FROZEN_REASON[layer])
    if layer == "l3":
        assert {r["destination"] for r in rows[1].routes} >= {"10.0.1.0/24", "10.0.2.0/24"}


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_build_that_lands_on_a_teardown_park_is_parked_by_the_freeze_branch(layer):
    async def change(row_id, va):
        await _freeze()
        await _park(layer, row_id, l3svc.TEARDOWN_PENDING_REMOVAL)

    await _hold_and_act(layer, added=True, succeeded=True, change=change)
    row = _only(await _rows(layer))
    assert (row.status, row.intended, row.last_error) == (
        "FAILED",
        "RELEASED",
        FROZEN_REASON[layer],
    )


@pytest.mark.parametrize("layer", LAYERS)
async def test_a_release_that_lands_after_teardown_released_the_row_keeps_it_released(layer):
    async def change(row_id, va):
        await _freeze()
        await _release(layer)

    row_id, holder = await _hold_and_act(layer, added=False, succeeded=True, change=change)
    assert _only(await _rows(layer)).status == "RELEASED"
    assert holder.lost == set()


# --- the row rebuilt ACTIVE while the holder released it -------------------------------


@pytest.mark.parametrize(
    ("layer", "succeeded"),
    [("l1", True), ("l2", True), ("l3", True), ("l3", False)],
    ids=["l1", "l2", "l3", "l3-deprovision-failed"],
)
async def test_a_release_that_lands_on_a_rebuilt_row_reopens_it_for_the_build(layer, succeeded):
    """The row was rebuilt ACTIVE (the reconcile) while the holder's removal was on the
    device; whichever call landed last, the device may not carry it now. The row is
    reopened FAILED intended ACTIVE with attempts 0, so the build channels re-apply it.
    A failed L3 deprovision may also have removed part of the set, so it reopens too;
    a failed L1 or L2 removal changed nothing and keeps the strict refusal."""

    async def change(row_id, va):
        await _rebuild(layer, va)

    row_id, holder = await _hold_and_act(layer, added=False, succeeded=succeeded, change=change)
    row = _only(await _rows(layer))
    assert (row.status, row.intended, row.attempts, row.last_error) == (
        "FAILED",
        "ACTIVE",
        0,
        REBUILT_ROW_PENDING_BUILD,
    )
    assert holder.lost == set()


# --- the guard mechanism itself: the lock is released, an error rolls back -------------


async def test_holder_record_ends_its_transaction_when_it_writes_nothing():
    """The plan's lock lives in the holder's per-switch session; exiting must commit even
    when the body wrote nothing, or the row stays locked across the switch's remaining
    driver calls."""
    row_id, _va = await _seed("l1", "ACTIVE")
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await claims.claim(s, L1ConnectionAssignment, row_id)
        async with holder_record(
            s, L1ConnectionAssignment, row_id, claims, added=True, succeeded=True
        ) as plan:
            assert plan.kind is HolderRecord.WRITE
            assert s.in_transaction()
        assert not s.in_transaction()


async def test_holder_record_rolls_back_on_an_error():
    row_id, _va = await _seed("l1", "ACTIVE")
    claims = WiringRowClaims()
    async with TestSessionLocal() as s:
        assert await claims.claim(s, L1ConnectionAssignment, row_id)
        with pytest.raises(RuntimeError):
            async with holder_record(
                s, L1ConnectionAssignment, row_id, claims, added=True, succeeded=True
            ) as plan:
                plan.row.last_error = "never committed"
                raise RuntimeError("boom")
        assert not s.in_transaction()
    assert (await _state("l1", row_id))[3] == "driver timeout"
