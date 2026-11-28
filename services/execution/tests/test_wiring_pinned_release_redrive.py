"""The admin re-drive of pinned release rows on an ended reservation (issue #1199).

A release-direction wiring row (a disconnect, a VLAN leave, a route removal) recorded
FAILED with a pinned reason while its switch still existed, because the switch's driver
package was broken (issue #1002) or its template was missing, could never be driven
again once its reservation had ended: the tick's selects leave pinned reasons out
(WIRE-RETRY-14), the manual retry reports them `not_retryable` (WIRE-RETRY-4), and a
fork re-save needs an ACTIVE reservation. The connection may still be on the device.

The owner chose option 2 (2026-11-25): a manual-only admin action. These tests pin:

  - the reproduction, through the real teardown at all three layers: a broken package
    pins every release row, and neither existing channel drives them;
  - the re-drive, once the package (or template) is fixed, drives `disconnect_ports`,
    `remove_from_vlan`, and `remove_route` and each row ends RELEASED; a package that
    is still broken records its pin again with no driver call;
  - what stays out: a switch that was not found, an unresolvable hop and a hop set
    that is not a simple chain, an L2 row with no VLAN number, build rows, and release
    rows whose reason is retryable (the tick's work); the eligibility predicate over
    the whole space of reasons, directions, statuses, and layers;
  - the refusal on a reservation whose wiring is not frozen, the idempotency of a second
    call, and two concurrent calls driving one row once (here on SQLite with the second
    call run strictly inside the first one's drive; the database-level race is in
    `test_wiring_retry_claim_race_live_pg.py`);
  - the route: admin only, 409, 503, and the outcomes relayed.

Inventory, the template read, the driver load, and the sandbox are patched; everything
from the ledgers down is the real code against in-memory SQLite.
"""

import asyncio
import threading
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from app.database import Base
from app.models.l1_connection_assignment import L1ConnectionAssignment
from app.models.l2_port_assignment import L2PortAssignment
from app.models.reservation_wiring_state import ReservationWiringState
from app.models.route_assignment import RouteAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.driver_loader import DriverPackageError
from app.services.nats_consumer import (
    WIRING_NOT_SIMPLE_CHAIN_REASON,
    WIRING_PACKAGE_UNLOADABLE_PREFIX,
    WIRING_STALE_BUILD_REASON,
    WIRING_UNRESOLVABLE_REASON,
    TransientUpstreamError,
    _FetchContext,
    _teardown_from_ledgers,
    _wiring_load_failure,
    wiring_template_missing_reason,
)
from app.services.wiring_retry_service import (
    WiringReservationNotFrozen,
    is_redrivable_pinned_release,
    reattempt_reservation,
    redrive_pinned_releases,
    run_wiring_retry_tick,
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


# Fixed ids, never drawn at import: several of them end up in parametrized test ids.
RES_ID = "11990000-0000-4000-8000-000000000001"
DRIVER_ID = "11990000-0000-4000-8000-000000000002"
SW_L1 = "11990000-0000-4000-8000-0000000000a1"
SW_L2 = "11990000-0000-4000-8000-0000000000a2"
SW_L3 = "11990000-0000-4000-8000-0000000000a3"
FABRIC = uuid.UUID("11990000-0000-4000-8000-0000000000f1")
NIL = uuid.UUID(int=0)

TEMPLATE_DATA = {"id": "tmpl-1", "name": "Switch Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}
ROUTES = [{"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"}]
PACKAGE_PIN = f"{WIRING_UNRESOLVABLE_REASON}: driver load failed: DriverPackageError"
ROW_ACTIONS = ("disconnect_ports", "remove_from_vlan", "remove_route", "connect_ports")
SECRET_KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
ROUTE = f"/reservations/{RES_ID}/wiring/redrive-pinned-releases"


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
}


def _recorder():
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        return SUCCESS_RESULT

    return execute_fn, calls


def _row_calls(calls):
    return [a for a in calls if a in ROW_ACTIONS]


def _patches(execute_fn, *, load=None, template=TEMPLATE_DATA, devices=None):
    """Patch inventory and the sandbox. `load` replaces load_driver (an AsyncMock), else
    it returns a path; `template` is what every template read answers (None is a 404);
    `devices` maps a switch id to its record (a missing id reads as a 404)."""
    devices = DEVICES if devices is None else devices

    async def _device(device_id, client=None):
        return devices.get(str(device_id))

    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device)),
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=template)),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires", new=AsyncMock(return_value=[])
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
        patch(
            "app.services.driver_loader.load_driver",
            new=load or AsyncMock(return_value="/tmp/driver"),
        ),
    ]


