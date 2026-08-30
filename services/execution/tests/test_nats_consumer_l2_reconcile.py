"""Consumer tests for the L2 layered membership reconcile (ADR 0009 phase 4, issue #416).

Covers the option-C membership derivation (a hop terminating on a Layer 2 Switch joins
that port; an inter-switch trunk contributes nothing; a hop reaching an L2 switch THROUGH
an L1 switch joins only the L2 side), the full-reconcile move-a-port case, the
allocation lifecycle coupling (first membership allocates, last release frees the VLAN),
and result-gated ledger writes, all driven through handle_wiring_changed with an
in-memory SQLite DB and a mocked sandbox, the pattern the L1 wiring_changed tests use.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.l2_port_assignment import L2PortAssignment
from app.models.vlan_assignment import VlanAssignment
from app.services.nats_consumer import (
    _apply_l2_memberships,
    _derive_l2_memberships,
    _FetchContext,
    handle_wiring_changed,
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


SW_L2 = str(uuid.uuid4())
SW_L2_B = str(uuid.uuid4())
SW_L1 = str(uuid.uuid4())
DUT1 = str(uuid.uuid4())
DUT2 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
FABRIC = uuid.uuid4()

TEMPLATE_DATA = {"id": "tmpl-1", "name": "L2 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}


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
    SW_L2: _dev(SW_L2, "Layer 2 Switch", "L2-A"),
    SW_L2_B: _dev(SW_L2_B, "Layer 2 Switch", "L2-B"),
    SW_L1: _dev(SW_L1, "Layer 1 Switch", "L1"),
    DUT1: _dev(DUT1, "Server", "dut1"),
    DUT2: _dev(DUT2, "Server", "dut2"),
}


def _wire(device_a, port_a, device_b, port_b):
    return {
        "device_a_id": device_a,
        "port_a": port_a,
        "device_b_id": device_b,
        "port_b": port_b,
        "layer": "L1",
        "physical_connection_id": str(uuid.uuid4()),
        "edge_key": None,
    }


async def _device_fetch(device_id, client=None):
    return DEVICES.get(str(device_id))


def _l2_recorder(fail=None):
    """(execute_fn, calls): calls records (action, port, vlan_id). `fail` fails matches.

    `fail` is a set of (action, port) tuples whose sandbox op returns success=False.
    """
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        port = mk.get("port")
        vlan_id = mk.get("vlan_id")
        calls.append((action, port, vlan_id))
        if (action, port) in fail:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn, fork_wires):
    return [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=fork_wires),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]


async def _reconcile(fork_wires, fork_version=1, execute_fn=None, calls=None):
    if execute_fn is None:
        execute_fn, calls = _l2_recorder()
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in _patches(execute_fn, fork_wires):
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": fork_version},
            _db_session_factory(),
        )
    return calls


async def _active_memberships():
    async with TestSessionLocal() as s:
        rows = (
            (await s.execute(select(L2PortAssignment).where(L2PortAssignment.status == "ACTIVE")))
            .scalars()
            .all()
        )
        return {(str(r.switch_device_id), r.port): r.vlan_assignment_id for r in rows}


# --- derivation (option C) ---


async def test_derive_dut_to_l2_joins_switch_port():
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l2_memberships([_wire(DUT1, "eth0", SW_L2, "0/0/1")], ctx)
    assert m == {(SW_L2, "0/0/1")}


async def test_derive_l2_to_l2_trunk_contributes_nothing():
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l2_memberships([_wire(SW_L2, "0/0/9", SW_L2_B, "0/0/9")], ctx)
    assert m == set(), "an inter-switch trunk is assumed provisioned, no membership"


async def test_derive_through_l1_joins_only_l2_side():
    """DUT - L1 - L2: two hops; only the L2-side terminal port joins."""
    wires = [_wire(DUT1, "eth0", SW_L1, "1/0/1"), _wire(SW_L1, "1/0/2", SW_L2, "0/0/1")]
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l2_memberships(wires, ctx)
    assert m == {(SW_L2, "0/0/1")}


async def test_derive_dedups_shared_membership():
    """Two hops implying the same (switch, port) collapse to one membership; the full
    reconcile diffs the SET, so releasing one hop cannot remove a still-implied member."""
    wires = [_wire(DUT1, "eth0", SW_L2, "0/0/1"), _wire(DUT2, "eth1", SW_L2, "0/0/1")]
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l2_memberships(wires, ctx)
    assert m == {(SW_L2, "0/0/1")}


# --- reconcile through handle_wiring_changed ---


async def test_reconcile_joins_member_and_allocates_vlan():
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")])
    actions = [(a, p) for a, p, _v in calls]
    assert ("add_to_vlan", "0/0/1") in actions
    active = await _active_memberships()
    assert (SW_L2, "0/0/1") in active
    async with TestSessionLocal() as s:
        vas = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE")))
            .scalars()
            .all()
        )
    assert len(vas) == 1, "the fabric's VLAN was allocated on the first membership"


async def test_reconcile_move_a_port_moves_membership_same_vlan():
    """Re-wire the DUT to a different port on the same switch: old port out, new in, same VLAN."""
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    active1 = await _active_memberships()
    vlan_before = active1[(SW_L2, "0/0/1")]

    calls = await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/2")], fork_version=2)
    actions = [(a, p) for a, p, _v in calls]
    assert ("remove_from_vlan", "0/0/1") in actions
    assert ("add_to_vlan", "0/0/2") in actions

    active2 = await _active_memberships()
    assert (SW_L2, "0/0/1") not in active2, "old port left the VLAN"
    assert (SW_L2, "0/0/2") in active2, "new port joined the VLAN"
    assert active2[(SW_L2, "0/0/2")] == vlan_before, "same fabric allocation reused"


async def test_reconcile_last_release_frees_allocation():
    """Removing the last membership frees the fabric's VLAN allocation (Decision 4)."""
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    # A save that drops the connection: intended set empty -> the membership leaves.
    await _reconcile([], fork_version=2)
    active = await _active_memberships()
    assert active == {}
    async with TestSessionLocal() as s:
        active_vas = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE")))
            .scalars()
            .all()
        )
    assert active_vas == [], "the allocation was released when its last member left"


