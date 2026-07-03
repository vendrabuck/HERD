"""Tests for the reservation.failed teardown paths in nats_consumer.py (issue #244).

The invariant under test throughout: reservation.failed tears down exactly the
provisioning that landed before the failure, and nothing else. L1 disconnects
only pairs with a SUCCESS connect_ports run for the reservation; L2 removes
only what the stored ACTIVE vlan_assignments record (no derived-VLAN fallback)
and releases the rows only after the switches were driven; L3 needs no special
handling because deprovision is already pinned-set-driven.
"""

import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import (
    EVENT_ACTIONS,
    L2_EVENT_ACTIONS,
    L3_EVENT_ACTIONS,
    TransientUpstreamError,
    _derive_vlan_id,
    _execute_l2_switch_operations,
    _execute_switch_operations,
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
    """get_db_session() must directly return an async context manager."""

    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


SWITCH_ID = str(uuid.uuid4())
OTHER_SWITCH_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
CREATED_KEY = "HERD_RESERVATIONS:41"
FAILED_KEY = "HERD_RESERVATIONS:42"

SWITCH_DATA = {
    "id": SWITCH_ID,
    "name": "L1-Switch",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip": "10.0.0.1"},
}

TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "Switch Template",
    "sections": [{"name": "Net", "fields": [{"key": "ip", "type": "string"}]}],
}

SUCCESS_RESULT = {
    "success": True,
    "output": {"result": True},
    "error": None,
    "duration_ms": 50,
}

L1_PAIRS = [
    {"switch_device_id": SWITCH_ID, "switch_port_a": "p1", "switch_port_b": "p2"},
    {"switch_device_id": SWITCH_ID, "switch_port_a": "p3", "switch_port_b": "p4"},
]


def _recording_execute():
    """An execute_driver_method stub that records each (action, kwargs) it runs."""
    calls: list[tuple[str, dict]] = []

    def _execute(driver_path, action, context, **kwargs):
        calls.append((action, kwargs.get("method_kwargs") or {}))
        return SUCCESS_RESULT

    return calls, _execute


def _l1_patches(execute_fn, resolver_return=None):
    """Patches for driving _execute_switch_operations without real HTTP."""
    if resolver_return is None:
        resolver_return = L1_PAIRS
    return [
        patch(
            "app.services.nats_consumer._resolve_l1_switch_operations",
            new=AsyncMock(return_value=resolver_return),
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value=SWITCH_DATA),
        ),
        patch(
            "app.services.nats_consumer._fetch_template",
            new=AsyncMock(return_value=TEMPLATE_DATA),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]


def _l2_patches(execute_fn, resolver_return):
    """Patches for driving _execute_l2_switch_operations without real HTTP."""
    return [
        patch(
            "app.services.nats_consumer._resolve_l2_switch_operations",
            new=AsyncMock(return_value=resolver_return),
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value={**SWITCH_DATA, "connection_type": "Layer 2 Switch"}),
        ),
        patch(
            "app.services.nats_consumer._fetch_template",
            new=AsyncMock(return_value=TEMPLATE_DATA),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]


async def _seed_success_run(
    action, port_a=None, port_b=None, *, dedupe_key, device_id, reservation_id=RES_ID
):
    """Persist a SUCCESS ExecutionRun standing in for a prior, acked delivery."""
    async with TestSessionLocal() as session:
        session.add(
            ExecutionRun(
                device_id=uuid.UUID(device_id),
                driver_id=uuid.UUID(DRIVER_ID),
                driver_sha256="sha256abc",
                action=action,
                status="SUCCESS",
                reservation_id=uuid.UUID(reservation_id),
                user_id=uuid.UUID(USER_ID),
                input_params={},
                port_a=port_a,
                port_b=port_b,
                dedupe_key=dedupe_key,
            )
        )
        await session.commit()


async def _seed_vlan_assignment(vlan_id, switch_ids, *, status="ACTIVE"):
    async with TestSessionLocal() as session:
        session.add(
            VlanAssignment(
                reservation_id=uuid.UUID(RES_ID),
                fabric_id=uuid.uuid4(),
                vlan_id=vlan_id,
                switch_device_ids=switch_ids,
                status=status,
            )
        )
        await session.commit()


async def _vlan_assignment_statuses():
    async with TestSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(VlanAssignment).where(VlanAssignment.reservation_id == uuid.UUID(RES_ID))
                )
            )
            .scalars()
            .all()
        )
        return [r.status for r in rows]


