"""Driver load failures in the wiring paths are classified by kind (issue #1002).

``load_driver`` raises ``DriverPackageError`` for a broken package (permanent: the same
SHA256 can never load) and a plain ``RuntimeError`` when the package download fails
(transient: inventory or package storage is unreachable). The L1, L2, and L3 wiring
applies used to park both under the pinned non-retryable ``recorded hop unresolvable``
reason, so a brief outage during a wiring event stranded the rows until a fork re-save,
and on the release side left ports, memberships, and routes on the devices for good.

These tests pin the split at all three layers in both directions, through the real
``load_driver`` with only the package download patched to fail, plus a terminal
teardown removal, and show the background retry tick picking the transient rows up
and converging once the download works.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.driver_loader import (
    DriverPackageError,
    driver_load_failure_text,
    is_permanent_load_failure,
)
from app.services.nats_consumer import (
    WIRING_UNRESOLVABLE_REASON,
    _apply_l2_memberships,
    _apply_l3_adjacency,
    _apply_wiring_pairs,
    _FetchContext,
    _teardown_from_ledgers,
    _wiring_load_failure,
)
from app.services.wiring_retry_service import is_retryable_failure, run_wiring_retry_tick
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


RES_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
SW_L1 = str(uuid.uuid4())
SW_L2 = str(uuid.uuid4())
SW_L3 = str(uuid.uuid4())
SW_L3_B = str(uuid.uuid4())
FABRIC = uuid.uuid4()

TEMPLATE_DATA = {"id": "tmpl-1", "name": "Switch Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}
ROUTES = [{"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"}]
TRANSIENT_TEXT = "driver load failed: ConnectError"
PERMANENT_TEXT = f"{WIRING_UNRESOLVABLE_REASON}: driver load failed: DriverPackageError"


def _dev(device_id, ctype):
    return {
        "id": device_id,
        "name": f"dev-{device_id[:4]}",
        "template_id": "tmpl-1",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha256abc",
        "driver_filename": "driver.zip",
        "connection_type": ctype,
        "field_data": {"ip": "10.0.0.1"},
    }


DEVICES = {
    SW_L1: _dev(SW_L1, "Layer 1 Switch"),
    SW_L2: _dev(SW_L2, "Layer 2 Switch"),
    SW_L3: _dev(SW_L3, "Layer 3 Switch"),
    SW_L3_B: _dev(SW_L3_B, "Layer 3 Switch"),
}


async def _device_fetch(device_id, client=None):
    # Anything that is not a switch resolves as a plain server (wire far ends).
    return DEVICES.get(str(device_id)) or {
        "id": str(device_id),
        "name": "dut",
        "connection_type": "Server",
        "field_data": {},
    }


def _recorder():
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append((action, kwargs.get("method_kwargs") or {}))
        return SUCCESS_RESULT

    return execute_fn, calls


def _driver_actions(calls):
    return [a for a, _kw in calls if a not in ("login", "logout")]


def _l1_wires(pairs):
    """Two recorded hops per pair, sharing an edge_key, so the chain-walk yields `pairs`."""
    wires = []
    for pa, pb in pairs:
        edge = str(uuid.uuid4())
        wires.append(
            {
                "device_a_id": str(uuid.uuid4()),
                "port_a": "eth0",
                "device_b_id": SW_L1,
                "port_b": pa,
                "layer": "L1",
                "physical_connection_id": None,
                "edge_key": edge,
            }
        )
        wires.append(
            {
                "device_a_id": SW_L1,
                "port_a": pb,
                "device_b_id": str(uuid.uuid4()),
                "port_b": "eth0",
                "layer": "L1",
                "physical_connection_id": None,
                "edge_key": edge,
            }
        )
    return wires


def _patches(execute_fn, *, load=None, download_fails=False, fork_wires=None):
    """Patch inventory and the sandbox. `download_fails` keeps the REAL load_driver and
    makes only the package download raise (the transient case end to end); `load`
    replaces load_driver outright (an AsyncMock), else load_driver returns a path."""
    ps = [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=fork_wires or []),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]
    if download_fails:
        ps.append(
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(side_effect=httpx.ConnectError("http://inventory:8000 refused")),
            )
        )
    else:
        ps.append(
            patch(
                "app.services.driver_loader.load_driver",
                new=load or AsyncMock(return_value="/tmp/driver"),
            )
        )
    return ps


def _enter(stack, ps):
    for p in ps:
        stack.enter_context(p)


# --- the classifier ---------------------------------------------------------


def test_only_a_driver_package_error_is_a_permanent_load_failure():
    assert is_permanent_load_failure(DriverPackageError("missing Driver class")) is True
    assert is_permanent_load_failure(RuntimeError("Failed to download driver x")) is False
    assert is_permanent_load_failure(OSError("cache table unreadable")) is False


def test_load_failure_text_names_the_cause_class_never_the_message():
    try:
        try:
            raise httpx.ConnectError("http://inventory:8000/secret-path refused")
        except httpx.ConnectError as cause:
            raise RuntimeError("Failed to download driver x: ConnectError") from cause
    except RuntimeError as exc:
        text = driver_load_failure_text(exc)
    assert text == "driver load failed: ConnectError"
    assert driver_load_failure_text(DriverPackageError("Driver validation failed: x")) == (
        "driver load failed: DriverPackageError"
    )


def test_wiring_load_failure_reason_and_attempts_by_kind():
    reason, attempts = _wiring_load_failure(DriverPackageError("broken"), SW_L1)
    assert (reason, attempts) == (PERMANENT_TEXT, 0)
    assert is_retryable_failure(reason) is False

    reason, attempts = _wiring_load_failure(RuntimeError("download failed"), SW_L1)
    assert (reason, attempts) == ("driver load failed: RuntimeError", 1)
    assert is_retryable_failure(reason) is True


# --- L1 ---------------------------------------------------------------------


async def _seed_l1_active(port_a, port_b):
    async with TestSessionLocal() as s:
        s.add(
            L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(SW_L1),
                port_a=port_a,
                port_b=port_b,
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()


async def _l1_rows():
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L1ConnectionAssignment))).scalars().all()
    return {(r.port_a, r.port_b): r for r in rows}


async def _apply_l1(execute_fn, **patch_kwargs):
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        await _apply_wiring_pairs(
            RES_ID,
            {SW_L1: [("0/0/3", "0/0/4", None)]},
            {SW_L1: [("0/0/1", "0/0/2", None)]},
            [],
            _FetchContext(None),
            _db_session_factory(),
        )


async def test_l1_transient_download_failure_parks_build_and_release_retryable():
    await _seed_l1_active("0/0/3", "0/0/4")
    execute_fn, calls = _recorder()
    await _apply_l1(execute_fn, download_fails=True)

    assert calls == [], "a driver that did not load is never driven"
    rows = await _l1_rows()
    build, release = rows[("0/0/1", "0/0/2")], rows[("0/0/3", "0/0/4")]
    assert (build.status, build.intended) == ("FAILED", "ACTIVE")
    assert (release.status, release.intended) == ("FAILED", "RELEASED")
    for row in (build, release):
        assert row.last_error == TRANSIENT_TEXT
        assert row.attempts == 1
        assert is_retryable_failure(row.last_error)


async def test_l1_broken_package_parks_build_and_release_permanent():
    await _seed_l1_active("0/0/3", "0/0/4")
    execute_fn, calls = _recorder()
    await _apply_l1(execute_fn, load=AsyncMock(side_effect=DriverPackageError("broken")))

    assert calls == []
    for row in (await _l1_rows()).values():
        assert row.status == "FAILED"
        assert row.last_error == PERMANENT_TEXT
        assert row.attempts == 0
        assert not is_retryable_failure(row.last_error)


async def test_l1_transient_rows_converge_on_the_next_retry_tick():
    """The transient build and release rows are both picked up by the background tick
    once the download works: the build connects ACTIVE, the release disconnects."""
    await _seed_l1_active("0/0/3", "0/0/4")
    execute_fn, _calls = _recorder()
    await _apply_l1(execute_fn, download_fails=True)

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, fork_wires=_l1_wires([("0/0/1", "0/0/2")])))
        stats = await run_wiring_retry_tick(_db_session_factory())

    assert stats["skipped_not_retryable"] == 0
    assert stats["reconnected"] == 1
    assert stats["released"] == 1
    assert sorted(_driver_actions(calls)) == ["connect_ports", "disconnect_ports"]
    rows = await _l1_rows()
    assert rows[("0/0/1", "0/0/2")].status == "ACTIVE"
    assert rows[("0/0/3", "0/0/4")].status == "RELEASED"


async def test_l1_broken_package_rows_are_not_retried_by_the_tick():
    await _seed_l1_active("0/0/3", "0/0/4")
    execute_fn, _calls = _recorder()
    await _apply_l1(execute_fn, load=AsyncMock(side_effect=DriverPackageError("broken")))

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, fork_wires=_l1_wires([("0/0/1", "0/0/2")])))
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert stats["skipped_not_retryable"] == 2
    assert calls == []


async def test_teardown_l1_removal_with_transient_download_failure_stays_retryable():
    """A terminal teardown whose disconnect cannot load the driver leaves the pair
    FAILED intended RELEASED under a retryable reason; the frozen reservation's
    release-direction tick then finishes the disconnect."""
    await _seed_l1_active("0/0/3", "0/0/4")
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=True))
        await s.commit()

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, download_fails=True))
        await _teardown_from_ledgers(RES_ID, _FetchContext(None), _db_session_factory())
    row = (await _l1_rows())[("0/0/3", "0/0/4")]
    assert (row.status, row.intended, row.last_error) == ("FAILED", "RELEASED", TRANSIENT_TEXT)
    assert calls == []

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert stats["released"] == 1
    assert _driver_actions(calls) == ["disconnect_ports"]
    assert (await _l1_rows())[("0/0/3", "0/0/4")].status == "RELEASED"


# --- L2 ---------------------------------------------------------------------


async def _seed_l2(defined: bool):
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2] if defined else [],
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
                port="0/0/9",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()
        return va.id


async def _l2_rows():
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    return {r.port: r for r in rows}


async def _apply_l2(va_id, execute_fn, *, adds=True, **patch_kwargs):
    def item(port):
        return {
            "switch_device_id": SW_L2,
            "port": port,
            "vlan_assignment_id": va_id,
            "vlan_id": 100,
        }

    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        await _apply_l2_memberships(
            RES_ID,
            [item("0/0/9")],
            [item("0/0/1")] if adds else [],
            _FetchContext(None),
            _db_session_factory(),
        )


async def test_l2_transient_download_failure_parks_add_and_remove_retryable():
    va = await _seed_l2(defined=True)
    execute_fn, calls = _recorder()
    await _apply_l2(va, execute_fn, download_fails=True)

    assert calls == []
    rows = await _l2_rows()
    assert (rows["0/0/1"].status, rows["0/0/1"].intended) == ("FAILED", "ACTIVE")
    assert (rows["0/0/9"].status, rows["0/0/9"].intended) == ("FAILED", "RELEASED")
    for row in rows.values():
        assert row.last_error == TRANSIENT_TEXT
        assert row.attempts == 1
        assert is_retryable_failure(row.last_error)


async def test_l2_broken_package_parks_add_and_remove_permanent():
    va = await _seed_l2(defined=True)
    execute_fn, calls = _recorder()
    await _apply_l2(va, execute_fn, load=AsyncMock(side_effect=DriverPackageError("broken")))

    assert calls == []
    for row in (await _l2_rows()).values():
        assert row.last_error == PERMANENT_TEXT
        assert row.attempts == 0
        assert not is_retryable_failure(row.last_error)


async def test_l2_transient_failure_during_vlan_define_parks_the_add_retryable():
    """The create_vlan define pass loads the driver too; a download failure there parks
    the dependent add under a retryable create_vlan reason, never the pinned one."""
    va = await _seed_l2(defined=False)
    execute_fn, calls = _recorder()
    await _apply_l2(va, execute_fn, download_fails=True)

    row = (await _l2_rows())["0/0/1"]
    assert row.status == "FAILED"
    assert row.last_error == f"create_vlan failed: {TRANSIENT_TEXT}"
    assert row.attempts == 1
    assert is_retryable_failure(row.last_error)


async def test_l2_transient_remove_converges_on_the_next_retry_tick():
    va = await _seed_l2(defined=True)
    execute_fn, _calls = _recorder()
    await _apply_l2(va, execute_fn, adds=False, download_fails=True)
    assert (await _l2_rows())["0/0/9"].last_error == TRANSIENT_TEXT

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert stats["released"] == 1
    assert "remove_from_vlan" in _driver_actions(calls)
    assert (await _l2_rows())["0/0/9"].status == "RELEASED"


# --- L3 ---------------------------------------------------------------------


async def _seed_l3_active(switch_id):
    async with TestSessionLocal() as s:
        s.add(
            RouteAssignment(
                reservation_id=uuid.UUID(RES_ID),
                device_id=uuid.UUID(switch_id),
                routes=ROUTES,
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()


async def _l3_rows():
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(RouteAssignment))).scalars().all()
    return {str(r.device_id): r for r in rows}


async def _apply_l3(execute_fn, **patch_kwargs):
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        await _apply_l3_adjacency(
            RES_ID,
            [{"device_id": SW_L3_B, "routes": ROUTES}],
            [{"device_id": SW_L3, "routes": ROUTES}],
            _FetchContext(None),
            _db_session_factory(),
        )


async def test_l3_transient_download_failure_parks_provision_and_deprovision_retryable():
    await _seed_l3_active(SW_L3_B)
    execute_fn, calls = _recorder()
    await _apply_l3(execute_fn, download_fails=True)

    assert calls == []
    rows = await _l3_rows()
    assert (rows[SW_L3].status, rows[SW_L3].intended) == ("FAILED", "ACTIVE")
    assert (rows[SW_L3_B].status, rows[SW_L3_B].intended) == ("FAILED", "RELEASED")
    for row in rows.values():
        assert row.last_error == TRANSIENT_TEXT
        assert row.attempts == 1
        assert row.routes == ROUTES
        assert is_retryable_failure(row.last_error)


async def test_l3_broken_package_parks_provision_and_deprovision_permanent():
    await _seed_l3_active(SW_L3_B)
    execute_fn, calls = _recorder()
    await _apply_l3(execute_fn, load=AsyncMock(side_effect=DriverPackageError("broken")))

    assert calls == []
    for row in (await _l3_rows()).values():
        assert row.last_error == PERMANENT_TEXT
        assert row.attempts == 0
        assert not is_retryable_failure(row.last_error)


async def test_l3_transient_rows_converge_on_the_next_retry_tick():
    await _seed_l3_active(SW_L3_B)
    execute_fn, _calls = _recorder()
    await _apply_l3(execute_fn, download_fails=True)

    l3_wire = {
        "device_a_id": str(uuid.uuid4()),
        "port_a": "eth0",
        "device_b_id": SW_L3,
        "port_b": "ge-0/0/1",
        "layer": "L1",
        "physical_connection_id": None,
        "edge_key": None,
    }
    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, fork_wires=[l3_wire]))
        stats = await run_wiring_retry_tick(_db_session_factory())
    assert stats["reconnected"] == 1
    assert stats["released"] == 1
    assert sorted(_driver_actions(calls)) == ["configure_route", "remove_route"]
    rows = await _l3_rows()
    assert rows[SW_L3].status == "ACTIVE"
    assert rows[SW_L3_B].status == "RELEASED"