async def test_reconcile_move_within_fabric_keeps_allocation():
    """A port move (remove + add in one reconcile) must NOT free the allocation mid-pass."""
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fork_version=1)
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/2")], fork_version=2)
    async with TestSessionLocal() as s:
        active_vas = (
            (await s.execute(select(VlanAssignment).where(VlanAssignment.status == "ACTIVE")))
            .scalars()
            .all()
        )
    assert len(active_vas) == 1, "the fabric keeps its allocation across a port move"


# --- result gating ---


async def test_reconcile_failed_add_lands_failed_not_active():
    execute_fn, calls = _l2_recorder(fail={("add_to_vlan", "0/0/1")})
    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=calls)
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "ACTIVE"
    active = await _active_memberships()
    assert active == {}, "a driver-reported failure never records an ACTIVE membership"


async def test_reconcile_present_key_falsy_result_is_failure():
    """A driver returning success=False (transport ok) is a failure (issue #393)."""

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "add_to_vlan":
            # transport succeeded, driver reported failure in the payload
            return {"success": True, "output": {"success": False}, "error": None, "duration_ms": 1}
        return SUCCESS_RESULT

    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=[])
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    assert [r.status for r in rows] == ["FAILED"]


async def test_reconcile_bare_data_output_stays_success():
    """A driver returning bare data (no success key in the output) is a success: the
    absent-key rule of driver_result_failed, the L2 twin of the L3 pin (issue #393)."""

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "add_to_vlan":
            # Bare-data output: no "success" key at all. Must stay a success.
            return {"success": True, "output": {"vlan": 123}, "error": None, "duration_ms": 1}
        return SUCCESS_RESULT

    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=[])
    assert (SW_L2, "0/0/1") in await _active_memberships()


# --- allocation grouping by fabric (re-expressed from the retired legacy pins
# --- test_assign_vlans_groups_switches_by_fabric and
# --- test_assign_vlans_uses_fallback_when_fabric_lookup_fails) ---


