"""Unit tests for the ledger-driven terminal teardown (ADR 0009 phase 6, issue #416).

Terminal transitions (cancelled/completed/failed) release a reservation's applied wiring
from the three ledgers (l1_connection_assignments, l2_port_assignments + vlan_assignments,
route_assignments) instead of resolving teardown work from the device set. The pass reuses
the shared apply functions, so a driver failure lands a FAILED intended-RELEASED row
(retryable, discharging the issue #244 posture), the L2 allocation is freed when its last
membership leaves, and the issue #20 pinned route set is removed verbatim. These tests seed
ACTIVE rows across all three ledgers, drive the terminal path, and assert the release plus
the freeze ordering, mirroring the test_nats_consumer_l2_reconcile.py fixtures.
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
from app.services.nats_consumer import (
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


SW_L1 = str(uuid.uuid4())
SW_L2 = str(uuid.uuid4())
SW_L3 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
FABRIC = uuid.uuid4()

TEMPLATE_DATA = {"id": "tmpl-1", "name": "Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}
ROUTES = [{"destination": "10.9.0.0/24", "next_hop": "192.168.1.1", "interface": "eth1"}]


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
    SW_L1: _dev(SW_L1, "Layer 1 Switch", "L1"),
    SW_L2: _dev(SW_L2, "Layer 2 Switch", "L2"),
    SW_L3: _dev(SW_L3, "Layer 3 Switch", "L3"),
}


async def _device_fetch(device_id, client=None):
    return DEVICES.get(str(device_id))


def _recorder(fail=None):
    """(execute_fn, calls): calls records the driver action; `fail` is a set of actions
    whose sandbox op returns success=False."""
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action in fail:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _driver_patches(execute_fn):
    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]


def _event_patches(execute_fn):
    """Driver patches plus the terminal-path best-effort side effects (health tiers and
    dynamic teardown) mocked out so a full handle_reservation_event stays hermetic."""
    return _driver_patches(execute_fn) + [
        patch(
            "app.services.nats_consumer.apply_reservation_event_tiers",
            new=AsyncMock(),
        ),
        patch(
            "app.services.nats_consumer._execute_dynamic_teardown",
            new=AsyncMock(),
        ),
    ]


async def _seed_l1(port_a="0/0/1", port_b="0/0/2", status="ACTIVE"):
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SW_L1),
                port_a=port_a,
                port_b=port_b,
                status=status,
                intended="ACTIVE",
            )
        )
        await s.commit()


async def _seed_l2(port="0/0/5", vlan_id=100):
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=vlan_id,
            switch_device_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        await s.refresh(va)
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va.id,
                switch_device_id=uuid.UUID(SW_L2),
                port=port,
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()
        return va.id


async def _seed_l3(routes=None):
    async with TestSessionLocal() as s:
        s.add(
            RouteAssignment(
                reservation_id=uuid.UUID(RES_ID),
                device_id=uuid.UUID(SW_L3),
                routes=routes if routes is not None else ROUTES,
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()


async def _statuses(model):
    async with TestSessionLocal() as s:
        rows = (
            (await s.execute(select(model).where(model.reservation_id == uuid.UUID(RES_ID))))
            .scalars()
            .all()
        )
        return [r.status for r in rows]


async def _wiring_state():
    async with TestSessionLocal() as s:
        return (
            await s.execute(
                select(ReservationWiringState).where(
                    ReservationWiringState.reservation_id == uuid.UUID(RES_ID)
                )
            )
        ).scalar_one_or_none()


async def _teardown(execute_fn):
    with ExitStack() as stack:
        for p in _driver_patches(execute_fn):
            stack.enter_context(p)
        ctx = _FetchContext(None)
        await _teardown_from_ledgers(RES_ID, ctx, _db_session_factory())


def _make_event(event_type):
    return {
        "event": event_type,
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": ["dut-1"],
    }


async def _handle(event_type, execute_fn):
    with ExitStack() as stack:
        for p in _event_patches(execute_fn):
            stack.enter_context(p)
        await handle_reservation_event(_make_event(event_type), _db_session_factory())


# --- full three-layer teardown through the terminal handler ---


@pytest.mark.parametrize(
    "event_type", ["reservation.cancelled", "reservation.completed", "reservation.failed"]
)
async def test_terminal_event_releases_all_three_ledgers(event_type):
    """A reservation with ACTIVE rows in all three ledgers releases every row, frees the
    orphaned VLAN allocation, drives the three release driver calls, and freezes."""
    await _seed_l1()
    await _seed_l2()
    await _seed_l3()

    execute_fn, calls = _recorder()
    await _handle(event_type, execute_fn)

    assert await _statuses(L1ConnectionAssignment) == ["RELEASED"]
    assert await _statuses(L2PortAssignment) == ["RELEASED"]
    assert await _statuses(VlanAssignment) == ["RELEASED"], "last membership frees the allocation"
    assert await _statuses(RouteAssignment) == ["RELEASED"]

    assert "disconnect_ports" in calls
    assert "remove_from_vlan" in calls
    assert "remove_route" in calls

    state = await _wiring_state()
    assert state is not None and state.frozen is True


@pytest.mark.parametrize(
    "event_type", ["reservation.cancelled", "reservation.completed", "reservation.failed"]
)
async def test_terminal_event_none_reservation_id_raises(event_type):
    """Pins ACTUAL current behavior (issue #448 item 2): a terminal event with a None
    reservation_id is NOT skipped by any guard. handle_reservation_event has no
    missing-reservation_id check on this path (unlike handle_wiring_changed's explicit
    guard), so `str(None)` flows into _teardown_from_ledgers as the literal string
    "None" and uuid.UUID("None") raises ValueError. This is a deliberate,
    already-accepted poison-handling outcome per the issue: process_reservation_message's
    generic `except Exception` branch NAKs the message for redelivery and only routes it
    to the DLQ once num_delivered reaches max_deliver (it is NOT raised as
    PermanentEventError, so it is not DLQ'd on the first delivery). This test pins the
    handler-level raise; it does not exercise the outer NATS message loop.
    """
    execute_fn, _calls = _recorder()
    event = {
        "event": event_type,
        "reservation_id": None,
        "user_id": USER_ID,
        "device_ids": ["dut-1"],
    }
    with ExitStack() as stack:
        for p in _event_patches(execute_fn):
            stack.enter_context(p)
        with pytest.raises(ValueError):
            await handle_reservation_event(event, _db_session_factory())


async def test_empty_ledgers_noop_without_driver_calls():
    """A reservation with nothing applied tears down with no driver call at all."""
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)
    assert calls == []


async def test_partial_ledger_only_l2_tears_down_cleanly():
    """Only L2 rows present: the membership leaves, its allocation is freed, and neither
    the L1 nor L3 apply is driven (no login even)."""
    await _seed_l2()
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)

    assert await _statuses(L2PortAssignment) == ["RELEASED"]
    assert await _statuses(VlanAssignment) == ["RELEASED"]
    assert "remove_from_vlan" in calls
    assert "disconnect_ports" not in calls
    assert "remove_route" not in calls


async def test_partial_ledger_only_l3_pin_tears_down_cleanly():
    """Only an L3 pin present: remove_route runs and the pin releases; L1/L2 untouched."""
    await _seed_l3()
    execute_fn, calls = _recorder()
    await _teardown(execute_fn)

    assert await _statuses(RouteAssignment) == ["RELEASED"]
    assert "remove_route" in calls
    assert "disconnect_ports" not in calls
    assert "remove_from_vlan" not in calls


async def test_teardown_scoped_to_the_reservation():
    """Another reservation's ACTIVE rows are untouched by this reservation's teardown.

    Re-expresses the retired legacy pin (test_l1_failed_teardown_ignores_other_
    reservations_connects) against the ledger path: the release query is scoped by
    reservation_id, so a shared switch keeps serving its other tenants.
    """
    other_res = uuid.uuid4()
    await _seed_l1()
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=other_res,
                switch_device_id=uuid.UUID(SW_L1),
                port_a="0/0/7",
                port_b="0/0/8",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()

    execute_fn, calls = _recorder()
    await _teardown(execute_fn)

    assert await _statuses(L1ConnectionAssignment) == ["RELEASED"]
    async with TestSessionLocal() as s:
        other_rows = (
            (
                await s.execute(
                    select(L1ConnectionAssignment).where(
                        L1ConnectionAssignment.reservation_id == other_res
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r.status for r in other_rows] == ["ACTIVE"]
    assert calls.count("disconnect_ports") == 1


async def test_redelivered_terminal_event_is_idempotent():
    """A redelivered terminal event tears down nothing more: the first delivery released
    every row, so the second finds empty ACTIVE ledgers and drives zero driver calls.
    Re-expresses the retired legacy pin (test_l1_failed_teardown_redelivery_is_idempotent)
    at the ledger level, where released-state idempotency replaces the dedupe-key guard."""
    await _seed_l1()
    await _seed_l2()
    await _seed_l3()

    execute_fn, calls = _recorder()
    await _handle("reservation.failed", execute_fn)
    first_calls = list(calls)
    assert "disconnect_ports" in first_calls

    await _handle("reservation.failed", execute_fn)
    assert calls == first_calls, "a redelivered terminal event must drive no further teardown"
    assert await _statuses(L1ConnectionAssignment) == ["RELEASED"]
    assert await _statuses(L2PortAssignment) == ["RELEASED"]
    assert await _statuses(RouteAssignment) == ["RELEASED"]


async def test_teardown_upstream_error_naks_and_keeps_rows_active():
    """An UPSTREAM transient (inventory unreachable while resolving the switch) raises
    for a NAK and leaves the ledger rows ACTIVE, so the redelivery retries the teardown.
    Re-expresses the retired legacy keeps-rows-active-on-transient pin at the ledger
    level: a transient is not a driver failure, so no FAILED row is written either."""
    from app.services.nats_consumer import TransientUpstreamError

    await _seed_l1()
    execute_fn, _calls = _recorder()

    with ExitStack() as stack:
        for p in _driver_patches(execute_fn):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.nats_consumer._fetch_device",
                new=AsyncMock(side_effect=TransientUpstreamError("inventory 503")),
            )
        )
        ctx = _FetchContext(None)
        with pytest.raises(TransientUpstreamError):
            await _teardown_from_ledgers(RES_ID, ctx, _db_session_factory())

    assert await _statuses(L1ConnectionAssignment) == ["ACTIVE"]


# --- driver failure lands a retryable FAILED intended-RELEASED row ---


async def test_l1_teardown_driver_failure_lands_failed_intended_released():
    """A disconnect that the driver reports failed lands a FAILED row tagged intended
    RELEASED (not a silently stranded ACTIVE row), and never raises (no NAK)."""
    await _seed_l1()
    execute_fn, _calls = _recorder(fail={"disconnect_ports"})
    await _teardown(execute_fn)

    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L1ConnectionAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "RELEASED"

    # The row is hardware-retryable (a driver failure, not an unresolvable-intent reason).
    from app.services.wiring_retry_service import is_retryable_failure

    assert is_retryable_failure(rows[0].last_error) is True


async def test_l3_teardown_driver_failure_keeps_pin_and_lands_failed_released():
    """A failed remove_route keeps the route pin (issue #20: a pin releases only after
    clean removal) and records it FAILED intended RELEASED for the retry channel."""
    await _seed_l3()
    execute_fn, _calls = _recorder(fail={"remove_route"})
    await _teardown(execute_fn)

    async with TestSessionLocal() as s:
        rows = (await s.execute(select(RouteAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "RELEASED"
    assert rows[0].routes == ROUTES, "the pinned set survives the failure verbatim"


async def test_failed_teardown_row_converges_on_manual_retry():
    """After a failed L1 disconnect lands a FAILED intended-RELEASED row, a manual retry
    with the knob cleared converges it to RELEASED, exercising the phase-3 direction-scoped
    retry on the ledger-driven teardown path."""
    await _seed_l1()
    execute_fn, _calls = _recorder(fail={"disconnect_ports"})
    await _teardown(execute_fn)
    # Freeze the reservation (as the terminal handler does): a RELEASE-direction row must
    # still retry while frozen (phase 3).
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=True))
        await s.commit()

    ok_fn, _c = _recorder()  # knob cleared: disconnect now succeeds
    from app.services.wiring_retry_service import reattempt_reservation

    with ExitStack() as stack:
        for p in _driver_patches(ok_fn):
            stack.enter_context(p)
        result = await reattempt_reservation(RES_ID, _db_session_factory())

    outcomes = {o["outcome"] for o in result["results"]}
    assert "released" in outcomes
    assert await _statuses(L1ConnectionAssignment) == ["RELEASED"]


# --- action_succeeded_for_reservation is retired (ADR 0009 phase 7 tombstone) ---


def test_action_succeeded_for_reservation_is_removed():
    """The legacy applied-state helper had no live caller after the ledger-driven
    teardown landed, so ADR 0009 phase 7 deleted it. The ledger teardown reads the
    ACTIVE ledgers directly (the ACTIVE row IS the applied set); pin its absence so a
    reintroduction is caught."""
    from app.services import execution_service

    assert not hasattr(execution_service, "action_succeeded_for_reservation")


# --- freeze ordering ---


async def test_teardown_runs_before_freeze_and_frozen_is_set_last():
    """The terminal handler releases the ledgers BEFORE it sets frozen: at freeze time the
    L1 row is already RELEASED. Pins the pre-phase-6 ordering (teardown, then freeze)."""
    await _seed_l1()
    seen_status = {}

    from app.services import l1_assignment_service as l1svc

    real_freeze = l1svc.freeze_reservation_wiring

    async def _spy_freeze(db, reservation_id):
        async with TestSessionLocal() as s:
            rows = (await s.execute(select(L1ConnectionAssignment))).scalars().all()
            seen_status["at_freeze"] = [r.status for r in rows]
        return await real_freeze(db, reservation_id)

    execute_fn, _calls = _recorder()
    with ExitStack() as stack:
        for p in _event_patches(execute_fn):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.l1_assignment_service.freeze_reservation_wiring",
                new=AsyncMock(side_effect=_spy_freeze),
            )
        )
        await handle_reservation_event(_make_event("reservation.cancelled"), _db_session_factory())

    assert seen_status["at_freeze"] == ["RELEASED"], "teardown released the row before freezing"


async def test_teardown_not_blocked_by_preexisting_frozen():
    """The teardown pass is NOT gated by the frozen flag: even if a reservation is already
    frozen (a redelivery or a race), its ACTIVE ledger rows still release. The freeze gates
    late wiring_changed events and build retries, not the terminal teardown itself."""
    await _seed_l1()
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=True))
        await s.commit()

    execute_fn, calls = _recorder()
    await _teardown(execute_fn)

    assert await _statuses(L1ConnectionAssignment) == ["RELEASED"]
    assert "disconnect_ports" in calls
