"""Consumer tests for the L3 layered adjacency reconcile (ADR 0009 phase 5, issue #416).

Covers the option-C adjacency derivation (a hop terminating on a Layer 3 Switch makes the
reservation adjacent to it; an L3-to-L3 trunk contributes nothing; a hop reaching an L3
switch THROUGH an L1 switch credits the L3 side; multi-hop shared adjacency dedups), the
provision-on-first-adjacency / deprovision-on-last lifecycle, the pinned-set-verbatim
retry in both directions, result-gated pin writes, the #412 ACTIVE-immutable guard, and
the Decision 4 ordering (L3 runs after L2 within one apply), all driven through
handle_wiring_changed with an in-memory SQLite DB and a mocked sandbox, the pattern the L2
reconcile tests use.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.route_assignment import RouteAssignment
from app.services.nats_consumer import (
    TransientUpstreamError,
    _derive_l3_adjacency,
    _fetch_latest_config,
    _FetchContext,
    _route_run_identity,
    handle_wiring_changed,
)
from app.services.route_service import record_route_active, record_route_failed
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


SW_L3 = str(uuid.uuid4())
SW_L3_B = str(uuid.uuid4())
SW_L2 = str(uuid.uuid4())
SW_L1 = str(uuid.uuid4())
DUT1 = str(uuid.uuid4())
DUT2 = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
FABRIC = uuid.uuid4()

TEMPLATE_DATA = {"id": "tmpl-1", "name": "L3 Template", "sections": []}
SUCCESS_RESULT = {"success": True, "output": {"result": True}, "error": None, "duration_ms": 5}

ROUTES = [
    {"destination": "10.0.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.1.0.0/24", "next_hop": None, "interface": "eth1"},
]
ROUTES_B = [{"destination": "172.16.0.0/16", "next_hop": "192.0.2.1", "interface": "eth9"}]
EDITED_ROUTES = [{"destination": "203.0.113.0/24", "next_hop": "198.51.100.1", "interface": "eth5"}]


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
    SW_L3: _dev(SW_L3, "Layer 3 Switch", "L3-A"),
    SW_L3_B: _dev(SW_L3_B, "Layer 3 Switch", "L3-B"),
    SW_L2: _dev(SW_L2, "Layer 2 Switch", "L2"),
    SW_L1: _dev(SW_L1, "Layer 1 Switch", "L1"),
    DUT1: _dev(DUT1, "Server", "dut1"),
    DUT2: _dev(DUT2, "Server", "dut2"),
}

CONFIG_BY_SWITCH = {
    SW_L3: {"config": {"routes": ROUTES}},
    SW_L3_B: {"config": {"routes": ROUTES_B}},
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


async def _config_fetch(device_id, client=None):
    return CONFIG_BY_SWITCH.get(str(device_id))


def _l3_recorder(fail=None):
    """(execute_fn, calls): calls records (action, destination). `fail` fails matches.

    `fail` is a set of (action, destination) tuples whose sandbox op returns success=False;
    a bare action name (str) in the set fails every call of that action.
    """
    fail = fail or set()
    calls = []

    def execute_fn(driver_path, action, context, **kwargs):
        mk = kwargs.get("method_kwargs") or {}
        dest = mk.get("destination")
        calls.append((action, dest))
        if action in fail or (action, dest) in fail:
            return {"success": False, "output": None, "error": "boom", "duration_ms": 1}
        return SUCCESS_RESULT

    return execute_fn, calls


def _patches(execute_fn, fork_wires, config_fetch=_config_fetch):
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
        patch(
            "app.services.nats_consumer._fetch_latest_config",
            new=AsyncMock(side_effect=config_fetch),
        ),
        patch("app.services.vlan_service.fetch_fabric_id", new=AsyncMock(return_value=FABRIC)),
    ]


async def _reconcile(fork_wires, fork_version=1, execute_fn=None, calls=None, config_fetch=None):
    if execute_fn is None:
        execute_fn, calls = _l3_recorder()
    kwargs = {} if config_fetch is None else {"config_fetch": config_fetch}
    with ExitStack() as stack:
        for p in _patches(execute_fn, fork_wires, **kwargs):
            stack.enter_context(p)
        await handle_wiring_changed(
            {"reservation_id": RES_ID, "fork_version": fork_version},
            _db_session_factory(),
        )
    return calls


async def _rows(status=None):
    async with TestSessionLocal() as s:
        stmt = select(RouteAssignment)
        if status is not None:
            stmt = stmt.where(RouteAssignment.status == status)
        return (await s.execute(stmt)).scalars().all()


async def _active_switches():
    return {str(r.device_id) for r in await _rows("ACTIVE")}


# --- derivation (option C) ---


async def test_derive_dut_to_l3_joins_switch():
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l3_adjacency([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], ctx)
    assert m == {SW_L3}


async def test_derive_l3_to_l3_trunk_contributes_nothing():
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l3_adjacency([_wire(SW_L3, "xe-0/0/9", SW_L3_B, "xe-0/0/9")], ctx)
    assert m == set(), "an inter-switch L3 trunk is assumed provisioned, no adjacency"


async def test_derive_through_l1_credits_the_l3_side():
    """DUT - L1 - L3: two hops; the L3 switch is credited even though no reserved DUT is
    directly adjacent to it (the divergence from the legacy device-set resolver)."""
    wires = [_wire(DUT1, "eth0", SW_L1, "1/0/1"), _wire(SW_L1, "1/0/2", SW_L3, "ge-0/0/1")]
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l3_adjacency(wires, ctx)
    assert m == {SW_L3}


async def test_derive_dedups_shared_adjacency():
    """Two DUTs both adjacent to one L3 switch collapse to one adjacency entry."""
    wires = [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1"), _wire(DUT2, "eth1", SW_L3, "ge-0/0/2")]
    ctx = _FetchContext(None)
    with patch(
        "app.services.nats_consumer._fetch_device", new=AsyncMock(side_effect=_device_fetch)
    ):
        m = await _derive_l3_adjacency(wires, ctx)
    assert m == {SW_L3}


# --- reconcile lifecycle ---


async def test_reconcile_provisions_pinned_routes_on_first_adjacency():
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")])
    assert ("configure_route", "10.0.0.0/24") in calls
    assert ("configure_route", "10.1.0.0/24") in calls
    active = await _active_switches()
    assert active == {SW_L3}
    rows = await _rows("ACTIVE")
    assert rows[0].routes == ROUTES, "the switch's config routes were pinned verbatim"


ROUTES_ECMP = [
    {"destination": "10.5.0.0/24", "next_hop": "192.168.1.1", "interface": "eth0"},
    {"destination": "10.5.0.0/24", "next_hop": "192.168.1.2", "interface": "eth0"},
]


async def test_reconcile_ecmp_siblings_both_configured_not_collapsed():
    """Two ECMP routes to the same destination via different next hops must both be
    configured through the L3 apply path, not collapsed to one (issue #448 item 3).

    Restores end-to-end strength for the ECMP non-collapse property: previously it
    was pinned only at the _route_run_identity identity-function level
    (test_route_run_identity_packs_three_fields below), which proves the two
    siblings WOULD get distinct idempotency keys but never proves the reconcile
    apply loop actually drives a driver call for each one rather than deduping by
    destination somewhere upstream (the retired legacy device-set L3 executor had
    an end-to-end pin for exactly this that was deleted with it in ADR 0009 phase
    7). This test drives the real fork-reconcile path (handle_wiring_changed to
    _reconcile_l3_adjacency to _apply_l3_adjacency) with a switch config carrying
    both siblings and asserts both configure_route calls happen and both routes
    are pinned verbatim.
    """

    async def _ecmp_config_fetch(device_id, client=None):
        if str(device_id) == SW_L3:
            return {"config": {"routes": ROUTES_ECMP}}
        return None

    calls = await _reconcile(
        [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], config_fetch=_ecmp_config_fetch
    )
    configure_calls = [c for c in calls if c[0] == "configure_route"]
    assert configure_calls.count(("configure_route", "10.5.0.0/24")) == 2, (
        "both ECMP siblings must be configured, not collapsed into a single call"
    )

    rows = await _rows("ACTIVE")
    assert len(rows) == 1
    assert rows[0].routes == ROUTES_ECMP, "the pinned set retains both ECMP siblings verbatim"


async def test_reconcile_deprovisions_on_last_adjacency_lost():
    """Emptying the canvas removes exactly the pinned set and releases the pin."""
    await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], fork_version=1)
    calls = await _reconcile([], fork_version=2)
    removed = {d for a, d in calls if a == "remove_route"}
    assert removed == {"10.0.0.0/24", "10.1.0.0/24"}, "exactly the pinned set was removed"
    assert await _active_switches() == set()
    released = await _rows("RELEASED")
    assert len(released) == 1 and str(released[0].device_id) == SW_L3


async def test_reconcile_multi_hop_shared_adjacency_keeps_routes_until_last_hop():
    """Two DUTs behind one L3 switch: releasing one hop keeps the routes while the other
    remains (adjacency is shared derived state; only the intended SET can end it)."""
    await _reconcile(
        [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1"), _wire(DUT2, "eth1", SW_L3, "ge-0/0/2")],
        fork_version=1,
    )
    assert await _active_switches() == {SW_L3}

    # Drop only DUT2's hop: the switch is still adjacent via DUT1, so no deprovision.
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], fork_version=2)
    assert not any(a == "remove_route" for a, _d in calls), "a still-adjacent switch keeps routes"
    assert await _active_switches() == {SW_L3}

    # Drop the last hop: now the switch is deprovisioned.
    calls = await _reconcile([], fork_version=3)
    assert any(a == "remove_route" for a, _d in calls)
    assert await _active_switches() == set()


async def test_reconcile_reuses_pinned_set_not_edited_config_on_reprovision():
    """After a switch is pinned, a later reconcile that re-adds it (e.g. after a FAILED
    build) reuses the PINNED routes, never the switch's current (edited) config."""
    # First provision fails so the row is FAILED (pin captured), config still ROUTES.
    fail_fn, _ = _l3_recorder(fail={"configure_route"})
    await _reconcile(
        [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], fork_version=1, execute_fn=fail_fn, calls=[]
    )
    failed = await _rows("FAILED")
    assert len(failed) == 1 and failed[0].routes == ROUTES

    # Now the switch's config is edited, and a fresh reconcile re-provisions it cleanly.
    async def _edited_config(device_id, client=None):
        return {"config": {"routes": EDITED_ROUTES}}

    calls = await _reconcile(
        [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], fork_version=2, config_fetch=_edited_config
    )
    dests = {d for a, d in calls if a == "configure_route"}
    assert dests == {"10.0.0.0/24", "10.1.0.0/24"}, "the ORIGINAL pinned set, not edited config"
    assert await _active_switches() == {SW_L3}


# --- result gating ---


async def test_reconcile_failed_provision_lands_failed_intended_active():
    execute_fn, calls = _l3_recorder(fail={"configure_route"})
    await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], execute_fn=execute_fn, calls=calls)
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].status == "FAILED"
    assert rows[0].intended == "ACTIVE"
    assert rows[0].routes == ROUTES, "the pinned set is captured even on a failed provision"
    assert await _active_switches() == set()