async def _reconcile_with_fabric(fork_wires, fabric_fetch):
    from contextlib import ExitStack

    execute_fn, calls = _l2_recorder()
    patches = _patches(execute_fn, fork_wires)[:-1] + [
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(side_effect=fabric_fetch))
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": 1},
            _db_session_factory(),
        )
    return calls


async def test_allocation_groups_switches_by_fabric():
    """Two L2 switches in DIFFERENT fabrics get one allocation each; memberships key on
    their own fabric's allocation."""
    fabric_a, fabric_b = uuid.uuid4(), uuid.uuid4()
    fabric_of = {SW_L2: fabric_a, SW_L2_B: fabric_b}

    async def fabric_fetch(switch_id):
        return fabric_of[str(switch_id)]

    await _reconcile_with_fabric(
        [_wire(DUT1, "eth0", SW_L2, "0/0/1"), _wire(DUT2, "eth0", SW_L2_B, "0/0/2")],
        fabric_fetch,
    )

    async with TestSessionLocal() as s:
        allocations = (await s.execute(select(VlanAssignment))).scalars().all()
    assert {a.fabric_id for a in allocations} == {fabric_a, fabric_b}
    memberships = await _active_memberships()
    alloc_by_fabric = {a.fabric_id: a.id for a in allocations}
    assert memberships[(SW_L2, "0/0/1")] == alloc_by_fabric[fabric_a]
    assert memberships[(SW_L2_B, "0/0/2")] == alloc_by_fabric[fabric_b]


async def test_allocation_shares_one_vlan_within_a_fabric():
    """Two L2 switches in the SAME fabric share one allocation (one VLAN number)."""

    async def fabric_fetch(switch_id):
        return FABRIC

    await _reconcile_with_fabric(
        [_wire(DUT1, "eth0", SW_L2, "0/0/1"), _wire(DUT2, "eth0", SW_L2_B, "0/0/2")],
        fabric_fetch,
    )

    async with TestSessionLocal() as s:
        allocations = (await s.execute(select(VlanAssignment))).scalars().all()
    assert len(allocations) == 1
    memberships = await _active_memberships()
    assert set(memberships.values()) == {allocations[0].id}


async def test_allocation_falls_back_when_fabric_lookup_fails():
    """A switch whose fabric cannot be determined still allocates, under a deterministic
    per-switch fallback fabric id (uuid5 of the switch id), so provisioning never blocks
    on the fabric endpoint."""

    async def fabric_fetch(switch_id):
        return None

    await _reconcile_with_fabric([_wire(DUT1, "eth0", SW_L2, "0/0/1")], fabric_fetch)

    async with TestSessionLocal() as s:
        allocations = (await s.execute(select(VlanAssignment))).scalars().all()
    assert len(allocations) == 1
    assert allocations[0].fabric_id == uuid.uuid5(uuid.NAMESPACE_DNS, SW_L2)
    assert (SW_L2, "0/0/1") in await _active_memberships()


# --- Stale-intent settlement in the full reconcile (issue #491) ---


async def _seed_alloc(vlan_id=100):
    async with TestSessionLocal() as s:
        va = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=vlan_id,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[],
            status="ACTIVE",
        )
        s.add(va)
        await s.commit()
        await s.refresh(va)
        return va.id


