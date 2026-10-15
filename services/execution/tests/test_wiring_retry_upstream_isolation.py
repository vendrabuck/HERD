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
    reattempt_reservation,
    run_wiring_retry_tick,
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