async def test_reconcile_present_key_falsy_result_is_failure():
    """configure_route returning {"success": False} in its output (transport ok) fails."""

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "configure_route":
            return {"success": True, "output": {"success": False}, "error": None, "duration_ms": 1}
        return SUCCESS_RESULT

    await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], execute_fn=execute_fn, calls=[])
    rows = await _rows()
    assert [r.status for r in rows] == ["FAILED"]


async def test_reconcile_bare_data_output_stays_success():
    """An output dict with no `success` key is a bare-data return and stays SUCCESS."""

    def execute_fn(driver_path, action, context, **kwargs):
        if action == "configure_route":
            return {"success": True, "output": {"applied": True}, "error": None, "duration_ms": 1}
        return SUCCESS_RESULT

    await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], execute_fn=execute_fn, calls=[])
    assert await _active_switches() == {SW_L3}


# --- #412 guard ---


async def test_record_route_failed_ignores_stale_build_failure_on_active_row():
    """An ACTIVE pin is immutable to a stale BUILD-direction failure writer (#412)."""
    async with TestSessionLocal() as db:
        await record_route_active(db, RES_ID, SW_L3, ROUTES)
    async with TestSessionLocal() as db:
        row = await record_route_failed(
            db, RES_ID, SW_L3, ROUTES, 3, "late boom", intended="ACTIVE"
        )
    assert row.status == "ACTIVE", "the concurrent-proven ACTIVE row is not downgraded"
    assert row.attempts == 0 and row.last_error is None


