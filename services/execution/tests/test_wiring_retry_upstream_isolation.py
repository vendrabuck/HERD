"""One reservation's failed upstream read stops only that reservation (issue #1204).

The background retry tick drives a batch that spans reservations, and each layer retry
calls its consumer apply once per reservation. Before #1204 a TransientUpstreamError
raised inside one reservation's apply (inventory's switch or template read, L2's
fail-closed fabric lookup, the L3 route apply) escaped the whole layer: the tick caught
it, dropped the layer's outcomes, and left EVERY selected row of that layer unchanged,
including the rows of reservations the iteration had not reached yet. Twenty release
rows on a switch whose inventory read failed hid a newer row on a healthy switch, tick
after tick.

The invariant pinned here, per layer, through the real entry point
`run_wiring_retry_tick` with fakes only at the upstream seam (inventory reads, the
fabric lookup, the driver load and the sandbox; everything from the ledger down is real):

    an upstream read failure inside one reservation's layer apply leaves ONLY that
    reservation's rows of that layer unchanged for the tick (no attempt counted, no
    claim left), and every other reservation's rows in the same layer and tick are
    driven as if that reservation were absent.

Also pinned: the order the reservations are applied in does not matter (the failing one
first or last); the stat `skipped_upstream` counts only rows left unchanged, not rows
an apply drove before it raised; the log line carries the exception class name and
never the upstream text; an exception other than TransientUpstreamError still drops the
layer (the catch is not wider than it says); and the manual channel still propagates
the error, which its endpoint answers with 503 (WIRE-RETRY-12).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services import nats_consumer
from app.services.nats_consumer import TransientUpstreamError
from app.services.wiring_retry_service import (
    RESERVATION_UPSTREAM_FAILED_ACTION,
    SUPERSEDED_RELEASE_UPSTREAM_FAILED_ACTION,
    reattempt_reservation,
    run_wiring_retry_tick,
)
from herd_common.logging import JSONFormatter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

BATCH = 20
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEWER = OLD + timedelta(days=1)

DRIVER_ID = uuid.uuid4()
GOOD_SWITCH = uuid.uuid4()
# The upstream text a failing read carries. It must never reach the log line.
UPSTREAM_TEXT = "inventory answered 500: db password=hunter2 at 10.9.8.7"
TEMPLATE_DATA = {"id": "tmpl-1", "name": "Template", "sections": []}
SUCCESS = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 1}
ROUTES = [{"destination": "198.51.100.0/24", "next_hop": "198.51.100.1"}]

LAYERS = ("l1", "l2", "l3")
MODELS = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}
CONNECTION_TYPES = {"l1": "Layer 1 Switch", "l2": "Layer 2 Switch", "l3": "Layer 3 Switch"}
RELEASE_ACTIONS = {"l1": "disconnect_ports", "l2": "remove_from_vlan", "l3": "remove_route"}
APPLY_NAMES = {
    "l1": "_apply_wiring_pairs",
    "l2": "_apply_l2_memberships",
    "l3": "_apply_l3_adjacency",
}


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _pin_tick_settings(monkeypatch):
    monkeypatch.setattr(settings, "wiring_retry_max_attempts", 10)
    monkeypatch.setattr(settings, "wiring_retry_batch_size", BATCH)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    return lambda: _Ctx()


def _recorder():
    calls: list[str] = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        return SUCCESS

    return execute_fn, calls


@contextmanager
def _upstream(layer: str, failing: set[uuid.UUID], exc_type=TransientUpstreamError):
    """Inventory, the driver load, and the sandbox. A device read for an id in
    `failing` raises `exc_type` with UPSTREAM_TEXT; every other switch answers as a
    switch of the layer's connection type, every other device as a plain server."""
    failing_ids = {str(d) for d in failing}
    execute_fn, calls = _recorder()

    async def _device(device_id, client=None):
        if str(device_id) in failing_ids:
            raise exc_type(UPSTREAM_TEXT)
        if str(device_id).startswith("dut-"):
            return {"id": str(device_id), "name": "dut", "connection_type": "Server"}
        return {
            "id": str(device_id),
            "name": "switch",
            "template_id": "tmpl-1",
            "driver_id": str(DRIVER_ID),
            "driver_sha256": "sha256abc",
            "driver_filename": "driver.zip",
            "connection_type": CONNECTION_TYPES[layer],
            "field_data": {"ip": "10.0.0.1"},
        }

    with ExitStack() as stack:
        for p in (
            patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
            patch(
                "app.services.nats_consumer._fetch_template",
                new=AsyncMock(return_value=TEMPLATE_DATA),
            ),
            patch(
                "app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")
            ),
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        ):
            stack.enter_context(p)
        yield calls