def _enter(stack, ps):
    for p in ps:
        stack.enter_context(p)


async def _freeze(frozen=True):
    async with TestSessionLocal() as s:
        s.add(ReservationWiringState(reservation_id=uuid.UUID(RES_ID), frozen=frozen))
        await s.commit()


async def _seed_active_every_layer():
    """One ACTIVE row per layer: an L1 pair, an L2 membership on a defined ACTIVE
    allocation, and an L3 pin of one route. Returns the allocation's id."""
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(va)
        await s.flush()
        s.add_all(
            [
                L1ConnectionAssignment(
                    reservation_id=uuid.UUID(RES_ID),
                    switch_device_id=uuid.UUID(SW_L1),
                    port_a="0/0/3",
                    port_b="0/0/4",
                    status="ACTIVE",
                    intended="ACTIVE",
                ),
                L2PortAssignment(
                    reservation_id=uuid.UUID(RES_ID),
                    vlan_assignment_id=va.id,
                    switch_device_id=uuid.UUID(SW_L2),
                    port="0/0/9",
                    status="ACTIVE",
                    intended="ACTIVE",
                ),
                RouteAssignment(
                    reservation_id=uuid.UUID(RES_ID),
                    device_id=uuid.UUID(SW_L3),
                    routes=ROUTES,
                    status="ACTIVE",
                    intended="ACTIVE",
                ),
            ]
        )
        await s.commit()
        return va.id


async def _seed_failed(layer, *, intended="RELEASED", last_error=PACKAGE_PIN, va_id=None, **kw):
    """Seed one FAILED row; L2 gets an ACTIVE allocation of its own unless `va_id`."""
    async with TestSessionLocal() as s:
        if layer == "l1":
            row = L1ConnectionAssignment(
                reservation_id=uuid.UUID(RES_ID),
                switch_device_id=uuid.UUID(kw.get("switch", SW_L1)),
                port_a=kw.get("port_a", "0/0/3"),
                port_b=kw.get("port_b", "0/0/4"),
                status="FAILED",
                intended=intended,
                attempts=kw.get("attempts", 0),
                last_error=last_error,
            )
        elif layer == "l2":
            if va_id is None:
                va = VlanAssignment(reservation_id=uuid.UUID(RES_ID), fabric_id=FABRIC, vlan_id=100)
                s.add(va)
                await s.flush()
                va_id = va.id
            row = L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va_id,
                switch_device_id=uuid.UUID(kw.get("switch", SW_L2)),
                port=kw.get("port", "0/0/9"),
                status="FAILED",
                intended=intended,
                attempts=kw.get("attempts", 0),
                last_error=last_error,
            )
        else:
            row = RouteAssignment(
                reservation_id=uuid.UUID(RES_ID),
                device_id=uuid.UUID(kw.get("switch", SW_L3)),
                routes=ROUTES,
                status="FAILED",
                intended=intended,
                attempts=kw.get("attempts", 0),
                last_error=last_error,
            )
        s.add(row)
        await s.commit()
        return row.id


_MODELS = {"l1": L1ConnectionAssignment, "l2": L2PortAssignment, "l3": RouteAssignment}


async def _row(layer, row_id):
    model = _MODELS[layer]
    async with TestSessionLocal() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalar_one()


async def _all_rows():
    async with TestSessionLocal() as s:
        return {
            layer: (await s.execute(select(model))).scalars().all()
            for layer, model in _MODELS.items()
        }


async def _state(layer, row_id):
    row = await _row(layer, row_id)
    return (row.status, row.intended, row.attempts, row.last_error)


async def _teardown(**patch_kwargs):
    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        await _teardown_from_ledgers(RES_ID, _FetchContext(None), _db_session_factory())
    return calls


async def _redrive(**patch_kwargs):
    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        result = await redrive_pinned_releases(RES_ID, _db_session_factory())
    return result, calls


def _broken():
    return AsyncMock(side_effect=DriverPackageError("broken"))


def _by_layer(result):
    return {r["layer"]: r for r in result["results"]}


# --- the reproduction --------------------------------------------------------


