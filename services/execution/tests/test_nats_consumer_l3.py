"""Tests for the L3 route provisioning paths in nats_consumer.py (issue #20).

The invariant under test throughout: provision applies exactly the routes the
switch's latest config version declared at provisioning time, pinned in
route_assignments; deprovision removes exactly the pinned set, never a
re-derived one, and releases the pin only AFTER driver execution.
"""

import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.execution_run import ExecutionRun
from app.services.nats_consumer import (
    L3_EVENT_ACTIONS,
    TransientUpstreamError,
    _execute_l3_switch_operations,
    _fetch_latest_config,
    _resolve_l3_switch_operations,
)
from app.services.route_service import assign_routes, get_route_assignments
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
DRIVER_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())

SWITCH_DATA = {
    "id": SWITCH_ID,
    "name": "L3-Switch",
    "template_id": "tmpl-1",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 3 Switch",
    "field_data": {"ip": "10.0.0.1"},
}

TEMPLATE_DATA = {
    "id": "tmpl-1",
    "name": "L3 Template",
    "sections": [{"name": "Net", "fields": [{"key": "ip", "type": "string"}]}],
}

SUCCESS_RESULT = {
    "success": True,
    "output": {"result": True},
    "error": None,
    "duration_ms": 50,
}

ROUTES = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]

EDITED_ROUTES = [
    {"destination": "172.16.0.0/16", "next_hop": "192.168.1.254", "interface": "eth2"},
]

CONFIG_DETAIL = {"id": str(uuid.uuid4()), "version_number": 2, "config": {"routes": ROUTES}}


def _recording_execute():
    """An execute_driver_method stub that records each (action, kwargs) it runs."""
    calls: list[tuple[str, dict]] = []

    def _execute(driver_path, action, context, **kwargs):
        calls.append((action, kwargs.get("method_kwargs") or {}))
        return SUCCESS_RESULT

    return calls, _execute


def _l3_patches(execute_fn, config_detail=CONFIG_DETAIL, resolver_return=None):
    """Patches for driving _execute_l3_switch_operations without real HTTP."""
    if resolver_return is None:
        resolver_return = [SWITCH_ID]
    return [
        patch(
            "app.services.nats_consumer._resolve_l3_switch_operations",
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
        patch(
            "app.services.nats_consumer._fetch_latest_config",
            new=AsyncMock(return_value=config_detail),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ]


async def _seed_success_run(action, port_a=None, port_b=None, *, dedupe_key, device_id):
    """Persist a SUCCESS ExecutionRun standing in for a prior, acked delivery."""
    async with TestSessionLocal() as session:
        session.add(
            ExecutionRun(
                device_id=uuid.UUID(device_id),
                driver_id=uuid.UUID(DRIVER_ID),
                driver_sha256="sha256abc",
                action=action,
                status="SUCCESS",
                user_id=uuid.UUID(USER_ID),
                input_params={},
                port_a=port_a,
                port_b=port_b,
                dedupe_key=dedupe_key,
            )
        )
        await session.commit()


async def _active_assignments():
    async with TestSessionLocal() as db:
        return await get_route_assignments(db, RES_ID)


# --- mappings and resolver ---


def test_l3_event_actions_mapping():
    assert L3_EVENT_ACTIONS["reservation.created"] == "provision"
    assert L3_EVENT_ACTIONS["reservation.cancelled"] == "deprovision"
    assert L3_EVENT_ACTIONS["reservation.completed"] == "deprovision"
    assert "reservation.updated" not in L3_EVENT_ACTIONS


async def test_resolve_l3_ops_identifies_and_dedupes_adjacent_switches():
    """Two reserved devices behind one L3 switch resolve to that switch once."""
    conns = {
        "dut-1": [
            {
                "device_a_id": "dut-1",
                "port_a": "eth0",
                "device_b_id": SWITCH_ID,
                "port_b": "ge-0/0/1",
            }
        ],
        "dut-2": [
            {
                "device_a_id": SWITCH_ID,
                "port_a": "ge-0/0/2",
                "device_b_id": "dut-2",
                "port_b": "eth0",
            }
        ],
    }
    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new=AsyncMock(side_effect=lambda did, client=None: conns[did]),
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value=SWITCH_DATA),
        ),
    ):
        switches = await _resolve_l3_switch_operations(["dut-1", "dut-2"])
    assert switches == [SWITCH_ID]


async def test_resolve_l3_ops_ignores_non_l3_far_ends():
    conn = [{"device_a_id": "dut-1", "port_a": "eth0", "device_b_id": "other", "port_b": "p1"}]
    l2_device = {**SWITCH_DATA, "connection_type": "Layer 2 Switch"}
    with (
        patch(
            "app.services.nats_consumer._fetch_connections_for_device",
            new=AsyncMock(return_value=conn),
        ),
        patch(
            "app.services.nats_consumer._fetch_device",
            new=AsyncMock(return_value=l2_device),
        ),
    ):
        switches = await _resolve_l3_switch_operations(["dut-1"])
    assert switches == []