@contextmanager
def _apply_order(layer: str):
    """Record the reservation each consumer apply call is made for, in call order."""
    real = getattr(nats_consumer, APPLY_NAMES[layer])
    order: list[str] = []

    async def _spy(reservation_id, *args, **kwargs):
        order.append(str(reservation_id))
        return await real(reservation_id, *args, **kwargs)

    with patch.object(nats_consumer, APPLY_NAMES[layer], new=_spy):
        yield order


async def _seed_release_rows(
    layer: str,
    reservation_id: uuid.UUID,
    switches: list[uuid.UUID],
    *,
    created_at: datetime,
    vlan_id: int = 1001,
) -> list[uuid.UUID]:
    """One retryable RELEASE-direction FAILED row per entry of `switches`, all older
    than `created_at` plus their index in seconds (oldest first in the list).

    Release direction on purpose: a release needs no fork fetch and no intent
    revalidation, so the failing read is the apply's own switch read. An L2 row needs
    its reservation's allocation to resolve the VLAN number; an L3 pin is one row per
    (reservation, switch), so an L3 reservation with several rows uses several switches.
    """
    ids = []
    async with TestSessionLocal() as s:
        alloc_id = None
        if layer == "l2":
            alloc = VlanAssignment(
                reservation_id=reservation_id, fabric_id=uuid.uuid4(), vlan_id=vlan_id
            )
            s.add(alloc)
            await s.flush()
            alloc_id = alloc.id
        for i, switch in enumerate(switches):
            common = dict(
                reservation_id=reservation_id,
                status="FAILED",
                intended="RELEASED",
                attempts=1,
                last_error="driver timeout",
                created_at=created_at + timedelta(seconds=i),
            )
            if layer == "l1":
                row = L1ConnectionAssignment(
                    switch_device_id=switch, port_a=f"p{i}/a", port_b=f"p{i}/b", **common
                )
            elif layer == "l2":
                row = L2PortAssignment(
                    switch_device_id=switch, port=f"p{i}", vlan_assignment_id=alloc_id, **common
                )
            else:
                row = RouteAssignment(device_id=switch, routes=ROUTES, **common)
            s.add(row)
            await s.flush()
            ids.append(row.id)
        await s.commit()
    return ids


def _a_switches(layer: str, count: int) -> list[uuid.UUID]:
    """The failing reservation's switches: one for L1 and L2, one per row for L3."""
    if layer == "l3":
        return [uuid.uuid4() for _ in range(count)]
    return [uuid.uuid4()] * count


async def _snapshot(layer: str) -> dict:
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(MODELS[layer]))).scalars().all()
    return {r.id: (r.status, r.intended, r.attempts, r.last_error, r.claimed_until) for r in rows}


def _isolation_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "action", None) == RESERVATION_UPSTREAM_FAILED_ACTION
    ]


# --- The door: a failing reservation no longer hides the rest of its layer ---


