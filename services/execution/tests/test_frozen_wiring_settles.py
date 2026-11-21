"""A frozen reservation's wiring settles on every layer (issue #1501, pieces 1 and 2).

Once a reservation ends, its wiring is frozen and torn down, and the retry tick drives
only release-direction rows. A row FAILED with intended ACTIVE is then driven by no
pass. Two changes close the two ways such a row was left behind on L1 and L2, and the
late-write way on L3:

  - the terminal teardown parks every FAILED intended-ACTIVE L1 and L2 row (the L3 pin
    park of #1001) intended RELEASED and removes it with the ACTIVE rows; on L2 a nil
    or dangling allocation is released with no driver call; pinned-reason rows are
    left as the reconcile's stale settlement leaves them;
  - a key-based BUILD failure written on a frozen reservation (the reconcile's, from a
    pass that passed its frozen gate before the freeze) records intended RELEASED and
    attempts 0 in its own UPDATE or INSERT, the state the teardown's park leaves, so
    the next tick removes whatever the build may have left.

The invariant these tests enumerate: after the freeze, the teardown, and a tick, every
row of the reservation is RELEASED unless its reason is pinned (non-retryable by
decision, WIRE-RETRY-4); nothing is configured under a RELEASED row that a pass
planned; and other reservations' rows are untouched. In-memory SQLite; the Postgres
staging of the late write is tests/test_frozen_wiring_settles_live_pg.py.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.l1_assignment_service import TEARDOWN_PENDING_DISCONNECT
from app.services.l2_membership_service import TEARDOWN_PENDING_VLAN_REMOVAL
from app.services.nats_consumer import (
    WIRING_UNRESOLVABLE_REASON,
    _FetchContext,
    _teardown_from_ledgers,
    handle_reservation_event,
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
# Another reservation, frozen, with rows of its own: a frozen read that is not scoped
# to the row's own reservation, or a teardown that is not, shows up against it.
FOREIGN = uuid.uuid4()
SWITCH = {"l1": uuid.uuid4(), "l2": uuid.uuid4(), "l3": uuid.uuid4()}
CONNECTION_TYPE = {"l1": "Layer 1 Switch", "l2": "Layer 2 Switch", "l3": "Layer 3 Switch"}
MODEL = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}
PORT_A, PORT_B = "0/0/1", "0/0/2"
L2_PORT = "0/0/9"
VLAN = 100
ROUTE = {"destination": "10.9.0.0/24", "next_hop": "10.9.0.1"}
NIL = uuid.UUID(int=0)
SUCCESS = {"success": True, "output": {}, "error": None, "duration_ms": 1}
FAILURE = {"success": False, "output": None, "error": "boom", "duration_ms": 1}
TEMPLATE = {"id": "t", "name": "t", "sections": []}
DRIVER_ID = str(uuid.uuid4())
RELEASE_ACTION = {"l1": "disconnect_ports", "l2": "remove_from_vlan", "l3": "remove_route"}
BUILD_ACTION = {"l1": "connect_ports", "l2": "add_to_vlan", "l3": "configure_route"}
TEARDOWN_REASON = {"l1": TEARDOWN_PENDING_DISCONNECT, "l2": TEARDOWN_PENDING_VLAN_REMOVAL}


async def _device(device_id, client=None):
    for layer, switch in SWITCH.items():
        if str(device_id) == str(switch):
            return {
                "id": str(switch),
                "name": layer,
                "template_id": "t",
                "driver_id": DRIVER_ID,
                "driver_sha256": "sha",
                "driver_filename": "d.zip",
                "connection_type": CONNECTION_TYPE[layer],
                "field_data": {},
            }
    return None


def _recorder(fail: set[str] | None = None, on_call=None):
    fail = fail or set()
    calls: list[str] = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if on_call is not None:
            on_call(action)
        return FAILURE if action in fail else SUCCESS

    return execute_fn, calls


def _patches(execute_fn, stack: ExitStack) -> None:
    for p in (
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE)),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/d")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch("app.services.nats_consumer.apply_reservation_event_tiers", new=AsyncMock()),
        patch("app.services.nats_consumer._execute_dynamic_teardown", new=AsyncMock()),
    ):
        stack.enter_context(p)


async def _freeze(reservation=RES) -> None:
    from app.services.l1_assignment_service import freeze_reservation_wiring

    async with TestSessionLocal() as s:
        await freeze_reservation_wiring(s, reservation)


async def _allocation(reservation=RES, vlan=VLAN) -> uuid.UUID:
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=reservation,
            fabric_id=uuid.uuid4(),
            vlan_id=vlan,
            switch_device_ids=[str(SWITCH["l2"])],
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        return va.id


async def _seed(
    layer: str,
    status: str,
    intended: str,
    *,
    reservation=RES,
    last_error: str | None = "driver timeout",
    attempts: int = 3,
    allocation: uuid.UUID | None = None,
    port: str | None = None,
) -> uuid.UUID:
    common = {
        "reservation_id": reservation,
        "status": status,
        "intended": intended,
        "attempts": attempts,
        "last_error": last_error,
    }
    if layer == "l1":
        # Stored canonical (port_a sorts first), as every writer stores a pair.
        row = L1ConnectionAssignment(
            switch_device_id=SWITCH["l1"],
            port_a=port or PORT_A,
            port_b=f"{port}b" if port else PORT_B,
            **common,
        )
    elif layer == "l2":
        if allocation is None:
            allocation = await _allocation(reservation)
        row = L2PortAssignment(
            switch_device_id=SWITCH["l2"],
            port=port or L2_PORT,
            vlan_assignment_id=allocation,
            **common,
        )
    else:
        row = RouteAssignment(device_id=SWITCH["l3"], routes=[ROUTE], **common)
    async with TestSessionLocal() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _row(layer: str, row_id) -> tuple:
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        row = (await s.execute(select(model).where(model.id == row_id))).scalar_one()
        return (row.status, row.intended, row.attempts, row.last_error)


async def _rows(layer: str, reservation=RES) -> list:
    model = MODEL[layer]
    async with TestSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(model)
                    .where(model.reservation_id == reservation)
                    .order_by(model.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [(r.id, r.status, r.intended, r.attempts, r.last_error) for r in rows]


async def _foreign_snapshot() -> list:
    out = []
    for layer in MODEL:
        out.extend(await _rows(layer, FOREIGN))
    async with TestSessionLocal() as s:
        out.extend(
            (v.id, v.status)
            for v in (
                await s.execute(
                    select(VlanAssignment).where(VlanAssignment.reservation_id == FOREIGN)
                )
            ).scalars()
        )
    return sorted(out, key=str)


async def _seed_foreign() -> None:
    """A used ledger: another frozen reservation's FAILED build rows on every layer."""
    await _freeze(FOREIGN)
    for layer in MODEL:
        await _seed(layer, "FAILED", "ACTIVE", reservation=FOREIGN, port="0/0/40")