async def test_a_broken_package_at_teardown_pins_every_release_row_and_no_channel_drives_it():
    """The defect as it stood: the terminal teardown, with the switches' package broken,
    records every release FAILED intended RELEASED under the pinned reason; afterwards
    the tick excludes them and the manual retry reports them not_retryable, with no
    driver call either way."""
    await _seed_active_every_layer()
    await _freeze()
    assert _row_calls(await _teardown(load=_broken())) == []

    rows = await _all_rows()
    for layer in ("l1", "l2", "l3"):
        (row,) = rows[layer]
        assert (row.status, row.intended, row.last_error) == ("FAILED", "RELEASED", PACKAGE_PIN)
        assert is_redrivable_pinned_release(row, layer)

    execute_fn, calls = _recorder()
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stats = await run_wiring_retry_tick(_db_session_factory())
        manual = await reattempt_reservation(RES_ID, _db_session_factory())
    assert stats["rows_due"] == 0
    assert stats["excluded_not_retryable"] == 3
    assert {r["outcome"] for r in manual["results"]} == {"not_retryable"}
    assert calls == []


# --- the re-drive --------------------------------------------------------------


async def test_once_the_package_is_fixed_the_redrive_removes_every_layers_release():
    """The fix: after the teardown pinned the three releases, the re-drive with a
    working package drives one removal per layer, and every row ends RELEASED; the
    emptied allocation is released by the frozen reservation's allocation check."""
    va_id = await _seed_active_every_layer()
    await _freeze()
    await _teardown(load=_broken())

    result, calls = await _redrive()

    assert sorted(_row_calls(calls)) == ["disconnect_ports", "remove_from_vlan", "remove_route"]
    outcomes = _by_layer(result)
    assert {layer: o["outcome"] for layer, o in outcomes.items()} == {
        "l1": "released",
        "l2": "released",
        "l3": "released",
    }
    rows = await _all_rows()
    assert {layer: rows[layer][0].status for layer in rows} == {
        "l1": "RELEASED",
        "l2": "RELEASED",
        "l3": "RELEASED",
    }
    async with TestSessionLocal() as s:
        va = (
            await s.execute(select(VlanAssignment).where(VlanAssignment.id == va_id))
        ).scalar_one()
    assert va.status == "RELEASED"


async def test_a_package_still_broken_records_the_pin_again_and_drives_nothing():
    """A re-drive while the package is still broken makes no driver call and leaves each
    row FAILED with the same pinned reason and attempts (a pinned load failure counts
    none), reported still_failed; a later re-drive with the package fixed removes them."""
    await _seed_active_every_layer()
    await _freeze()
    await _teardown(load=_broken())
    before = {layer: await _state(layer, r[0].id) for layer, r in (await _all_rows()).items()}

    result, calls = await _redrive(load=_broken())

    assert calls == []
    assert {o["outcome"] for o in result["results"]} == {"still_failed"}
    after = {layer: await _state(layer, r[0].id) for layer, r in (await _all_rows()).items()}
    assert after == before

    result, calls = await _redrive()
    assert {o["outcome"] for o in result["results"]} == {"released"}


async def test_a_missing_template_at_teardown_is_redriven_once_the_template_is_back():
    """The other switch-level pin: the template read answered 404 during the teardown.
    The reason each layer records is exactly `wiring_template_missing_reason` for the
    row's own switch, which the re-drive matches, and the removal runs once the
    template is back."""
    await _seed_active_every_layer()
    await _freeze()
    assert _row_calls(await _teardown(template=None)) == []

    rows = await _all_rows()
    switch = {"l1": SW_L1, "l2": SW_L2, "l3": SW_L3}
    for layer in ("l1", "l2", "l3"):
        (row,) = rows[layer]
        assert row.last_error == wiring_template_missing_reason(layer, switch[layer])
        assert (row.status, row.intended) == ("FAILED", "RELEASED")

    result, calls = await _redrive()
    assert sorted(_row_calls(calls)) == ["disconnect_ports", "remove_from_vlan", "remove_route"]
    assert {o["outcome"] for o in result["results"]} == {"released"}


async def test_a_second_call_after_success_finds_nothing_to_do():
    """Idempotent: a released row is not FAILED, so the next call drives nothing."""
    await _freeze()
    for layer in ("l1", "l2", "l3"):
        await _seed_failed(layer)
    first, calls = await _redrive()
    assert len(_row_calls(calls)) == 3
    assert {o["outcome"] for o in first["results"]} == {"released"}

    second, calls = await _redrive()
    assert second == {"reservation_id": RES_ID, "results": []}
    assert calls == []