@pytest.mark.parametrize("layer", LAYERS)
async def test_failing_reservation_leaves_only_its_own_rows_unchanged(layer, caplog):
    """Reservation A holds BATCH - 1 release rows on switches whose inventory read
    fails; reservation B holds one newer release row on a healthy switch, so both fit in
    one batch. The tick drives B to RELEASED and leaves every row of A exactly as it
    was: same status, attempts and reason, no claim stamp. The stats count A's rows in
    skipped_upstream (and, as before, in still_failed and rows_retried), and the one log
    line for A names the class, never the upstream text."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    switches_a = _a_switches(layer, BATCH - 1)
    a_ids = await _seed_release_rows(layer, res_a, switches_a, created_at=OLD)
    (b_id,) = await _seed_release_rows(layer, res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)
    before = await _snapshot(layer)

    caplog.set_level(logging.WARNING, logger="app.services.wiring_retry_service")
    with _upstream(layer, set(switches_a)) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot(layer)
    assert after[b_id][0] == "RELEASED", "the healthy reservation is driven"
    assert calls.count(RELEASE_ACTIONS[layer]) == 1, "exactly B's row reached the driver"
    assert {i: after[i] for i in a_ids} == {i: before[i] for i in a_ids}, (
        "the failing reservation's rows are untouched: no attempt, no reason, no claim"
    )
    assert all(after[i][4] is None for i in a_ids), "no claim stamp is left behind"

    assert stats["rows_due"] == BATCH
    assert stats["rows_retried"] == BATCH
    assert stats[f"{layer}_rows_retried"] == BATCH
    assert stats["released"] == 1
    assert stats["still_failed"] == BATCH - 1
    assert stats["skipped_upstream"] == BATCH - 1

    records = _isolation_records(caplog)
    assert len(records) == 1, "one line per skipped reservation"
    formatted = JSONFormatter("execution").format(records[0])
    assert f'"reservation_id": "{res_a}"' in formatted
    assert f'"layer": "{layer}"' in formatted
    assert '"error_class": "TransientUpstreamError"' in formatted
    assert f'"rows_unchanged": {BATCH - 1}' in formatted
    assert "hunter2" not in formatted and "10.9.8.7" not in formatted, (
        "upstream text never reaches the log line (class-name-only rule)"
    )


@pytest.mark.parametrize("a_first", [True, False], ids=["failing_first", "failing_last"])
@pytest.mark.parametrize("layer", LAYERS)
async def test_isolation_does_not_depend_on_which_reservation_is_applied_first(layer, a_first):
    """The failing reservation A is applied before B (its rows older) and after B (its
    rows newer). Either way B is driven and the stats describe both reservations, so a
    catch that only helped the reservations after A, or an A whose error escaped after B
    was driven (the layer's outcomes then dropped), both fail here. The spy proves the
    two orders really ran, since the per-reservation applies follow selection order."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    switches_a = _a_switches(layer, 3)
    a_at, b_at = (OLD, NEWER) if a_first else (NEWER, OLD)
    a_ids = await _seed_release_rows(layer, res_a, switches_a, created_at=a_at)
    (b_id,) = await _seed_release_rows(layer, res_b, [GOOD_SWITCH], created_at=b_at, vlan_id=1002)
    before = await _snapshot(layer)

    with _upstream(layer, set(switches_a)) as calls, _apply_order(layer) as order:
        stats = await run_wiring_retry_tick(_db_session_factory())

    expected = [str(res_a), str(res_b)] if a_first else [str(res_b), str(res_a)]
    assert order == expected, "the applies run in selection order"
    after = await _snapshot(layer)
    assert after[b_id][0] == "RELEASED"
    assert calls.count(RELEASE_ACTIONS[layer]) == 1
    assert {i: after[i] for i in a_ids} == {i: before[i] for i in a_ids}
    assert stats[f"{layer}_rows_retried"] == 4
    assert (stats["released"], stats["still_failed"], stats["skipped_upstream"]) == (1, 3, 3)


# --- L2: the fail-closed fabric lookup ------------------------------------


async def _seed_fabric_case():
    """A: a release row plus a nil-allocation build row on one L2 switch; B: one newer
    release row on GOOD_SWITCH. Returns (switch_a, a_release, a_build, b_id)."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    switch_a = uuid.uuid4()
    (a_release,) = await _seed_release_rows("l2", res_a, [switch_a], created_at=OLD)
    async with TestSessionLocal() as s:
        build = L2PortAssignment(
            reservation_id=res_a,
            switch_device_id=switch_a,
            port="build/1",
            vlan_assignment_id=uuid.UUID(int=0),
            status="FAILED",
            intended="ACTIVE",
            attempts=2,
            last_error="driver timeout",
            created_at=OLD + timedelta(seconds=30),
        )
        s.add(build)
        await s.commit()
        a_build = build.id
    (b_id,) = await _seed_release_rows("l2", res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)
    return switch_a, a_release, a_build, b_id


@contextmanager
def _fabric_failing_for(switch_a: uuid.UUID, exc_type):
    """Inventory as in _upstream("l2"), A's fork intending its build row (so
    revalidation passes and re-resolution runs), and a fabric lookup that raises
    `exc_type` for A's switch only."""
    wire = {
        "device_a_id": "dut-1",
        "port_a": "eth0",
        "device_b_id": str(switch_a),
        "port_b": "build/1",
        "layer": "L1",
        "physical_connection_id": None,
        "edge_key": None,
    }

    async def _fabric(device_id):
        if str(device_id) == str(switch_a):
            raise exc_type(UPSTREAM_TEXT)
        return uuid.uuid5(uuid.NAMESPACE_URL, str(device_id))

    with (
        _upstream("l2", set()) as calls,
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=[wire]),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(side_effect=_fabric)),
    ):
        yield calls


async def test_l2_fabric_lookup_failure_skips_only_that_reservation():
    """A's build row has no usable allocation (the nil placeholder), so the retry
    re-resolves it through the fabric lookup, which cabling cannot answer for A's
    switch. A also holds a release row. The re-resolution failure skips A's apply
    altogether: neither row is driven or re-parked with the no-allocation reason, no
    allocation is minted, and B's release on another switch is still driven."""
    switch_a, a_release, a_build, b_id = await _seed_fabric_case()
    before = await _snapshot("l2")
    async with TestSessionLocal() as s:
        allocations_before = len((await s.execute(select(VlanAssignment))).scalars().all())

    with _fabric_failing_for(switch_a, TransientUpstreamError) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot("l2")
    assert after[b_id][0] == "RELEASED", "the other reservation is driven"
    assert calls == ["login", "remove_from_vlan", "logout"], "only B's switch was driven"
    assert after[a_build] == before[a_build], "not re-parked with the no-allocation reason"
    assert after[a_release] == before[a_release], "A's release waits for the next tick too"
    async with TestSessionLocal() as s:
        allocations_after = len((await s.execute(select(VlanAssignment))).scalars().all())
    assert allocations_after == allocations_before, "nothing is allocated for A"
    assert (stats["released"], stats["still_failed"], stats["skipped_upstream"]) == (1, 2, 2)