async def _terminal_event(execute_fn) -> None:
    with ExitStack() as stack:
        _patches(execute_fn, stack)
        await handle_reservation_event(
            {"event": "reservation.cancelled", "reservation_id": str(RES), "user_id": "u"},
            _db_session_factory(),
        )


async def _teardown(execute_fn) -> None:
    with ExitStack() as stack:
        _patches(execute_fn, stack)
        await _teardown_from_ledgers(str(RES), _FetchContext(None), _db_session_factory())


async def _tick(execute_fn) -> dict:
    from app.services.wiring_retry_service import run_wiring_retry_tick

    with ExitStack() as stack:
        _patches(execute_fn, stack)
        return await run_wiring_retry_tick(_db_session_factory())


async def _key_based_build_failure(layer: str, *, error="driver timeout", allocation=None):
    """The reconcile's failure write for a build: by key, no row id, no ledger."""
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    async with TestSessionLocal() as s:
        if layer == "l1":
            return await record_l1_failed(
                s, RES, SWITCH["l1"], PORT_A, PORT_B, 3, error, intended="ACTIVE"
            )
        if layer == "l2":
            return await record_l2_failed(
                s, RES, allocation, SWITCH["l2"], L2_PORT, 3, error, intended="ACTIVE"
            )
        return await record_route_failed(
            s, RES, str(SWITCH["l3"]), [ROUTE], 3, error, intended="ACTIVE"
        )