# --- what stays out --------------------------------------------------------------


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_release_row_whose_switch_was_not_found_is_never_driven(layer):
    """A switch that was not found is the one pinned load reason the re-drive leaves
    alone: the row is not in the results, no inventory read is made for it, and it is
    unchanged."""
    label = {"l1": "switch", "l2": "L2 switch", "l3": "L3 switch"}[layer]
    switch = {"l1": SW_L1, "l2": SW_L2, "l3": SW_L3}[layer]
    reason = f"{WIRING_UNRESOLVABLE_REASON}: {label} {switch} not found"
    await _freeze()
    row_id = await _seed_failed(layer, last_error=reason)
    before = await _state(layer, row_id)

    result, calls = await _redrive()

    assert result["results"] == []
    assert calls == []
    assert await _state(layer, row_id) == before


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_switch_deleted_since_the_pin_is_recorded_not_found_with_no_driver_call(layer):
    """A row pinned by a missing template whose switch has been deleted since: the
    re-drive reads the switch fresh, finds it gone (404), and records the not-found pin
    by row id with no driver call; the row then stays out of later calls."""
    switch = {"l1": SW_L1, "l2": SW_L2, "l3": SW_L3}[layer]
    await _freeze()
    row_id = await _seed_failed(layer, last_error=wiring_template_missing_reason(layer, switch))

    result, calls = await _redrive(devices={})

    assert calls == []
    assert [o["outcome"] for o in result["results"]] == ["still_failed"]
    status, intended, _attempts, last_error = await _state(layer, row_id)
    assert (status, intended) == ("FAILED", "RELEASED")
    assert last_error.startswith(WIRING_UNRESOLVABLE_REASON)
    assert last_error.endswith(f"{switch} not found")
    assert "template" not in last_error

    again, calls = await _redrive()
    assert again["results"] == []
    assert calls == []


async def test_build_rows_retryable_releases_and_unresolvable_hops_are_left_alone():
    """Only the pinned release rows are driven. A pinned build row, a release row with a
    retryable reason (the tick's work), an unresolvable hop row, and a not-a-simple-chain
    row of the same reservation are neither driven nor reported, and stay as they were."""
    await _freeze()
    redrivable = await _seed_failed("l1")
    others = [
        await _seed_failed("l1", intended="ACTIVE", port_a="0/0/5", port_b="0/0/6"),
        await _seed_failed("l1", last_error="driver timeout", port_a="0/0/7", port_b="0/0/8"),
        await _seed_failed(
            "l1", last_error=WIRING_UNRESOLVABLE_REASON, port_a="eth0", port_b="eth0"
        ),
        await _seed_failed(
            "l1", last_error=WIRING_NOT_SIMPLE_CHAIN_REASON, port_a="0/0/11", port_b="0/0/12"
        ),
    ]
    before = {rid: await _state("l1", rid) for rid in others}

    result, calls = await _redrive()

    assert [o["id"] for o in result["results"]] == [str(redrivable)]
    assert _row_calls(calls) == ["disconnect_ports"]
    assert {rid: await _state("l1", rid) for rid in others} == before


@pytest.mark.parametrize("allocation", ["nil", "dangling"])
async def test_an_l2_release_with_no_vlan_number_is_left_alone(allocation):
    """An L2 row whose allocation is the nil placeholder or no longer exists has no
    number a remove_from_vlan could name, so the re-drive leaves it out."""
    await _freeze()
    va_id = NIL if allocation == "nil" else uuid.UUID("11990000-0000-4000-8000-0000000000d1")
    row_id = await _seed_failed("l2", va_id=va_id)
    before = await _state("l2", row_id)

    result, calls = await _redrive()

    assert result["results"] == []
    assert calls == []
    assert await _state("l2", row_id) == before


@pytest.mark.parametrize("frozen", [None, False])
async def test_a_reservation_whose_wiring_is_not_frozen_is_refused(frozen):
    """No wiring state, or a live one: the call raises before any read of a switch, and
    the pinned row is left as it was. A live reservation's recovery is a re-save."""
    if frozen is not None:
        await _freeze(frozen)
    row_id = await _seed_failed("l1")
    before = await _state("l1", row_id)

    execute_fn, calls = _recorder()
    device = AsyncMock(side_effect=AssertionError("no inventory read"))
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stack.enter_context(patch("app.services.nats_consumer._fetch_device", new=device))
        with pytest.raises(WiringReservationNotFrozen):
            await redrive_pinned_releases(RES_ID, _db_session_factory())

    assert calls == []
    assert await _state("l1", row_id) == before