async def test_l2_fabric_lookup_error_other_than_transient_still_drops_the_layer():
    """The control for the re-resolution catch: a fabric lookup that raises anything
    but TransientUpstreamError escapes the L2 retry, so the layer is dropped for the
    tick and B is not driven."""
    switch_a, _a_release, _a_build, b_id = await _seed_fabric_case()

    with _fabric_failing_for(switch_a, RuntimeError) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert calls == []
    assert (await _snapshot("l2"))[b_id][0] == "FAILED"
    assert (stats["l2_rows_retried"], stats["skipped_upstream"]) == (0, 0)


# --- Accounting: rows an apply drove before it raised are not "unchanged" ----


async def test_rows_driven_before_the_apply_raised_are_not_counted_unchanged(caplog):
    """An apply can raise partway, after it drove and recorded some of the
    reservation's rows (a second switch's read failing, for instance). Simulated
    deterministically: A's apply runs for real and then raises. A's rows keep the state
    the apply gave them, with their claims cleared by the record, and skipped_upstream
    counts none of them; the log line still names A with rows_unchanged 0."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    a_ids = await _seed_release_rows("l1", res_a, [uuid.uuid4()] * 2, created_at=OLD)
    (b_id,) = await _seed_release_rows("l1", res_b, [GOOD_SWITCH], created_at=NEWER)
    real = nats_consumer._apply_wiring_pairs

    async def _drive_then_raise(reservation_id, *args, **kwargs):
        await real(reservation_id, *args, **kwargs)
        if str(reservation_id) == str(res_a):
            raise TransientUpstreamError(UPSTREAM_TEXT)

    caplog.set_level(logging.WARNING, logger="app.services.wiring_retry_service")
    with (
        _upstream("l1", set()),
        patch.object(nats_consumer, "_apply_wiring_pairs", new=_drive_then_raise),
    ):
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot("l1")
    assert [after[i][0] for i in a_ids] == ["RELEASED", "RELEASED"]
    assert all(after[i][4] is None for i in a_ids), "the record cleared each claim"
    assert after[b_id][0] == "RELEASED"
    assert stats["released"] == 3
    assert stats["skipped_upstream"] == 0
    (record,) = _isolation_records(caplog)
    assert '"rows_unchanged": 0' in JSONFormatter("execution").format(record)


async def test_a_row_the_other_channel_holds_is_in_progress_not_skipped_upstream():
    """A's apply loses the claim on its one row to the other channel (the manual retry
    claims it after the select; simulated by stamping `claimed_until` before the real
    apply runs) and then raises TransientUpstreamError. The row was not left unchanged
    by the upstream failure, someone else is driving it: it reads back in_progress and
    is NOT counted in skipped_upstream. Without the `claims.lost` exclusion in
    _record_upstream_skips it would be counted in skipped_upstream while it is not in
    still_failed, breaking the rule that skipped_upstream is a subset of still_failed."""
    res_a = uuid.uuid4()
    (a_id,) = await _seed_release_rows("l1", res_a, [GOOD_SWITCH], created_at=OLD)
    real = nats_consumer._apply_wiring_pairs

    async def _claimed_elsewhere_then_raise(reservation_id, *args, **kwargs):
        async with TestSessionLocal() as s:
            await s.execute(
                update(L1ConnectionAssignment)
                .where(L1ConnectionAssignment.id == a_id)
                .values(claimed_until=datetime.now(timezone.utc) + timedelta(minutes=5))
            )
            await s.commit()
        await real(reservation_id, *args, **kwargs)
        raise TransientUpstreamError(UPSTREAM_TEXT)

    with (
        _upstream("l1", set()) as calls,
        patch.object(nats_consumer, "_apply_wiring_pairs", new=_claimed_elsewhere_then_raise),
    ):
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert "disconnect_ports" not in calls, "the claim was lost, so the row is not driven"
    assert (stats["in_progress"], stats["still_failed"], stats["skipped_upstream"]) == (1, 0, 0)


async def _seed_superseded_cleanup_case(cleanup_switch: uuid.UUID) -> tuple:
    """C: one L2 release row on a port reservation D now holds ACTIVE, so the retry
    settles it superseded and releases C's allocation afterwards. C's allocation is
    defined on `cleanup_switch`, so that release runs delete_vlan there and reads the
    switch from inventory. Returns (res_c, c_id)."""
    res_c, res_d = uuid.uuid4(), uuid.uuid4()
    shared_switch = uuid.uuid4()
    async with TestSessionLocal() as s:
        alloc_c = VlanAssignment(
            reservation_id=res_c,
            fabric_id=uuid.uuid4(),
            vlan_id=1003,
            defined_switch_ids=[str(cleanup_switch)],
        )
        alloc_d = VlanAssignment(reservation_id=res_d, fabric_id=uuid.uuid4(), vlan_id=1004)
        s.add_all([alloc_c, alloc_d])
        await s.flush()
        c_row = L2PortAssignment(
            reservation_id=res_c,
            switch_device_id=shared_switch,
            port="c0",
            vlan_assignment_id=alloc_c.id,
            status="FAILED",
            intended="RELEASED",
            attempts=1,
            last_error="driver timeout",
            created_at=NEWER + timedelta(minutes=5),
        )
        d_row = L2PortAssignment(
            reservation_id=res_d,
            switch_device_id=shared_switch,
            port="c0",
            vlan_assignment_id=alloc_d.id,
            status="ACTIVE",
            intended="ACTIVE",
        )
        s.add_all([c_row, d_row])
        await s.commit()
        return res_c, c_row.id


async def _allocation_of(reservation_id: uuid.UUID) -> VlanAssignment:
    async with TestSessionLocal() as s:
        return (
            await s.execute(
                select(VlanAssignment).where(VlanAssignment.reservation_id == reservation_id)
            )
        ).scalar_one()


def _vlan_delete_upstream_records(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if getattr(r, "action", None) == "vlan_delete_upstream_failed"
    ]


def _release_raising_for(superseded_alloc: uuid.UUID):
    """A stand-in for _release_orphaned_allocations that raises TransientUpstreamError
    (with UPSTREAM_TEXT) for the call releasing `superseded_alloc`, the retry's
    post-loop release, and delegates every other call (each reservation's L2 apply
    tail) to the real function."""
    real = nats_consumer._release_orphaned_allocations

    async def _release(vlan_assignment_ids, get_db_session, ctx):
        if superseded_alloc in set(vlan_assignment_ids):
            raise TransientUpstreamError(UPSTREAM_TEXT)
        return await real(vlan_assignment_ids, get_db_session, ctx)

    return _release


def _backstop_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if getattr(r, "action", None) == SUPERSEDED_RELEASE_UPSTREAM_FAILED_ACTION
    ]


async def test_l2_superseded_allocation_release_failure_keeps_the_outcomes(caplog):
    """The reviewer's probe. A's two L2 release rows sit on a failing switch (the
    per-reservation catch), B's release row is healthy, and C's row is superseded, so
    its allocation is released after the per-reservation loop; that release's
    delete_vlan switch read fails too. Since #1209 the release catches that read for
    C's allocation itself (released, its switch still listed, one
    `vlan_delete_upstream_failed` line with the class name only), so nothing reaches
    the tick's backstop and its line is not logged. The layer's outcomes,
    skipped_upstream, and A's log line all survive as before."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    failing_switch, cleanup_switch = uuid.uuid4(), uuid.uuid4()
    a_ids = await _seed_release_rows("l2", res_a, [failing_switch] * 2, created_at=OLD)
    (b_id,) = await _seed_release_rows("l2", res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)
    res_c, c_id = await _seed_superseded_cleanup_case(cleanup_switch)
    before = await _snapshot("l2")

    caplog.set_level(logging.WARNING, logger="app.services.wiring_retry_service")
    caplog.set_level(logging.WARNING, logger="app.services.nats_consumer")
    with _upstream("l2", {failing_switch, cleanup_switch}) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot("l2")
    assert after[b_id][0] == "RELEASED" and after[c_id][0] == "RELEASED"
    assert calls.count("remove_from_vlan") == 1, "only B's row reached the driver"
    assert "delete_vlan" not in calls, "C's delete stopped at its switch read"
    assert {i: after[i] for i in a_ids} == {i: before[i] for i in a_ids}
    assert stats["l2_rows_retried"] == 4
    assert (stats["released"], stats["superseded"], stats["still_failed"]) == (1, 1, 2)
    assert stats["skipped_upstream"] == 2
    assert len(_isolation_records(caplog)) == 1, "A's line is still logged"
    alloc_c = await _allocation_of(res_c)
    assert alloc_c.status == "RELEASED"
    assert alloc_c.defined_switch_ids == [str(cleanup_switch)]
    (record,) = _vlan_delete_upstream_records(caplog)
    formatted = JSONFormatter("execution").format(record)
    assert '"error_class": "TransientUpstreamError"' in formatted
    assert "hunter2" not in formatted and "10.9.8.7" not in formatted
    assert _backstop_records(caplog) == [], "the release no longer raises into the tick"