# --- Piece 1: the teardown releases a FAILED intended-ACTIVE L1 or L2 row -----------


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_teardown_releases_a_failed_build_row(layer):
    """A row FAILED intended ACTIVE at the freeze (a connect or join that did not
    confirm, which may have landed) is removed by the terminal teardown and recorded
    RELEASED; on L2 its emptied allocation is released too. The other frozen
    reservation's FAILED build rows are untouched."""
    await _seed_foreign()
    foreign_before = await _foreign_snapshot()
    row_id = await _seed(layer, "FAILED", "ACTIVE")

    execute_fn, calls = _recorder()
    await _terminal_event(execute_fn)

    assert calls == ["login", RELEASE_ACTION[layer], "logout"]
    status, intended, _attempts, last_error = await _row(layer, row_id)
    assert (status, intended, last_error) == ("RELEASED", "RELEASED", None)
    if layer == "l2":
        async with TestSessionLocal() as s:
            alloc = (
                await s.execute(select(VlanAssignment).where(VlanAssignment.reservation_id == RES))
            ).scalar_one()
        assert alloc.status == "RELEASED"
    assert await _foreign_snapshot() == foreign_before


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_failed_teardown_removal_counts_only_its_own_attempts(layer):
    """The park resets the build's attempts to 0 (the #479 rationale), so a removal
    that fails records only its own three: the row stays well under the tick's cap."""
    row_id = await _seed(layer, "FAILED", "ACTIVE", attempts=9)
    execute_fn, _calls = _recorder(fail={RELEASE_ACTION[layer]})
    await _teardown(execute_fn)
    assert (await _row(layer, row_id))[:3] == ("FAILED", "RELEASED", 3)


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_teardown_park_survives_an_undrivable_switch(layer):
    """With the switch's driver load failing, the removal is recorded as a failed
    release on the parked row: FAILED intended RELEASED, never intended ACTIVE again."""
    row_id = await _seed(layer, "FAILED", "ACTIVE")
    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _patches(execute_fn, stack)
        stack.enter_context(
            patch(
                "app.services.driver_loader.load_driver",
                new=AsyncMock(side_effect=RuntimeError("load")),
            )
        )
        await _teardown_from_ledgers(str(RES), _FetchContext(None), _db_session_factory())
    assert calls == []
    status, intended, _attempts, last_error = await _row(layer, row_id)
    assert (status, intended) == ("FAILED", "RELEASED")
    assert last_error is not None and "RuntimeError" in last_error


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_teardown_park_before_the_driver_call_is_durable(layer):
    """Read from a second session inside the removal's driver call: the row is already
    parked (FAILED, intended RELEASED, attempts 0, the teardown reason)."""
    row_id = await _seed(layer, "FAILED", "ACTIVE")
    observed: dict = {}

    async def _driver(driver_path, action, context, password_keys, method_kwargs=None):
        if action == RELEASE_ACTION[layer]:
            observed["row"] = await _row(layer, row_id)
        return True, 1, None, SUCCESS

    execute_fn, _calls = _recorder()
    with ExitStack() as stack:
        _patches(execute_fn, stack)
        stack.enter_context(
            patch("app.services.nats_consumer._run_driver_with_retry", side_effect=_driver)
        )
        await _teardown_from_ledgers(str(RES), _FetchContext(None), _db_session_factory())
    assert observed["row"] == ("FAILED", "RELEASED", 0, TEARDOWN_REASON[layer])
    assert (await _row(layer, row_id))[0] == "RELEASED"


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_failed_teardown_removal_is_finished_by_the_tick(layer):
    """The removal fails: the row is FAILED intended RELEASED, which the tick selects on
    a frozen reservation and releases."""
    row_id = await _seed(layer, "FAILED", "ACTIVE")
    failing, _ = _recorder(fail={RELEASE_ACTION[layer]})
    await _terminal_event(failing)
    assert (await _row(layer, row_id))[:2] == ("FAILED", "RELEASED")

    execute_fn, calls = _recorder()
    stats = await _tick(execute_fn)
    assert calls == ["login", RELEASE_ACTION[layer], "logout"]
    assert stats["released"] == 1
    assert (await _row(layer, row_id))[:2] == ("RELEASED", "RELEASED")