async def _seed_l2_failed(port, va_id, *, intended="ACTIVE", attempts=2, err="boom"):
    async with TestSessionLocal() as s:
        row = L2PortAssignment(
            reservation_id=uuid.UUID(RES_ID),
            vlan_assignment_id=va_id,
            switch_device_id=uuid.UUID(SW_L2),
            port=port,
            status="FAILED",
            intended=intended,
            attempts=attempts,
            last_error=err,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _l2_rows(status=None):
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    return [r for r in rows if status is None or r.status == status]


async def test_reconcile_settles_stale_failed_join_through_remove():
    """A FAILED intended-ACTIVE membership whose (switch, port) left the intended set
    rides the remove path: remove_from_vlan drives with the row's recorded VLAN and the
    row lands RELEASED (issue #491)."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/7", va)
    calls = await _reconcile([])

    assert ("remove_from_vlan", "0/0/7", 100) in calls
    assert not any(a == "add_to_vlan" for a, _p, _v in calls), "a stale join must never be rebuilt"
    released = await _l2_rows("RELEASED")
    assert [r.id for r in released] == [rid], "the same row settled RELEASED in place"
    assert await _l2_rows("FAILED") == []


async def test_reconcile_still_rebuilds_failed_join_that_is_still_intended():
    """The asymmetry pin (issue #491): the remove diff is widened with FAILED
    intended-ACTIVE rows, but the ADD diff keeps comparing intended against ACTIVE rows
    only, so a failed join whose port is STILL intended is rebuilt, never removed."""
    va = await _seed_alloc()
    rid = await _seed_l2_failed("0/0/1", va)
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")])

    actions = [(a, p) for a, p, _v in calls]
    assert ("add_to_vlan", "0/0/1") in actions
    assert ("remove_from_vlan", "0/0/1") not in actions
    active = await _l2_rows("ACTIVE")
    assert [r.id for r in active] == [rid], "the FAILED row flipped ACTIVE in place"
    assert await _l2_rows("FAILED") == []


async def test_reconcile_stale_nil_allocation_join_flips_released_with_no_driver_call():
    """A stale FAILED intended-ACTIVE row holding the nil-UUID allocation placeholder
    has no VLAN number to drive remove_from_vlan against, and the nil allocation proves
    the build never reached the driver, so it flips RELEASED directly (issue #491)."""
    rid = await _seed_l2_failed(
        "0/0/7", uuid.UUID(int=0), attempts=0, err="recorded hop unresolvable: no VLAN allocation"
    )
    calls = await _reconcile([])

    assert calls == [], "no driver call may settle a nil-allocation stale join"
    released = await _l2_rows("RELEASED")
    assert [r.id for r in released] == [rid]
    assert await _l2_rows("FAILED") == []


# --- switch cannot be driven: parks adds and still-live removes FAILED --------


async def test_apply_l2_memberships_switch_not_found_parks_adds_and_removes_failed():
    """A switch that does not resolve in inventory parks every add on it FAILED
    intended ACTIVE and every still-live remove FAILED intended RELEASED, tagged with
    the pinned unresolvable reason, without any driver call.

    Both live callers of _apply_l2_memberships (the wiring_changed reconcile and the
    retry channel) pre-filter an unresolvable switch out of `adds` before this
    function ever sees it (derivation classifies the endpoint device first), so this
    exercises the function's own defensive branch directly rather than trying to
    contrive an unreachable end-to-end path.
    """
    gone_switch = str(uuid.uuid4())
    # Scoped ONLY to gone_switch (unlike _seed_alloc's default [SW_L2]): the vlan
    # define pre-pass and orphan-release tail _apply_l2_memberships itself runs
    # would otherwise drive real calls against SW_L2, muddying the "no driver call"
    # assertion below with activity unrelated to this switch's own resolution.
    async with TestSessionLocal() as s:
        va_row = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[gone_switch],
            defined_switch_ids=[],
            status="ACTIVE",
        )
        s.add(va_row)
        await s.commit()
        await s.refresh(va_row)
        va = va_row.id
        # membership_needs_remove only parks a remove that is believed live; seed
        # the pre-existing ACTIVE row the remove is meant to tear down.
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va,
                switch_device_id=uuid.UUID(gone_switch),
                port="0/0/9",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()

    async def device_fetch(device_id, client=None):
        return None if str(device_id) == gone_switch else DEVICES.get(str(device_id))

    execute_fn, calls = _l2_recorder()
    ctx = _FetchContext(None)
    with (
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ):
        await _apply_l2_memberships(
            RES_ID,
            [
                {
                    "switch_device_id": gone_switch,
                    "port": "0/0/9",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                }
            ],
            [
                {
                    "switch_device_id": gone_switch,
                    "port": "0/0/1",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                }
            ],
            ctx,
            _db_session_factory(),
        )

    assert calls == [], "an unresolvable switch must never be driven"
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    by_port = {r.port: r for r in rows}
    assert by_port["0/0/1"].status == "FAILED"
    assert by_port["0/0/1"].intended == "ACTIVE"
    assert "L2 switch" in by_port["0/0/1"].last_error
    assert "not found" in by_port["0/0/1"].last_error
    assert by_port["0/0/9"].status == "FAILED"
    assert by_port["0/0/9"].intended == "RELEASED"
    assert "L2 switch" in by_port["0/0/9"].last_error
    assert "not found" in by_port["0/0/9"].last_error


async def test_reconcile_template_not_found_parks_add_failed():
    """A switch that resolves but whose template lookup 404s is the same park path,
    tagged with the template-not-found variant of the pinned reason."""
    from contextlib import ExitStack

    execute_fn, calls = _l2_recorder()
    patches = [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=None)),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=[_wire(DUT1, "eth0", SW_L2, "0/0/1")]),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": 1},
            _db_session_factory(),
        )

    assert calls == [], "a missing template must never be driven"
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    assert rows[0].status == "FAILED"
    assert "template for L2 switch" in rows[0].last_error
    assert "not found" in rows[0].last_error