# --- mappings ---


def test_reservation_failed_in_all_three_event_maps():
    assert EVENT_ACTIONS["reservation.failed"] == "disconnect_ports"
    assert L2_EVENT_ACTIONS["reservation.failed"] == "deprovision"
    assert L3_EVENT_ACTIONS["reservation.failed"] == "deprovision"


# --- L1 applied-state guard ---


async def test_l1_failed_teardown_disconnects_only_applied_pairs():
    """Only the pair whose connect_ports succeeded for the reservation is torn
    down; the never-connected pair gets no disconnect_ports."""
    await _seed_success_run(
        "connect_ports", "p1", "p2", dedupe_key=CREATED_KEY, device_id=SWITCH_ID
    )

    calls, execute_fn = _recording_execute()
    patches = _l1_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_switch_operations(
            device_ids=["dut-1"],
            action="disconnect_ports",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            only_applied_pairs=True,
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "disconnect_ports", "logout"]
    disconnects = [k for a, k in calls if a == "disconnect_ports"]
    assert disconnects == [{"port_a": "p1", "port_b": "p2"}]


async def test_l1_failed_teardown_skips_switch_when_nothing_applied():
    """A reservation that failed before any connect_ports landed drives no
    driver call at all, not even login."""
    calls, execute_fn = _recording_execute()
    patches = _l1_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_switch_operations(
            device_ids=["dut-1"],
            action="disconnect_ports",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            only_applied_pairs=True,
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []


async def test_l1_failed_teardown_ignores_other_reservations_connects():
    """A connect_ports run belonging to a DIFFERENT reservation does not count
    as applied state for this one."""
    await _seed_success_run(
        "connect_ports",
        "p1",
        "p2",
        dedupe_key=CREATED_KEY,
        device_id=SWITCH_ID,
        reservation_id=str(uuid.uuid4()),
    )

    calls, execute_fn = _recording_execute()
    patches = _l1_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_switch_operations(
            device_ids=["dut-1"],
            action="disconnect_ports",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            only_applied_pairs=True,
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []


async def test_l1_failed_teardown_redelivery_is_idempotent():
    """A redelivered reservation.failed skips the pair its first delivery
    already disconnected (the existing dedupe-key guard composes with the
    applied-state guard)."""
    await _seed_success_run(
        "connect_ports", "p1", "p2", dedupe_key=CREATED_KEY, device_id=SWITCH_ID
    )
    await _seed_success_run(
        "disconnect_ports", "p1", "p2", dedupe_key=FAILED_KEY, device_id=SWITCH_ID
    )

    calls, execute_fn = _recording_execute()
    patches = _l1_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_switch_operations(
            device_ids=["dut-1"],
            action="disconnect_ports",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            only_applied_pairs=True,
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []


# --- L2 stored-assignments-only teardown ---


def _l2_op(switch_id, port="eth1"):
    return {"switch_device_id": switch_id, "switch_port": port, "tag": "tagged"}


async def test_l2_failed_teardown_noops_without_assignments(caplog):
    """No stored assignment means no VLAN was ever created: log-and-noop, and
    in particular NO derived-VLAN fallback teardown."""
    caplog.set_level(logging.INFO, logger="app.services.nats_consumer")
    calls, execute_fn = _recording_execute()
    patches = _l2_patches(execute_fn, [_l2_op(SWITCH_ID)])
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            failed_cleanup=True,
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []
    assert any("no L2 state to tear down" in r.message for r in caplog.records)


async def test_l2_failed_teardown_uses_stored_vlan_and_releases_after():
    """Teardown drives the STORED VLAN id (not the derived one) and flips the
    assignment to RELEASED only after the switch was driven."""
    stored_vlan = _derive_vlan_id(RES_ID) + 1
    await _seed_vlan_assignment(stored_vlan, [SWITCH_ID])

    calls, execute_fn = _recording_execute()
    patches = _l2_patches(execute_fn, [_l2_op(SWITCH_ID)])
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            failed_cleanup=True,
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "remove_from_vlan", "delete_vlan", "logout"]
    vlan_ids = {k["vlan_id"] for a, k in calls if a in ("remove_from_vlan", "delete_vlan")}
    assert vlan_ids == {stored_vlan}
    assert await _vlan_assignment_statuses() == ["RELEASED"]


async def test_l2_failed_teardown_skips_switches_without_stored_assignment():
    """A resolved switch the stored assignments never covered got no
    create_vlan, so it gets no teardown either."""
    stored_vlan = _derive_vlan_id(RES_ID) + 1
    await _seed_vlan_assignment(stored_vlan, [SWITCH_ID])

    calls, execute_fn = _recording_execute()
    patches = _l2_patches(execute_fn, [_l2_op(SWITCH_ID), _l2_op(OTHER_SWITCH_ID, "eth9")])
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=FAILED_KEY,
            failed_cleanup=True,
        )
    finally:
        for p in patches:
            p.stop()

    ports = [k["port"] for a, k in calls if a == "remove_from_vlan"]
    assert ports == ["eth1"]


async def test_l2_failed_teardown_keeps_rows_active_on_transient_failure():
    """The read-then-release ordering: a transient upstream error mid-teardown
    leaves the assignment ACTIVE so the redelivery can finish the removal."""
    stored_vlan = _derive_vlan_id(RES_ID) + 1
    await _seed_vlan_assignment(stored_vlan, [SWITCH_ID])

    calls, execute_fn = _recording_execute()
    patches = _l2_patches(execute_fn, [_l2_op(SWITCH_ID)])
    patches[2] = patch(
        "app.services.nats_consumer._fetch_template",
        new=AsyncMock(side_effect=TransientUpstreamError("inventory down")),
    )
    for p in patches:
        p.start()
    try:
        with pytest.raises(TransientUpstreamError):
            await _execute_l2_switch_operations(
                device_ids=["dut-1"],
                l2_action="deprovision",
                reservation_id=RES_ID,
                user_id=USER_ID,
                get_db_session=_db_session_factory(),
                dedupe_key=FAILED_KEY,
                failed_cleanup=True,
            )
    finally:
        for p in patches:
            p.stop()

    assert await _vlan_assignment_statuses() == ["ACTIVE"]


async def test_l2_cancel_deprovision_still_uses_release_first_fallback_path():
    """The cancel/complete path is unchanged: no failed_cleanup flag means the
    legacy derived-VLAN fallback still fires when no assignment is stored."""
    calls, execute_fn = _recording_execute()
    patches = _l2_patches(execute_fn, [_l2_op(SWITCH_ID)])
    for p in patches:
        p.start()
    try:
        await _execute_l2_switch_operations(
            device_ids=["dut-1"],
            l2_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "remove_from_vlan", "delete_vlan", "logout"]
    vlan_ids = {k["vlan_id"] for a, k in calls if a in ("remove_from_vlan", "delete_vlan")}
    assert vlan_ids == {_derive_vlan_id(RES_ID)}


# --- dispatch ---


async def test_handle_failed_event_dispatches_applied_state_teardown():
    """reservation.failed dispatches all three executors with the teardown
    flags: L1 only_applied_pairs, L2 failed_cleanup, L3 plain deprovision."""
    event = {
        "event": "reservation.failed",
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": ["dut-1"],
    }
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new=AsyncMock(),
        ) as l1,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new=AsyncMock(),
        ) as l2,
        patch(
            "app.services.nats_consumer._execute_l3_switch_operations",
            new=AsyncMock(),
        ) as l3,
    ):
        await handle_reservation_event(event, _db_session_factory(), FAILED_KEY)

    assert l1.await_args.args[0:2] == (["dut-1"], "disconnect_ports")
    assert l1.await_args.kwargs["only_applied_pairs"] is True
    assert l2.await_args.args[0:2] == (["dut-1"], "deprovision")
    assert l2.await_args.kwargs["failed_cleanup"] is True
    assert l3.await_args.args[0:2] == (["dut-1"], "deprovision")


async def test_handle_created_event_does_not_set_teardown_flags():
    """The provisioning path is unchanged: no applied-state flags on created."""
    event = {
        "event": "reservation.created",
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": ["dut-1"],
    }
    with (
        patch(
            "app.services.nats_consumer._execute_switch_operations",
            new=AsyncMock(),
        ) as l1,
        patch(
            "app.services.nats_consumer._execute_l2_switch_operations",
            new=AsyncMock(),
        ) as l2,
        patch(
            "app.services.nats_consumer._execute_l3_switch_operations",
            new=AsyncMock(),
        ),
    ):
        await handle_reservation_event(event, _db_session_factory(), CREATED_KEY)

    assert l1.await_args.kwargs["only_applied_pairs"] is False
    assert l2.await_args.kwargs["failed_cleanup"] is False