@pytest.mark.parametrize("removal_fails", [False, True], ids=["settled", "removal_failed"])
@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_redelivered_teardown_is_a_no_op(layer, removal_fails):
    """A redelivered terminal event makes no driver call for a row the first teardown
    parked: a RELEASED row is not read, and a FAILED intended-RELEASED row is left to
    the retry channels; neither changes."""
    row_id = await _seed(layer, "FAILED", "ACTIVE")
    first, _ = _recorder(fail={RELEASE_ACTION[layer]} if removal_fails else None)
    await _terminal_event(first)
    before = await _row(layer, row_id)

    again, calls = _recorder()
    await _terminal_event(again)
    assert calls == []
    assert await _row(layer, row_id) == before


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_teardown_leaves_a_pinned_reason_build_row_alone(layer):
    """A pinned-reason build row (WIRE-STALE-1, WIRE-RETRY-4) is not parked or driven:
    on L1 it is keyed on raw hop endpoints, and its recovery is a re-save."""
    reason = f"{WIRING_UNRESOLVABLE_REASON}: switch gone"
    row_id = await _seed(layer, "FAILED", "ACTIVE", last_error=reason)
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)
    assert calls == []
    assert await _row(layer, row_id) == ("FAILED", "ACTIVE", 3, reason)


@pytest.mark.parametrize("allocation", ["nil", "dangling"])
async def test_teardown_releases_an_unresolvable_allocation_join_without_a_driver_call(
    allocation,
):
    """An L2 build row whose allocation is the nil placeholder or no longer exists has no
    VLAN number to remove and never reached add_to_vlan: released with no driver call,
    whatever its reason (WIRE-STALE-2's rule)."""
    alloc = NIL if allocation == "nil" else uuid.uuid4()
    reason = f"{WIRING_UNRESOLVABLE_REASON}: no VLAN allocation for fabric"
    row_id = await _seed("l2", "FAILED", "ACTIVE", allocation=alloc, last_error=reason)
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)
    assert calls == []
    assert (await _row("l2", row_id))[:2] == ("RELEASED", "RELEASED")


async def test_teardown_removes_a_failed_and_an_active_l1_row_in_one_login():
    """The parked row joins the ACTIVE rows' release set on its switch."""
    active_id = await _seed("l1", "ACTIVE", "ACTIVE", port="0/0/3", last_error=None)
    failed_id = await _seed("l1", "FAILED", "ACTIVE")
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)
    assert calls == ["login", "disconnect_ports", "disconnect_ports", "logout"]
    assert (await _row("l1", active_id))[0] == "RELEASED"
    assert (await _row("l1", failed_id))[0] == "RELEASED"


# --- Piece 2: a key-based build failure after the freeze is a pending release -------


@pytest.mark.parametrize("branch", ["update", "insert"])
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_key_based_build_failure_after_the_freeze_records_a_pending_release(layer, branch):
    """The write reads the freeze in its own statement and records the state the
    teardown's park leaves: FAILED intended RELEASED, attempts 0, its own reason."""
    allocation = await _allocation() if layer == "l2" else None
    if branch == "update":
        await _seed(layer, "FAILED", "ACTIVE", allocation=allocation)
    await _freeze()

    written = await _key_based_build_failure(layer, allocation=allocation)

    assert (written.status, written.intended, written.attempts, written.last_error) == (
        "FAILED",
        "RELEASED",
        0,
        "driver timeout",
    )
    assert [r[1:] for r in await _rows(layer)] == [("FAILED", "RELEASED", 0, "driver timeout")]