async def test_record_route_failed_release_direction_flips_active_to_failed():
    """A RELEASE-direction failure on an ACTIVE row IS recorded (issue #369): the routes
    are still installed, and the removal that failed must be retryable."""
    async with TestSessionLocal() as db:
        await record_route_active(db, RES_ID, SW_L3, ROUTES)
    async with TestSessionLocal() as db:
        row = await record_route_failed(
            db, RES_ID, SW_L3, ROUTES, 2, "remove boom", intended="RELEASED"
        )
    assert row.status == "FAILED" and row.intended == "RELEASED"
    assert row.attempts == 2


# --- ordering (Decision 4: L3 after L2 within one apply) ---


async def test_l3_pass_runs_after_l2_within_one_apply():
    """One save that implies both an L2 membership and an L3 adjacency drives the L2 join
    before the L3 provision (Decision 4 ordering)."""
    wires = [_wire(DUT1, "eth0", SW_L2, "0/0/1"), _wire(DUT2, "eth1", SW_L3, "ge-0/0/1")]
    execute_fn, calls = _l3_recorder()
    await _reconcile(wires, execute_fn=execute_fn, calls=calls)
    actions = [a for a, _d in calls]
    assert "add_to_vlan" in actions and "configure_route" in actions
    assert actions.index("add_to_vlan") < actions.index("configure_route"), (
        "the L2 membership pass must complete before the L3 adjacency pass"
    )