# --- _fetch_latest_config transient classification ---


async def test_config_fetch_5xx_raises_transient_for_nak():
    """An inventory 5xx must NAK the event, not silently skip provisioning."""

    class _Resp:
        status_code = 500

    client = AsyncMock()
    client.get = AsyncMock(return_value=_Resp())
    with pytest.raises(TransientUpstreamError):
        await _fetch_latest_config(SWITCH_ID, client)


async def test_config_fetch_404_returns_none():
    class _Resp:
        status_code = 404

    client = AsyncMock()
    client.get = AsyncMock(return_value=_Resp())
    assert await _fetch_latest_config(SWITCH_ID, client) is None


# --- executor early returns ---


async def test_execute_l3_operations_skips_without_reservation_id(caplog):
    await _execute_l3_switch_operations(
        device_ids=["dut-1"],
        l3_action="provision",
        reservation_id=None,
        user_id=USER_ID,
        get_db_session=_db_session_factory(),
    )
    assert any("No reservation_id for L3 operations" in r.message for r in caplog.records)


async def test_execute_l3_operations_noops_when_no_l3_switches(caplog):
    caplog.set_level(logging.INFO, logger="app.services.nats_consumer")
    with patch(
        "app.services.nats_consumer._resolve_l3_switch_operations",
        new=AsyncMock(return_value=[]),
    ):
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    assert any("No L3 switch operations needed" in r.message for r in caplog.records)


async def test_execute_l3_provision_noops_when_config_has_no_routes(caplog):
    """A switch whose latest config declares no routes is skipped and nothing
    is pinned."""
    caplog.set_level(logging.INFO, logger="app.services.nats_consumer")
    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn, config_detail={"config": {"interfaces": []}})
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []
    assert await _active_assignments() == []
    assert any("has no routes in its latest config" in r.message for r in caplog.records)


# --- provision ---


async def test_execute_l3_provision_wraps_routes_in_one_login_logout():
    """Provision runs login, one configure_route per route, logout; pins the
    set; and passes a null next_hop through for interface routes."""
    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "configure_route", "configure_route", "logout"]
    kwargs_by_dest = {k["destination"]: k for a, k in calls if a == "configure_route"}
    assert kwargs_by_dest["10.0.0.0/24"] == {
        "destination": "10.0.0.0/24",
        "next_hop": "192.168.1.1",
        "interface": "eth0",
    }
    assert kwargs_by_dest["10.1.0.0/24"]["next_hop"] is None

    assignments = await _active_assignments()
    assert len(assignments) == 1
    assert assignments[0].routes == ROUTES

    # Each route run is keyed on (destination, next_hop, interface) via
    # _route_run_identity: port_a=destination, port_b="interface|next_hop".
    async with TestSessionLocal() as db:
        runs = list(
            (await db.execute(select(ExecutionRun).where(ExecutionRun.action == "configure_route")))
            .scalars()
            .all()
        )
    assert {(r.port_a, r.port_b) for r in runs} == {
        ("10.0.0.0/24", "eth0|192.168.1.1"),
        ("10.1.0.0/24", "eth1|"),
    }


async def test_execute_l3_provision_does_not_collapse_ecmp_siblings():
    """Two routes to the same prefix out the same interface via different next
    hops (same-interface ECMP) both get configured; the guard identity includes
    next_hop so they do not collapse into one action."""
    ecmp = [
        {"destination": "10.0.0.0/8", "next_hop": "192.0.2.1", "interface": "eth0"},
        {"destination": "10.0.0.0/8", "next_hop": "192.0.2.2", "interface": "eth0"},
    ]
    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn, config_detail={"config": {"routes": ecmp}})
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key="HERD_RESERVATIONS:9",
        )
    finally:
        for p in patches:
            p.stop()

    next_hops = sorted(k["next_hop"] for a, k in calls if a == "configure_route")
    assert next_hops == ["192.0.2.1", "192.0.2.2"]