@pytest.mark.parametrize("branch", ["update", "insert"])
@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_key_based_build_failure_on_a_reservation_not_frozen_is_unchanged(layer, branch):
    """Not frozen, while ANOTHER reservation is frozen: an ordinary build failure,
    intended ACTIVE with the attempts added (the frozen read is the row's own
    reservation's, never any frozen row)."""
    await _seed_foreign()
    foreign_before = await _foreign_snapshot()
    allocation = await _allocation() if layer == "l2" else None
    if branch == "update":
        await _seed(layer, "FAILED", "ACTIVE", allocation=allocation)

    written = await _key_based_build_failure(layer, allocation=allocation)

    expected_attempts = 6 if branch == "update" else 3
    assert (written.status, written.intended, written.attempts) == (
        "FAILED",
        "ACTIVE",
        expected_attempts,
    )
    assert await _foreign_snapshot() == foreign_before


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_key_based_release_failure_after_the_freeze_is_unchanged(layer):
    """Only builds are recorded differently: a release failure adds its attempts."""
    from app.services.l1_assignment_service import record_l1_failed
    from app.services.l2_membership_service import record_l2_failed
    from app.services.route_service import record_route_failed

    row_id = await _seed(layer, "ACTIVE", "ACTIVE", attempts=0, last_error=None)
    await _freeze()
    async with TestSessionLocal() as s:
        if layer == "l1":
            await record_l1_failed(
                s, RES, SWITCH["l1"], PORT_A, PORT_B, 3, "boom", intended="RELEASED"
            )
        elif layer == "l2":
            await record_l2_failed(
                s, RES, None, SWITCH["l2"], L2_PORT, 3, "boom", intended="RELEASED"
            )
        else:
            await record_route_failed(
                s, RES, str(SWITCH["l3"]), None, 3, "boom", intended="RELEASED"
            )
    assert await _row(layer, row_id) == ("FAILED", "RELEASED", 3, "boom")


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_the_412_guard_still_holds_after_the_freeze(layer):
    """A build failure never changes an ACTIVE row (WIRE-LEDGER-7), frozen or not."""
    row_id = await _seed(layer, "ACTIVE", "ACTIVE", attempts=0, last_error=None)
    await _freeze()
    await _key_based_build_failure(layer)
    assert await _row(layer, row_id) == ("ACTIVE", "ACTIVE", 0, None)


@pytest.mark.parametrize("layer", ["l1", "l2"])
async def test_a_pinned_reason_build_failure_after_the_freeze_is_unchanged(layer):
    """On L1 and L2 the rule covers exactly the rows the teardown parks: a pinned
    reason is recorded as before."""
    allocation = await _allocation() if layer == "l2" else None
    await _freeze()
    reason = f"{WIRING_UNRESOLVABLE_REASON}: switch gone"
    written = await _key_based_build_failure(layer, error=reason, allocation=allocation)
    assert (written.intended, written.attempts) == ("ACTIVE", 3)


async def test_an_l3_pinned_reason_build_failure_after_the_freeze_is_a_pending_release():
    """On L3 the teardown parks every FAILED intended-ACTIVE pin, so the write does too."""
    await _freeze()
    written = await _key_based_build_failure("l3", error=f"{WIRING_UNRESOLVABLE_REASON}: gone")
    assert (written.intended, written.attempts) == ("RELEASED", 0)