async def test_l2_superseded_release_backstop_keeps_the_outcomes(caplog):
    """The tick's backstop around the superseded rows' release (issue #1204 review)
    still holds when a TransientUpstreamError does escape the release (forced here by
    replacing the release, since no read inside it raises today): the outcomes and the
    skip accounting stand, and one line carries the backstop action and the class name
    only."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    failing_switch, cleanup_switch = uuid.uuid4(), uuid.uuid4()
    await _seed_release_rows("l2", res_a, [failing_switch] * 2, created_at=OLD)
    (b_id,) = await _seed_release_rows("l2", res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)
    res_c, c_id = await _seed_superseded_cleanup_case(cleanup_switch)
    c_alloc = (await _allocation_of(res_c)).id

    caplog.set_level(logging.WARNING, logger="app.services.wiring_retry_service")
    with (
        _upstream("l2", {failing_switch}),
        patch.object(
            nats_consumer,
            "_release_orphaned_allocations",
            new=_release_raising_for(c_alloc),
        ),
    ):
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot("l2")
    assert after[b_id][0] == "RELEASED" and after[c_id][0] == "RELEASED"
    assert stats["l2_rows_retried"] == 4
    assert (stats["released"], stats["superseded"], stats["still_failed"]) == (1, 1, 2)
    assert stats["skipped_upstream"] == 2
    assert len(_isolation_records(caplog)) == 1
    (cleanup,) = _backstop_records(caplog)
    formatted = JSONFormatter("execution").format(cleanup)
    assert '"error_class": "TransientUpstreamError"' in formatted
    assert "hunter2" not in formatted and "10.9.8.7" not in formatted


async def test_manual_retry_superseded_release_switch_read_failure_reports_the_outcome(caplog):
    """Since #1209 the manual channel no longer answers 503 for a failed delete_vlan
    switch read in the superseded rows' release: the release catches it per allocation,
    so the retry returns the row's `superseded` outcome, and the allocation is released
    with its switch still listed and one `vlan_delete_upstream_failed` line."""
    cleanup_switch = uuid.uuid4()
    res_c, c_id = await _seed_superseded_cleanup_case(cleanup_switch)

    caplog.set_level(logging.WARNING, logger="app.services.nats_consumer")
    with _upstream("l2", {cleanup_switch}) as calls:
        result = await reattempt_reservation(res_c, _db_session_factory())

    assert [r["outcome"] for r in result["results"]] == ["superseded"]
    assert (await _snapshot("l2"))[c_id][0] == "RELEASED"
    assert "delete_vlan" not in calls
    alloc_c = await _allocation_of(res_c)
    assert (alloc_c.status, alloc_c.defined_switch_ids) == ("RELEASED", [str(cleanup_switch)])
    assert len(_vlan_delete_upstream_records(caplog)) == 1


async def test_manual_retry_still_propagates_the_superseded_release_failure():
    """On the manual channel a TransientUpstreamError that escapes the superseded rows'
    release (forced here by replacing the release, since no read inside it raises
    since #1209) still propagates (503, WIRE-RETRY-12); the superseded row itself is
    already RELEASED."""
    cleanup_switch = uuid.uuid4()
    res_c, c_id = await _seed_superseded_cleanup_case(cleanup_switch)

    c_alloc = (await _allocation_of(res_c)).id
    with (
        _upstream("l2", set()),
        patch.object(
            nats_consumer,
            "_release_orphaned_allocations",
            new=_release_raising_for(c_alloc),
        ),
    ):
        with pytest.raises(TransientUpstreamError):
            await reattempt_reservation(res_c, _db_session_factory())

    assert (await _snapshot("l2"))[c_id][0] == "RELEASED"


async def test_l2_apply_tail_delete_read_failure_is_not_a_reservation_skip(caplog):
    """A delete read failure at the tail of ONE reservation's L2 apply inside the tick
    (issue #1209 review). A's release row sits on a healthy member switch; A's
    allocation is also defined on a transit switch whose inventory read fails, which
    only the allocation-release tail of A's apply reads. B's row is healthy.

    The release catches the read for A's allocation itself: A's row is driven and
    RELEASED, A's allocation is released with only the transit switch still listed,
    and one `vlan_delete_upstream_failed` line is logged. Nothing escapes A's apply, so
    the tick's per-reservation catch never fires: no
    `wiring_retry_reservation_upstream_failed` line, `skipped_upstream` 0, and both
    rows count as released. Before #1209 the error escaped A's apply into that catch,
    which logged A as an upstream skip (with 0 rows unchanged, since A's row was
    already driven)."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    member_a, transit_a = uuid.uuid4(), uuid.uuid4()
    (a_id,) = await _seed_release_rows("l2", res_a, [member_a], created_at=OLD)
    (b_id,) = await _seed_release_rows("l2", res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)
    async with TestSessionLocal() as s:
        await s.execute(
            update(VlanAssignment)
            .where(VlanAssignment.reservation_id == res_a)
            .values(defined_switch_ids=[str(member_a), str(transit_a)])
        )
        await s.commit()

    caplog.set_level(logging.WARNING, logger="app.services.wiring_retry_service")
    caplog.set_level(logging.WARNING, logger="app.services.nats_consumer")
    with _upstream("l2", {transit_a}) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    after = await _snapshot("l2")
    assert after[a_id][0] == "RELEASED" and after[b_id][0] == "RELEASED"
    assert calls.count("remove_from_vlan") == 2
    alloc_a = await _allocation_of(res_a)
    assert (alloc_a.status, alloc_a.defined_switch_ids) == ("RELEASED", [str(transit_a)])
    assert stats["l2_rows_retried"] == 2
    assert (stats["released"], stats["still_failed"], stats["skipped_upstream"]) == (2, 0, 0)
    assert _isolation_records(caplog) == [], "A's apply did not raise, so A is not a skip"
    (record,) = _vlan_delete_upstream_records(caplog)
    formatted = JSONFormatter("execution").format(record)
    assert f'"reservation_id": "{res_a}"' in formatted
    assert f'"switch_ids": ["{transit_a}"]' in formatted
    assert '"error_class": "TransientUpstreamError"' in formatted
    assert "hunter2" not in formatted and "10.9.8.7" not in formatted


# --- A failure remembered by one release pass only (issue #1358 review) ----


@contextmanager
def _blip(once: set[uuid.UUID]):
    """L2 upstream seam where each switch in `once` fails its FIRST device read only
    (a blip: retryable for whoever saw it). Yields (reads, calls): reads maps a device
    id to its read count, calls records (action, device_id) of every driver call."""
    once_ids = {str(d) for d in once}
    reads: dict[str, int] = {}
    calls: list[tuple[str, str]] = []

    async def _device(device_id, client=None):
        key = str(device_id)
        reads[key] = reads.get(key, 0) + 1
        if key in once_ids and reads[key] == 1:
            raise TransientUpstreamError(UPSTREAM_TEXT)
        if key.startswith("dut-"):
            return {"id": key, "name": "dut", "connection_type": "Server"}
        return {
            "id": key,
            "name": "switch",
            "template_id": "tmpl-1",
            "driver_id": str(DRIVER_ID),
            "driver_sha256": "sha256abc",
            "driver_filename": "driver.zip",
            "connection_type": "Layer 2 Switch",
            "field_data": {"ip": "10.0.0.1"},
        }

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append((action, context.get("HERD_device_id")))
        return SUCCESS

    with ExitStack() as stack:
        for p in (
            patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
            patch(
                "app.services.nats_consumer._fetch_template",
                new=AsyncMock(return_value=TEMPLATE_DATA),
            ),
            patch(
                "app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")
            ),
            patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        ):
            stack.enter_context(p)
        yield reads, calls


def _deletes(calls) -> list[str]:
    return sorted(device for action, device in calls if action == "delete_vlan")


async def test_a_blip_in_one_reservations_apply_does_not_stop_anothers_superseded_delete():
    """The review's scenario, through the real tick. Switch X blips once while
    reservation A's L2 apply reads it (A is skipped for the tick, retryable). Later in
    the same tick, C's superseded allocation, defined on X, is released. That release
    pass reads X again (it never consults a failure from outside itself), the read
    answers, delete_vlan runs on X, and C's defined_switch_ids is empty. Remembering
    A's failure on the shared context instead would release C with X listed, a
    definition no later pass would ever delete (WIRE-VLAN-12)."""
    res_a = uuid.uuid4()
    switch_x = uuid.uuid4()
    a_ids = await _seed_release_rows("l2", res_a, [switch_x] * 2, created_at=OLD)
    res_c, c_id = await _seed_superseded_cleanup_case(switch_x)
    before = await _snapshot("l2")

    with _blip({switch_x}) as (reads, calls):
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert reads[str(switch_x)] == 2, "A's apply read X, then C's release read it again"
    assert _deletes(calls) == [str(switch_x)]
    after = await _snapshot("l2")
    assert {i: after[i] for i in a_ids} == {i: before[i] for i in a_ids}, "A stays retryable"
    assert after[c_id][0] == "RELEASED"
    assert stats["skipped_upstream"] == 2
    alloc_c = await _allocation_of(res_c)
    assert (alloc_c.status, alloc_c.defined_switch_ids) == ("RELEASED", [])


async def test_a_failure_in_one_release_pass_does_not_reach_a_later_pass_in_the_same_tick():
    """The same tick, but the earlier failure happens INSIDE a release pass: A's
    release row on a healthy member switch M is driven, and the release tail of A's
    apply blips on X (A's allocation is also defined on X), so A's allocation is
    released with X still listed. C's superseded allocation on X is then released by
    a later pass on the same context: X is read again and its delete runs."""
    res_a = uuid.uuid4()
    member_m, switch_x = uuid.uuid4(), uuid.uuid4()
    (a_id,) = await _seed_release_rows("l2", res_a, [member_m], created_at=OLD)
    async with TestSessionLocal() as s:
        await s.execute(
            update(VlanAssignment)
            .where(VlanAssignment.reservation_id == res_a)
            .values(defined_switch_ids=[str(member_m), str(switch_x)])
        )
        await s.commit()
    res_c, c_id = await _seed_superseded_cleanup_case(switch_x)

    with _blip({switch_x}) as (reads, calls):
        await run_wiring_retry_tick(_db_session_factory())

    assert reads[str(switch_x)] == 2
    assert _deletes(calls) == sorted([str(member_m), str(switch_x)])
    alloc_a = await _allocation_of(res_a)
    assert (alloc_a.status, alloc_a.defined_switch_ids) == ("RELEASED", [str(switch_x)])
    alloc_c = await _allocation_of(res_c)
    assert (alloc_c.status, alloc_c.defined_switch_ids) == ("RELEASED", [])
    assert (await _snapshot("l2"))[a_id][0] == "RELEASED"


async def test_a_failed_intent_read_does_not_stop_the_same_reservations_release_tail():
    """One reservation, A: a FAILED build row on switch X and a FAILED release row on
    member switch M, both on A's allocation, which is defined on M and X. The tick's
    build-intent check reads X first and it blips: A's build row is left FAILED for the
    tick. A's release row is still applied, and the release tail of that apply (a new
    release pass) reads X again, so both deletes run and the allocation's
    defined_switch_ids is empty. Inside a single apply a failed read escapes the apply
    before its tail runs, so this is the reachable form of "an earlier failure for the
    same reservation"."""
    res_a = uuid.uuid4()
    member_m, switch_x = uuid.uuid4(), uuid.uuid4()
    (release_id,) = await _seed_release_rows("l2", res_a, [member_m], created_at=OLD)
    async with TestSessionLocal() as s:
        alloc = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.reservation_id == res_a))
        ).scalar_one()
        alloc.defined_switch_ids = [str(member_m), str(switch_x)]
        build = L2PortAssignment(
            reservation_id=res_a,
            switch_device_id=switch_x,
            port="p9",
            vlan_assignment_id=alloc.id,
            status="FAILED",
            intended="ACTIVE",
            attempts=1,
            last_error="driver timeout",
            created_at=OLD + timedelta(minutes=1),
        )
        s.add(build)
        await s.commit()
        build_id = build.id
    wires = [
        {
            "device_a_id": "dut-1",
            "port_a": "eth0",
            "device_b_id": str(switch_x),
            "port_b": "p9",
            "layer": "L1",
        }
    ]
    before = await _snapshot("l2")

    with (
        _blip({switch_x}) as (reads, calls),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=wires),
        ),
    ):
        await run_wiring_retry_tick(_db_session_factory())

    assert reads[str(switch_x)] == 2, "the intent check read X, then the tail read it again"
    assert _deletes(calls) == sorted([str(member_m), str(switch_x)])
    after = await _snapshot("l2")
    assert after[build_id] == before[build_id], "the build row stays FAILED for the tick"
    assert after[release_id][0] == "RELEASED"
    alloc_a = await _allocation_of(res_a)
    assert (alloc_a.status, alloc_a.defined_switch_ids) == ("RELEASED", [])


