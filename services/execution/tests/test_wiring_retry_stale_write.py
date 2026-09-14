"""Retry failure writes are row-identity compare-and-swaps (issue #814).

A retry channel loads FAILED rows, drives the driver, then records the outcome. The
driver call takes seconds, and in that window other writers move the rows: the manual
channel flips a FAILED row ACTIVE in place, and a fork save then releases it. The
failure write that arrives afterwards used to upsert by key, and its key match excluded
RELEASED rows, so it found nothing and INSERTED a fresh FAILED intended-ACTIVE row,
resurrecting wiring the reservation no longer intends (four l2 rows where the e2e test
needs two, the 2026-09-14 nightly failure).

These tests drive the exact ordering through the real retry channel: the concurrent
writers run INSIDE the patched driver call, which is precisely where the race lands
them, and they are the production functions (record_*_active, release_*), not hand-set
columns. One test per layer for the released-mid-flight case, the still-FAILED case
(the write must apply and attempts must accumulate), and the flipped-ACTIVE case.
"""

import logging
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.wiring_retry_service import run_wiring_retry_tick
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
DUT = str(uuid.uuid4())
DUT_B = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
FABRIC = uuid.uuid4()

PORTS = ("0/0/1", "0/0/2")
L1_PAIR = ("a1", "a2")
PINNED = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]
DRIVER_ERROR = "mock injected failure"
TEMPLATE_DATA = {"id": "tmpl-1", "name": "Template", "sections": []}


def _switch_data(device_id, ctype):
    return {
        "id": device_id,
        "name": ctype,
        "template_id": "tmpl-1",
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": ctype,
        "field_data": {"ip": "10.0.0.1"},
    }


SWITCHES = {
    SW_L1: _switch_data(SW_L1, "Layer 1 Switch"),
    SW_L2: _switch_data(SW_L2, "Layer 2 Switch"),
    SW_L3: _switch_data(SW_L3, "Layer 3 Switch"),
}


def _fork_wire(device_a, port_a, device_b, port_b, edge_key=None):
    return {
        "device_a_id": device_a,
        "port_a": port_a,
        "device_b_id": device_b,
        "port_b": port_b,
        "layer": "L1",
        "physical_connection_id": None,
        "edge_key": edge_key,
    }


# Build-intent revalidation (issue #491) fetches this before any build-direction
# reattempt: every seeded build below stays intended, so the retry really drives.
FORK_WIRES = [
    _fork_wire(DUT, "eth0", SW_L2, PORTS[0]),
    _fork_wire(DUT_B, "eth0", SW_L2, PORTS[1]),
    _fork_wire(DUT, "eth1", SW_L1, L1_PAIR[0], edge_key="e1"),
    _fork_wire(SW_L1, L1_PAIR[1], DUT_B, "eth1", edge_key="e1"),
    _fork_wire(DUT, "eth2", SW_L3, "ge-0/0/1"),
]


def _patches(driver_side_effect):
    """Patch the fetches, the driver load, and the bounded driver call itself.

    `_run_driver_with_retry` is the seam the concurrent writers hook into: it is the
    only await between the channel's row load and its failure write, which is exactly
    where the real race lands. It is patched (rather than the sandbox below it) so the
    injected writers run on the event loop, deterministically, once per op.
    """

    async def _device(device_id, client=None):
        found = SWITCHES.get(str(device_id))
        if found is not None:
            return found
        return {"id": str(device_id), "name": "dut", "connection_type": "Server", "field_data": {}}

    async def _config(device_id, client=None):
        return {"id": "cfg-1", "config": {"routes": PINNED}}

    stack = ExitStack()
    for p in [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch(
            "app.services.nats_consumer._fetch_latest_config", new=AsyncMock(side_effect=_config)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=FORK_WIRES),
        ),
        patch(
            "app.services.nats_consumer._run_driver_with_retry",
            new=AsyncMock(side_effect=driver_side_effect),
        ),
    ]:
        stack.enter_context(p)
    return stack