@pytest.mark.parametrize("branch", ["update", "insert"])
async def test_an_l2_nil_allocation_build_failure_after_the_freeze_is_unchanged(branch):
    """A nil allocation has no VLAN a removal could drive; the teardown releases such a
    row directly, and the write leaves it a build as before."""
    if branch == "update":
        await _seed("l2", "FAILED", "ACTIVE", allocation=NIL)
    await _freeze()
    written = await _key_based_build_failure("l2", allocation=None)
    assert (written.intended, written.vlan_assignment_id) == ("ACTIVE", NIL)


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_late_build_failure_does_not_undo_a_teardown_park(layer):
    """The teardown parked the row and its removal failed (FAILED intended RELEASED).
    A late key-based build failure for the same key keeps it intended RELEASED; before
    #1501 it flipped the row to intended ACTIVE, which no pass drives."""
    allocation = await _allocation() if layer == "l2" else None
    row_id = await _seed(layer, "FAILED", "RELEASED", allocation=allocation)
    await _freeze()
    await _key_based_build_failure(layer, allocation=allocation)
    assert (await _row(layer, row_id))[:3] == ("FAILED", "RELEASED", 0)


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_late_build_failure_never_rewrites_a_released_row(layer):
    """The row was released. A late key-based build failure (its driver call may have
    landed) leaves that row RELEASED and records the build in a new row as a pending
    release, which the next tick removes."""
    allocation = await _allocation() if layer == "l2" else None
    old_id = await _seed(layer, "RELEASED", "RELEASED", allocation=allocation, last_error=None)
    before = await _row(layer, old_id)
    await _freeze()
    await _key_based_build_failure(layer, allocation=allocation)
    assert await _row(layer, old_id) == before
    rows = await _rows(layer)
    new = [r for r in rows if r[0] != old_id]
    assert [r[1:4] for r in new] == [("FAILED", "RELEASED", 0)]

    execute_fn, calls = _recorder()
    stats = await _tick(execute_fn)
    assert RELEASE_ACTION[layer] in calls
    assert stats["released"] == 1
    assert [r[1] for r in await _rows(layer)] == ["RELEASED", "RELEASED"]


async def test_a_late_reconcile_delta_failure_after_the_freeze_is_a_pending_release():
    """`record_route_reconcile_failed` (an intent-delta failure on an ACTIVE pin) follows
    the same rule: on a frozen reservation the pin is FAILED intended RELEASED, attempts
    0, with the routes the delta may have left, which the next tick removes."""
    from app.services.route_service import record_route_reconcile_failed

    r2 = {"destination": "10.8.0.0/24", "next_hop": "10.8.0.1"}
    row_id = await _seed("l3", "ACTIVE", "ACTIVE", attempts=0, last_error=None)
    await _freeze()
    async with TestSessionLocal() as s:
        written = await record_route_reconcile_failed(
            s, RES, str(SWITCH["l3"]), 3, "boom", [ROUTE], possibly_installed=[ROUTE, r2]
        )
    assert (written.status, written.intended, written.attempts) == ("FAILED", "RELEASED", 0)

    removed: list = []

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "remove_route":
            removed.append((kwargs.get("method_kwargs") or {}).get("destination"))
        return SUCCESS

    with ExitStack() as stack:
        _patches(execute_fn, stack)
        from app.services.wiring_retry_service import run_wiring_retry_tick

        await run_wiring_retry_tick(_db_session_factory())
    assert sorted(removed) == sorted([ROUTE["destination"], r2["destination"]])
    assert (await _row("l3", row_id))[0] == "RELEASED"


async def test_a_reconcile_delta_failure_not_frozen_is_unchanged():
    from app.services.route_service import record_route_reconcile_failed

    await _seed_foreign()
    await _seed("l3", "ACTIVE", "ACTIVE", attempts=1, last_error=None)
    async with TestSessionLocal() as s:
        written = await record_route_reconcile_failed(s, RES, str(SWITCH["l3"]), 3, "boom", [ROUTE])
    assert (written.status, written.intended, written.attempts) == ("FAILED", "ACTIVE", 4)


# --- The issue's interleaving through the real applies -----------------------------