async def test_reconcile_driver_load_raises_parks_add_failed_no_driver_call():
    """A load_driver exception (corrupt cache, package missing) parks the add FAILED
    with the pinned reason wrapping the load error, and fires no driver call."""
    from contextlib import ExitStack

    execute_fn, calls = _l2_recorder()
    patches = [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch(
            "app.services.driver_loader.load_driver",
            new=AsyncMock(side_effect=RuntimeError("package corrupt")),
        ),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=[_wire(DUT1, "eth0", SW_L2, "0/0/1")]),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": 1},
            _db_session_factory(),
        )

    assert calls == [], "a driver that cannot load must never be driven"
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    assert rows[0].status == "FAILED"
    assert "driver load failed" in rows[0].last_error
    assert "package corrupt" in rows[0].last_error


async def test_reconcile_login_failure_parks_add_and_remove_failed_no_port_ops():
    """A per-switch login failure parks every pair on that switch FAILED, tagged with
    its direction, and no add_to_vlan/remove_from_vlan op ever reaches the driver
    (only the login attempt does)."""
    va = await _seed_alloc()
    remove_id = await _seed_l2_failed("0/0/9", va, attempts=0, err="stale")
    async with TestSessionLocal() as s:
        row = await s.get(L2PortAssignment, remove_id)
        row.status = "ACTIVE"
        row.last_error = None
        await s.commit()

    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        calls.append((action, mk.get("port"), mk.get("vlan_id")))
        if action == "login":
            return {"success": False, "output": None, "error": "auth failed", "duration_ms": 1}
        return SUCCESS_RESULT

    await _reconcile([_wire(DUT1, "eth0", SW_L2, "0/0/1")], execute_fn=execute_fn, calls=calls)

    assert [c for c in calls if c[0] in ("add_to_vlan", "remove_from_vlan")] == [], (
        "no port op may fire when login fails"
    )
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    by_port = {r.port: r for r in rows}
    assert by_port["0/0/1"].status == "FAILED"
    assert by_port["0/0/1"].intended == "ACTIVE"
    assert "driver login failed" in by_port["0/0/1"].last_error
    assert "auth failed" in by_port["0/0/1"].last_error
    assert by_port["0/0/9"].status == "FAILED"
    assert by_port["0/0/9"].intended == "RELEASED"
    assert "driver login failed" in by_port["0/0/9"].last_error


async def test_reconcile_no_allocation_for_fabric_parks_add_failed_no_driver_call():
    """When _resolve_add_allocations cannot resolve a VLAN allocation for a switch's
    fabric (an upstream fabric lookup or allocation failure), the join is parked
    FAILED intended ACTIVE with the pinned unresolvable reason, and the driver is
    never called for it: the retry channel is the recovery path, not this pass."""
    from contextlib import ExitStack

    execute_fn, calls = _l2_recorder()
    patches = [
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._fetch_fork_intended_wires",
            new=AsyncMock(return_value=[_wire(DUT1, "eth0", SW_L2, "0/0/1")]),
        ),
        patch(
            "app.services.nats_consumer._resolve_add_allocations",
            new=AsyncMock(return_value={}),
        ),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": 1},
            _db_session_factory(),
        )

    assert calls == [], "an unresolved allocation must never be driven"
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "ACTIVE"
    assert str(rows[0].vlan_assignment_id) == "00000000-0000-0000-0000-000000000000", (
        "no allocation resolved: the row carries the nil-UUID placeholder, not a real id"
    )
    assert "no VLAN allocation for fabric" in rows[0].last_error