# --- the eligibility predicate, over its whole space -------------------------------


def _row_like(layer, status, intended, last_error, switch):
    class _R:
        pass

    r = _R()
    r.status, r.intended, r.last_error = status, intended, last_error
    if layer == "l3":
        r.device_id = uuid.UUID(switch)
    else:
        r.switch_device_id = uuid.UUID(switch)
    return r


_OWN = {"l1": SW_L1, "l2": SW_L2, "l3": SW_L3}
_OTHER_SWITCH = "11990000-0000-4000-8000-0000000000b9"


def _reasons(layer):
    """(reason, redrivable when FAILED and intended RELEASED) for one layer."""
    own = _OWN[layer]
    other_layer = {"l1": "l2", "l2": "l3", "l3": "l1"}[layer]
    label = {"l1": "switch", "l2": "L2 switch", "l3": "L3 switch"}[layer]
    return [
        (PACKAGE_PIN, True),
        (f"{WIRING_PACKAGE_UNLOADABLE_PREFIX}BadZipFile", True),
        (f"{WIRING_PACKAGE_UNLOADABLE_PREFIX}OSError", True),
        (wiring_template_missing_reason(layer, own), True),
        (wiring_template_missing_reason(layer, _OTHER_SWITCH), False),
        (wiring_template_missing_reason(other_layer, own), False),
        (f"{WIRING_UNRESOLVABLE_REASON}: {label} {own} not found", False),
        (WIRING_UNRESOLVABLE_REASON, False),
        (WIRING_NOT_SIMPLE_CHAIN_REASON, False),
        (f"{WIRING_UNRESOLVABLE_REASON}: no VLAN allocation for fabric", False),
        (f"create_vlan failed: {PACKAGE_PIN}", False),
        ("driver load failed: DriverPackageError", False),
        ("driver load failed: ConnectError", False),
        (WIRING_STALE_BUILD_REASON, False),
        ("driver timeout", False),
        ("", False),
        (None, False),
    ]


_SPACE = [
    (layer, status, intended, reason, expected and status == "FAILED" and intended == "RELEASED")
    for layer in ("l1", "l2", "l3")
    for status in ("FAILED", "ACTIVE", "RELEASED")
    for intended in ("RELEASED", "ACTIVE")
    for reason, expected in _reasons(layer)
]


@pytest.mark.parametrize(("layer", "status", "intended", "reason", "expected"), _SPACE)
def test_the_redrive_predicate_over_every_reason_direction_status_and_layer(
    layer, status, intended, reason, expected
):
    """Exactly a FAILED intended-RELEASED row pinned by a broken package, or by a
    missing template of its own switch at its own layer, is re-drivable."""
    row = _row_like(layer, status, intended, reason, _OWN[layer])
    assert is_redrivable_pinned_release(row, layer) is expected


@pytest.mark.parametrize(
    "exc",
    [
        DriverPackageError("broken"),
        DriverPackageError("bad zip"),
        RuntimeError("download failed"),
        OSError("cache table unreadable"),
    ],
)
def test_every_permanent_load_failure_reason_is_redrivable_and_no_transient_one_is(exc):
    """The producer and the predicate agree: whatever cause a broken package chains, the
    reason `_wiring_load_failure` writes for it is re-drivable, and a transient load
    failure's reason (retryable, the tick's work) is not."""
    if isinstance(exc, DriverPackageError) and str(exc) == "bad zip":
        try:
            try:
                raise ValueError("not a zip")
            except ValueError as cause:
                raise exc from cause
        except DriverPackageError as chained:
            exc = chained
    reason, _attempts = _wiring_load_failure(exc, SW_L1)
    row = _row_like("l1", "FAILED", "RELEASED", reason, SW_L1)
    assert is_redrivable_pinned_release(row, "l1") is isinstance(exc, DriverPackageError)


# --- two calls at once ----------------------------------------------------------------


_REMOVAL = {"l1": "disconnect_ports", "l2": "remove_from_vlan", "l3": "remove_route"}