def _driver(fail_actions, on_first_fail=None):
    """A _run_driver_with_retry stand-in: succeed, except for `fail_actions`.

    `on_first_fail` is awaited once, immediately before the FIRST failing op returns,
    so the concurrent writers land between the channel's row load and its failure
    write. Returns the (ok, attempts, last_error, result) tuple the real one returns.
    """
    state = {"fired": False}

    async def _run(driver_path, action, context, password_keys, method_kwargs=None):
        if action not in fail_actions:
            return True, 1, None, {"success": True, "output": {"result": True}}
        if on_first_fail is not None and not state["fired"]:
            state["fired"] = True
            await on_first_fail()
        return False, 3, f"{DRIVER_ERROR} on {action}", None

    return _run


# --- seeding ------------------------------------------------------------------


async def _seed_alloc(vlan_id=100):
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=vlan_id,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        await s.refresh(va)
        return va.id


async def _seed_l2_failed(port, va_id, attempts=3):
    async with TestSessionLocal() as s:
        row = L2PortAssignment(
            reservation_id=uuid.UUID(RES_ID),
            vlan_assignment_id=va_id,
            switch_device_id=uuid.UUID(SW_L2),
            port=port,
            status="FAILED",
            intended="ACTIVE",
            attempts=attempts,
            last_error=DRIVER_ERROR,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_l1_failed(attempts=3):
    async with TestSessionLocal() as s:
        row = L1ConnectionAssignment(
            reservation_id=uuid.UUID(RES_ID),
            switch_device_id=uuid.UUID(SW_L1),
            port_a=L1_PAIR[0],
            port_b=L1_PAIR[1],
            status="FAILED",
            intended="ACTIVE",
            attempts=attempts,
            last_error=DRIVER_ERROR,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _seed_l3_failed(attempts=3):
    async with TestSessionLocal() as s:
        row = RouteAssignment(
            reservation_id=uuid.UUID(RES_ID),
            device_id=uuid.UUID(SW_L3),
            routes=PINNED,
            status="FAILED",
            intended="ACTIVE",
            attempts=attempts,
            last_error=DRIVER_ERROR,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _l2_rows():
    async with TestSessionLocal() as s:
        return list(
            (await s.execute(select(L2PortAssignment).order_by(L2PortAssignment.port)))
            .scalars()
            .all()
        )


async def _l1_rows():
    async with TestSessionLocal() as s:
        return list((await s.execute(select(L1ConnectionAssignment))).scalars().all())


async def _l3_rows():
    async with TestSessionLocal() as s:
        return list((await s.execute(select(RouteAssignment))).scalars().all())


# --- the concurrent writers ---------------------------------------------------


def _l2_flip_then_release(ports, va_id, release=True):
    """The manual retry flips FAILED to ACTIVE, then a fork save releases (timeline)."""

    async def _run():
        from app.services.l2_membership_service import (
            record_l2_membership_active,
            release_l2_membership,
        )

        async with TestSessionLocal() as s:
            for port in ports:
                await record_l2_membership_active(s, RES_ID, va_id, SW_L2, port)
            if release:
                for port in ports:
                    await release_l2_membership(s, RES_ID, SW_L2, port)

    return _run


def _l1_flip_then_release(release=True):
    async def _run():
        from app.services.l1_assignment_service import record_l1_connect, release_l1_connection

        async with TestSessionLocal() as s:
            await record_l1_connect(s, RES_ID, SW_L1, *L1_PAIR)
            if release:
                await release_l1_connection(s, RES_ID, SW_L1, *L1_PAIR)

    return _run


def _l3_flip_then_release(release=True):
    async def _run():
        from app.services.route_service import record_route_active, release_route_membership

        async with TestSessionLocal() as s:
            await record_route_active(s, RES_ID, SW_L3, PINNED)
            if release:
                await release_route_membership(s, RES_ID, SW_L3)

    return _run


# --- L2: the reported defect --------------------------------------------------


async def test_l2_tick_failure_write_never_resurrects_rows_released_mid_flight():
    """The issue #814 timeline: two FAILED rows, flipped ACTIVE then RELEASED mid-drive.

    The tick's late failure write must land on nothing: exactly the two rows it started
    from remain, both RELEASED, and no FAILED intended-ACTIVE zombie row exists.
    """
    va = await _seed_alloc()
    seeded = {await _seed_l2_failed(PORTS[0], va), await _seed_l2_failed(PORTS[1], va)}

    driver = _driver({"add_to_vlan"}, on_first_fail=_l2_flip_then_release(PORTS, va))
    with _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l2_rows()
    assert len(rows) == 2, f"expected the two seeded rows, got {[(r.port, r.status) for r in rows]}"
    assert {r.id for r in rows} == seeded, "no new row was inserted"
    assert [r.status for r in rows] == ["RELEASED", "RELEASED"]
    assert not [r for r in rows if r.status == "FAILED" and r.intended == "ACTIVE"]


async def test_l2_tick_failure_write_applies_while_the_row_is_still_failed():
    """No concurrent writer: the ordinary repeat failure still lands on the same row."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed(PORTS[0], va, attempts=2)

    with _patches(_driver({"add_to_vlan"})):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l2_rows()
    assert len(rows) == 1 and rows[0].id == rid
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "ACTIVE"
    assert rows[0].attempts == 5, "attempts accumulate onto the loaded row"
    assert rows[0].last_error == f"{DRIVER_ERROR} on add_to_vlan"


async def test_l2_tick_failure_write_no_ops_when_the_row_went_active(caplog):
    """A row a racing writer proved ACTIVE keeps its ACTIVE status and its attempts."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed(PORTS[0], va, attempts=2)

    driver = _driver(
        {"add_to_vlan"}, on_first_fail=_l2_flip_then_release([PORTS[0]], va, release=False)
    )
    with caplog.at_level(logging.WARNING), _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l2_rows()
    assert len(rows) == 1 and rows[0].id == rid
    assert rows[0].status == "ACTIVE"
    assert rows[0].attempts == 2, "a refused write never inflates attempts"
    assert any("a concurrent writer won" in r.getMessage() for r in caplog.records)


# --- L1: the same recorder, the same fix --------------------------------------


async def test_l1_tick_failure_write_never_resurrects_a_row_released_mid_flight():
    seeded = await _seed_l1_failed()

    driver = _driver({"connect_ports"}, on_first_fail=_l1_flip_then_release())
    with _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l1_rows()
    assert len(rows) == 1 and rows[0].id == seeded, "no new row was inserted"
    assert rows[0].status == "RELEASED"


async def test_l1_tick_failure_write_applies_while_the_row_is_still_failed():
    seeded = await _seed_l1_failed(attempts=2)

    with _patches(_driver({"connect_ports"})):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l1_rows()
    assert len(rows) == 1 and rows[0].id == seeded
    assert rows[0].status == "FAILED"
    assert rows[0].attempts == 5


async def test_l1_tick_failure_write_no_ops_when_the_row_went_active():
    seeded = await _seed_l1_failed(attempts=2)

    driver = _driver({"connect_ports"}, on_first_fail=_l1_flip_then_release(release=False))
    with _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l1_rows()
    assert len(rows) == 1 and rows[0].id == seeded
    assert rows[0].status == "ACTIVE"
    assert rows[0].attempts == 2


# --- L3: the same recorder, the same fix --------------------------------------


async def test_l3_tick_failure_write_never_resurrects_a_pin_released_mid_flight():
    seeded = await _seed_l3_failed()

    driver = _driver({"configure_route"}, on_first_fail=_l3_flip_then_release())
    with _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l3_rows()
    assert len(rows) == 1 and rows[0].id == seeded, "no new row was inserted"
    assert rows[0].status == "RELEASED"


async def test_l3_tick_failure_write_applies_while_the_row_is_still_failed():
    seeded = await _seed_l3_failed(attempts=2)

    with _patches(_driver({"configure_route"})):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l3_rows()
    assert len(rows) == 1 and rows[0].id == seeded
    assert rows[0].status == "FAILED"
    assert rows[0].attempts > 2, "attempts accumulate onto the loaded row"


async def test_l3_tick_failure_write_no_ops_when_the_row_went_active():
    seeded = await _seed_l3_failed(attempts=2)

    driver = _driver({"configure_route"}, on_first_fail=_l3_flip_then_release(release=False))
    with _patches(driver):
        await run_wiring_retry_tick(_db_session_factory())

    rows = await _l3_rows()
    assert len(rows) == 1 and rows[0].id == seeded
    assert rows[0].status == "ACTIVE"
    assert rows[0].attempts == 2