# --- _apply_l2_memberships idempotency gates: already-settled adds/removes skip ---
#
# Both live callers only ever pass an add/remove genuinely outside the current DB
# state (the intended/current diff excludes anything already matching), so these
# in-pass idempotency re-checks are exercised directly against _apply_l2_memberships,
# covering a redelivery or a race that lands a duplicate item in the same batch.


async def test_apply_l2_memberships_add_already_active_is_skipped_no_driver_call():
    """An add whose (switch, port) is already an ACTIVE membership for this
    reservation is a convergent no-op: is_membership_active gates it out before any
    add_to_vlan call, while a genuinely new add on the same switch still drives."""
    async with TestSessionLocal() as s:
        va_row = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(va_row)
        await s.commit()
        await s.refresh(va_row)
        va = va_row.id
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va,
                switch_device_id=uuid.UUID(SW_L2),
                port="0/0/1",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()

    execute_fn, calls = _l2_recorder()
    ctx = _FetchContext(None)
    with (
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ):
        await _apply_l2_memberships(
            RES_ID,
            [],
            [
                {
                    "switch_device_id": SW_L2,
                    "port": "0/0/1",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                },
                {
                    "switch_device_id": SW_L2,
                    "port": "0/0/2",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                },
            ],
            ctx,
            _db_session_factory(),
        )

    actions = [(a, p) for a, p, _v in calls]
    assert ("add_to_vlan", "0/0/1") not in actions, "already-ACTIVE membership skips the driver"
    assert ("add_to_vlan", "0/0/2") in actions, "the genuinely new add still drives"
    async with TestSessionLocal() as s:
        rows = (await s.execute(select(L2PortAssignment))).scalars().all()
    by_port = {r.port: r for r in rows}
    assert by_port["0/0/1"].status == "ACTIVE", "the pre-existing row is untouched"
    assert by_port["0/0/2"].status == "ACTIVE"


async def test_apply_l2_memberships_remove_not_live_is_skipped_no_driver_call():
    """A remove whose (switch, port) is not believed live (no row at all) is a
    convergent no-op: membership_needs_remove gates it out before any
    remove_from_vlan call, while a genuinely live remove on the same switch still
    drives."""
    async with TestSessionLocal() as s:
        va_row = VlanAssignment(
            reservation_id=uuid.UUID(RES_ID),
            fabric_id=FABRIC,
            vlan_id=100,
            switch_device_ids=[SW_L2],
            defined_switch_ids=[SW_L2],
            status="ACTIVE",
        )
        s.add(va_row)
        await s.commit()
        await s.refresh(va_row)
        va = va_row.id
        s.add(
            L2PortAssignment(
                reservation_id=uuid.UUID(RES_ID),
                vlan_assignment_id=va,
                switch_device_id=uuid.UUID(SW_L2),
                port="0/0/9",
                status="ACTIVE",
                intended="ACTIVE",
            )
        )
        await s.commit()

    execute_fn, calls = _l2_recorder()
    ctx = _FetchContext(None)
    with (
        patch("app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)),
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/driver")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
    ):
        await _apply_l2_memberships(
            RES_ID,
            [
                # 0/0/5 has no matching DB row at all: not believed live.
                {
                    "switch_device_id": SW_L2,
                    "port": "0/0/5",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                },
                {
                    "switch_device_id": SW_L2,
                    "port": "0/0/9",
                    "vlan_assignment_id": va,
                    "vlan_id": 100,
                },
            ],
            [],
            ctx,
            _db_session_factory(),
        )

    actions = [(a, p) for a, p, _v in calls]
    assert ("remove_from_vlan", "0/0/5") not in actions, "not-believed-live skips the driver"
    assert ("remove_from_vlan", "0/0/9") in actions, "the genuinely live remove still drives"