# --- Controls: the catch is no wider than it says --------------------------


@pytest.mark.parametrize("layer", LAYERS)
async def test_an_exception_other_than_transient_upstream_still_drops_the_layer(layer):
    """Only TransientUpstreamError is caught per reservation. Any other exception from
    one reservation's apply still escapes the layer retry, the tick's layer catch drops
    the layer (WIRE-RETRY-15), and so B is not driven this tick. A catch widened to
    Exception would drive B and fails here."""
    res_a, res_b = uuid.uuid4(), uuid.uuid4()
    switches_a = _a_switches(layer, 2)
    await _seed_release_rows(layer, res_a, switches_a, created_at=OLD)
    (b_id,) = await _seed_release_rows(layer, res_b, [GOOD_SWITCH], created_at=NEWER, vlan_id=1002)

    with _upstream(layer, set(switches_a), exc_type=RuntimeError) as calls:
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert RELEASE_ACTIONS[layer] not in calls
    assert (await _snapshot(layer))[b_id][0] == "FAILED"
    assert stats[f"{layer}_rows_retried"] == 0
    assert stats["skipped_upstream"] == 0


@pytest.mark.parametrize("layer", LAYERS)
async def test_manual_retry_still_propagates_the_upstream_error(layer):
    """The manual channel drives one reservation and passes no collector, so the
    error still propagates and its endpoint answers 503 (WIRE-RETRY-12); the rows stay
    as they were."""
    res_a = uuid.uuid4()
    switches_a = _a_switches(layer, 2)
    a_ids = await _seed_release_rows(layer, res_a, switches_a, created_at=OLD)
    before = await _snapshot(layer)

    with _upstream(layer, set(switches_a)):
        with pytest.raises(TransientUpstreamError):
            await reattempt_reservation(res_a, _db_session_factory())

    after = await _snapshot(layer)
    assert {i: after[i] for i in a_ids} == {i: before[i] for i in a_ids}