async def _reconcile_build_that_fails(layer: str, allocation=None) -> None:
    """A reconcile pass that passed its frozen gate before the freeze and whose build
    fails after the teardown ran: the shared apply's key-based failure write."""
    from app.services.nats_consumer import (
        _apply_l2_memberships,
        _apply_l3_adjacency,
        _apply_wiring_pairs,
    )

    failing, calls = _recorder(fail={BUILD_ACTION[layer]})
    with ExitStack() as stack:
        _patches(failing, stack)
        ctx = _FetchContext(None)
        if layer == "l1":
            await _apply_wiring_pairs(
                str(RES),
                {},
                {str(SWITCH["l1"]): [(PORT_A, PORT_B, None)]},
                [],
                ctx,
                _db_session_factory(),
            )
        elif layer == "l2":
            add = {
                "switch_device_id": str(SWITCH["l2"]),
                "port": L2_PORT,
                "vlan_assignment_id": allocation,
                "vlan_id": VLAN,
            }
            await _apply_l2_memberships(str(RES), [], [add], ctx, _db_session_factory())
        else:
            await _apply_l3_adjacency(
                str(RES),
                [],
                [{"device_id": str(SWITCH["l3"]), "routes": [ROUTE]}],
                ctx,
                _db_session_factory(),
            )
    assert BUILD_ACTION[layer] in calls


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_build_failure_landing_after_the_teardown_is_released_by_the_next_tick(layer):
    """The issue's case 2 on every layer: the reservation ends (freeze, teardown with
    nothing to release), then a reconcile's build fails and records by key. The row is
    a pending release, and the next tick removes it: every row ends RELEASED. Before
    #1501 the row was FAILED intended ACTIVE and the tick left it out as a frozen build."""
    await _seed_foreign()
    foreign_before = await _foreign_snapshot()
    allocation = await _allocation() if layer == "l2" else None
    nothing, calls = _recorder()
    await _terminal_event(nothing)
    assert calls == []

    await _reconcile_build_that_fails(layer, allocation)
    assert [r[1:4] for r in await _rows(layer)] == [("FAILED", "RELEASED", 0)]

    execute_fn, calls = _recorder()
    stats = await _tick(execute_fn)
    assert RELEASE_ACTION[layer] in calls
    assert stats["released"] == 1
    assert [r[1:3] for r in await _rows(layer)] == [("RELEASED", "RELEASED")]
    assert await _foreign_snapshot() == foreign_before


async def test_after_the_teardown_and_a_tick_every_row_is_released():
    """The invariant over a mixed ledger: ACTIVE rows, FAILED build rows (L1, L2, L3),
    and a FAILED release row on every layer, plus a late build failure on L1; one
    teardown whose L1 removal of the failed build fails, then one tick. Every row of
    the reservation is RELEASED, every L2 allocation released, and the other
    reservation's rows are untouched."""
    await _seed_foreign()
    foreign_before = await _foreign_snapshot()
    await _seed("l1", "ACTIVE", "ACTIVE", port="0/0/11", last_error=None)
    failed_build = await _seed("l1", "FAILED", "ACTIVE", port="0/0/12")
    await _seed("l1", "FAILED", "RELEASED", port="0/0/13")
    await _seed("l2", "ACTIVE", "ACTIVE", port="0/0/21", last_error=None)
    await _seed("l2", "FAILED", "ACTIVE", port="0/0/22")
    await _seed("l2", "FAILED", "RELEASED", port="0/0/23")
    await _seed("l3", "FAILED", "ACTIVE")

    def execute_fn(driver_path, action, context, **kwargs):
        if (
            action == "disconnect_ports"
            and (kwargs.get("method_kwargs") or {}).get("port_a") == "0/0/12"
        ):
            return FAILURE
        return SUCCESS

    await _terminal_event(execute_fn)
    assert (await _row("l1", failed_build))[:2] == ("FAILED", "RELEASED")
    await _key_based_build_failure("l1")
    unsettled = {
        layer: [r[1:3] for r in await _rows(layer) if r[1] != "RELEASED"] for layer in MODEL
    }
    assert unsettled == {
        "l1": [("FAILED", "RELEASED")] * 3,
        "l2": [("FAILED", "RELEASED")],
        "l3": [],
    }, "after the teardown only release-direction rows remain, for the tick"

    ok, _calls = _recorder()
    await _tick(ok)

    for layer in MODEL:
        assert {r[1] for r in await _rows(layer)} == {"RELEASED"}, layer
    async with TestSessionLocal() as s:
        statuses = {
            v.status
            for v in (
                await s.execute(select(VlanAssignment).where(VlanAssignment.reservation_id == RES))
            ).scalars()
        }
    assert statuses == {"RELEASED"}
    async with TestSessionLocal() as s:
        state = await s.get(ReservationWiringState, RES)
    assert state.frozen is True
    assert await _foreign_snapshot() == foreign_before