@pytest.mark.parametrize("layer", ["l1", "l2", "l3"])
async def test_a_second_call_inside_the_first_ones_drive_reports_in_progress(layer):
    """Two calls for one row: the second runs strictly while the first is inside the
    row's driver call (its claim committed), finds the row claimed up front, reports it
    in_progress, and drives nothing, reading no switch from inventory either; the
    driver is called once in all."""
    await _freeze()
    row_id = await _seed_failed(layer)

    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def execute_fn(driver_path, action, context, **kwargs):
        calls.append(action)
        if action == _REMOVAL[layer]:
            entered.set()
            assert release.wait(30), "the test never released the first call"
        return SUCCESS_RESULT

    async def _device_read(device_id, client=None):
        return DEVICES.get(str(device_id))

    device = AsyncMock(side_effect=_device_read)
    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stack.enter_context(patch("app.services.nats_consumer._fetch_device", new=device))
        first = asyncio.create_task(redrive_pinned_releases(RES_ID, _db_session_factory()))
        try:
            assert await asyncio.to_thread(entered.wait, 30), "the first call never drove"
            reads_before = device.await_count
            second = await redrive_pinned_releases(RES_ID, _db_session_factory())
            second_reads = device.await_count - reads_before
            first_done_early = first.done()
        finally:
            release.set()
            first_result = await asyncio.wait_for(first, 30)

    assert not first_done_early
    assert [(o["id"], o["outcome"]) for o in second["results"]] == [(str(row_id), "in_progress")]
    assert second_reads == 0, "the second call must not read the switch of a held row"
    assert [(o["id"], o["outcome"]) for o in first_result["results"]] == [(str(row_id), "released")]
    assert _row_calls(calls) == [_REMOVAL[layer]]


# --- the route ---------------------------------------------------------------------


def _auth(role="admin"):
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "role": role, "username": "probe"},
        SECRET_KEY,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def _post(headers, execute_fn, **patch_kwargs):
    from app.main import app
    from app.routers import executions as ex_router

    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn, **patch_kwargs))
        stack.enter_context(
            patch.object(ex_router, "make_session_ctx_factory", _db_session_factory)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(ROUTE, headers=headers)


@pytest.mark.parametrize("role", ["admin", "superadmin"])
async def test_the_route_drives_the_rows_for_an_admin_and_relays_the_outcomes(role):
    await _freeze()
    row_id = await _seed_failed("l1")
    execute_fn, calls = _recorder()

    resp = await _post(_auth(role), execute_fn)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reservation_id"] == RES_ID
    (result,) = body["results"]
    assert (result["id"], result["layer"], result["outcome"], result["status"]) == (
        str(row_id),
        "l1",
        "released",
        "RELEASED",
    )
    assert _row_calls(calls) == ["disconnect_ports"]


@pytest.mark.parametrize(
    ("headers", "status", "detail"),
    [
        (None, 401, "Not authenticated"),
        ("user", 403, "Admin or superadmin role required"),
    ],
)
async def test_the_route_refuses_anyone_but_an_admin(headers, status, detail):
    """No token is 401, a user's token is 403; neither reads or drives anything."""
    await _freeze()
    row_id = await _seed_failed("l1")
    before = await _state("l1", row_id)
    execute_fn, calls = _recorder()

    resp = await _post(_auth(headers) if headers else {}, execute_fn)

    assert resp.status_code == status
    assert resp.json()["detail"] == detail
    assert calls == []
    assert await _state("l1", row_id) == before


async def test_the_route_answers_409_on_a_live_reservation():
    from app.routers.executions import REDRIVE_NOT_ENDED_DETAIL

    await _freeze(False)
    row_id = await _seed_failed("l1")
    execute_fn, calls = _recorder()

    resp = await _post(_auth(), execute_fn)

    assert resp.status_code == 409
    assert resp.json()["detail"] == REDRIVE_NOT_ENDED_DETAIL
    assert REDRIVE_NOT_ENDED_DETAIL == (
        "Reservation wiring is not frozen; pinned release rows are re-driven only after "
        "the reservation has ended"
    )
    assert calls == []
    assert (await _state("l1", row_id))[0] == "FAILED"


async def test_the_route_answers_503_when_an_inventory_read_fails():
    await _freeze()
    await _seed_failed("l1")
    execute_fn, calls = _recorder()
    failing = AsyncMock(side_effect=TransientUpstreamError("inventory device: HTTP 503"))

    from app.main import app
    from app.routers import executions as ex_router

    with ExitStack() as stack:
        _enter(stack, _patches(execute_fn))
        stack.enter_context(patch("app.services.nats_consumer._fetch_device", new=failing))
        stack.enter_context(
            patch.object(ex_router, "make_session_ctx_factory", _db_session_factory)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(ROUTE, headers=_auth())

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Upstream service unavailable: inventory device: HTTP 503"
    assert calls == []