async def test_provision_skips_switch_whose_config_has_no_routes():
    """A newly adjacent L3 switch whose latest config declares no routes provisions
    nothing and pins nothing (re-expresses the retired legacy pin
    test_execute_l3_provision_noops_when_config_has_no_routes on the reconcile path)."""

    async def _no_routes_config(device_id, client=None):
        return {"config": {"routes": []}}

    calls = await _reconcile(
        [_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")], config_fetch=_no_routes_config
    )
    assert [c for c in calls if c[0] == "configure_route"] == []
    assert await _rows() == []


async def test_provision_wraps_routes_in_one_login_logout():
    """All of a switch's routes provision inside ONE login/logout session
    (re-expresses the retired legacy pin
    test_execute_l3_provision_wraps_routes_in_one_login_logout)."""
    calls = await _reconcile([_wire(DUT1, "eth0", SW_L3, "ge-0/0/1")])
    actions = [a for a, _ in calls]
    assert actions.count("login") == 1
    assert actions.count("logout") == 1
    assert actions.count("configure_route") == len(ROUTES)
    assert actions.index("login") < actions.index("configure_route")
    assert actions.index("logout") > max(i for i, a in enumerate(actions) if a == "configure_route")


# --- surviving-helper unit tests (moved here when the retired device-set L3
# --- executor tests in test_nats_consumer_l3.py were deleted for ADR 0009 phase 7) ---


def test_route_run_identity_packs_three_fields():
    """A route's idempotency identity puts destination in port_a and packs
    interface plus next_hop into port_b as "interface|next_hop"; a None next_hop
    renders as an empty string."""
    assert _route_run_identity("10.0.0.0/24", "192.168.1.1", "eth0") == (
        "10.0.0.0/24",
        "eth0|192.168.1.1",
    )
    # None next_hop (an interface route) renders empty after the pipe.
    assert _route_run_identity("10.1.0.0/24", None, "eth1") == ("10.1.0.0/24", "eth1|")


async def test_config_fetch_5xx_raises_transient_for_nak():
    """An inventory 5xx while fetching a switch's latest config must NAK the event
    (raise TransientUpstreamError), not silently skip the reconcile."""

    class _Resp:
        status_code = 500

    client = AsyncMock()
    client.get = AsyncMock(return_value=_Resp())
    with pytest.raises(TransientUpstreamError):
        await _fetch_latest_config(SW_L3, client)


async def test_config_fetch_404_returns_none():
    """A 404 (no config version for the switch) resolves to None, a clean no-op."""

    class _Resp:
        status_code = 404

    client = AsyncMock()
    client.get = AsyncMock(return_value=_Resp())
    assert await _fetch_latest_config(SW_L3, client) is None