async def test_execute_l3_provision_redelivery_uses_pinned_set_not_current_config():
    """A redelivery after a config edit provisions the ORIGINAL pinned routes."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn, config_detail={"config": {"routes": EDITED_ROUTES}})
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    destinations = [k["destination"] for a, k in calls if a == "configure_route"]
    assert destinations == ["10.0.0.0/24", "10.1.0.0/24"]


async def test_execute_l3_redelivery_skips_already_applied_routes():
    """With a dedupe_key, a route whose SUCCESS run exists is not re-run."""
    key = "HERD_RESERVATIONS:7"
    # Seed the SUCCESS run using the same (destination, next_hop, interface)
    # identity encoding the guard uses: port_a=destination, port_b="iface|hop".
    await _seed_success_run(
        "configure_route", "10.0.0.0/24", "eth0|192.168.1.1", dedupe_key=key, device_id=SWITCH_ID
    )

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="provision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            dedupe_key=key,
        )
    finally:
        for p in patches:
            p.stop()

    destinations = [k["destination"] for a, k in calls if a == "configure_route"]
    assert destinations == ["10.1.0.0/24"]


# --- deprovision ---


async def test_execute_l3_deprovision_uses_stored_assignments_not_current_config():
    """Deprovision removes the pinned set even after a config edit, then
    releases the assignment."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn, config_detail={"config": {"routes": EDITED_ROUTES}})
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "remove_route", "remove_route", "logout"]
    destinations = {k["destination"] for a, k in calls if a == "remove_route"}
    assert destinations == {"10.0.0.0/24", "10.1.0.0/24"}
    assert await _active_assignments() == []


async def test_execute_l3_deprovision_noops_without_stored_assignment(caplog):
    """No pinned assignment means log-and-noop: no legacy re-derive fallback."""
    caplog.set_level(logging.INFO, logger="app.services.nats_consumer")
    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []
    assert any("No L3 switch operations needed" in r.message for r in caplog.records)


async def test_execute_l3_deprovision_keeps_pin_on_transient_failure():
    """The release-LAST ordering: a transient upstream error mid-deprovision
    leaves the assignment ACTIVE so the redelivery can finish the removal."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    # Template fetch dies AFTER the assignment was read but before any release.
    patches[2] = patch(
        "app.services.nats_consumer._fetch_template",
        new=AsyncMock(side_effect=TransientUpstreamError("inventory down")),
    )
    for p in patches:
        p.start()
    try:
        with pytest.raises(TransientUpstreamError):
            await _execute_l3_switch_operations(
                device_ids=["dut-1"],
                l3_action="deprovision",
                reservation_id=RES_ID,
                user_id=USER_ID,
                get_db_session=_db_session_factory(),
            )
    finally:
        for p in patches:
            p.stop()

    assignments = await _active_assignments()
    assert len(assignments) == 1
    assert assignments[0].status == "ACTIVE"


async def test_execute_l3_deprovision_keeps_pin_when_remove_route_result_fails():
    """A driver-result failure (remove_route returns success=False, no raise)
    does NOT release the pin: an ACTIVE pin means routes may still be installed,
    which stays accurate for the stranded-teardown cleanup path (#244)."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    def _fail_remove(driver_path, action, context, **kwargs):
        if action == "remove_route":
            return {
                "success": False,
                "output": None,
                "error": "switch unreachable",
                "duration_ms": 5,
            }
        return SUCCESS_RESULT

    patches = _l3_patches(_fail_remove)
    for p in patches:
        p.start()
    try:
        # Driver-result failures ACK (no raise), so this returns normally.
        await _execute_l3_switch_operations(
            device_ids=["dut-1"],
            l3_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
        )
    finally:
        for p in patches:
            p.stop()

    assignments = await _active_assignments()
    assert len(assignments) == 1
    assert assignments[0].status == "ACTIVE"


# --- reservation.updated removal path ---


async def test_updated_removal_keeps_routes_while_switch_still_serves_reservation():
    """A switch adjacent to a remaining device is NOT deprovisioned on edit."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    # Removed devices resolve to the switch AND the remaining devices still
    # resolve to it: departing set is empty.
    patches[0] = patch(
        "app.services.nats_consumer._resolve_l3_switch_operations",
        new=AsyncMock(side_effect=[[SWITCH_ID], [SWITCH_ID]]),
    )
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-removed"],
            l3_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            remaining_device_ids=["dut-kept"],
        )
    finally:
        for p in patches:
            p.stop()

    assert calls == []
    assert len(await _active_assignments()) == 1


async def test_updated_removal_deprovisions_departing_switch():
    """A switch no longer adjacent to any remaining device loses its routes."""
    async with TestSessionLocal() as db:
        await assign_routes(db, RES_ID, SWITCH_ID, ROUTES)

    calls, execute_fn = _recording_execute()
    patches = _l3_patches(execute_fn)
    patches[0] = patch(
        "app.services.nats_consumer._resolve_l3_switch_operations",
        new=AsyncMock(side_effect=[[SWITCH_ID], []]),
    )
    for p in patches:
        p.start()
    try:
        await _execute_l3_switch_operations(
            device_ids=["dut-removed"],
            l3_action="deprovision",
            reservation_id=RES_ID,
            user_id=USER_ID,
            get_db_session=_db_session_factory(),
            remaining_device_ids=["dut-kept"],
        )
    finally:
        for p in patches:
            p.stop()

    actions = [a for a, _ in calls]
    assert actions == ["login", "remove_route", "remove_route", "logout"]
    assert await _active_assignments() == []
