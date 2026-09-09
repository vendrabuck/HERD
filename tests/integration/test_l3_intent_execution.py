"""Integration tests for execution's consumption of Layer 3 routing intent
(ADR 0014 phase 3, issue #34).

Reuses `_l3_helpers.py` (mock_l3 driver upload, device/connection helpers) and the
`_canvas_with_l3` shape from test_l3_intent_validate_and_fork.py (a device node
carrying `data.l3.routes`), combined with test_l3_reconcile.py's fork-save and
polling helpers. The switch's config version publishes `interfaces` (INTERFACES
below) rather than a bare `routes` array: with routing intent present, execution
drives the INTENT, not the config's own routes (there are none here), so any
configure_route/remove_route observed in these tests is proof intent, not config
fallback, drove it (ADR 0014 Decision 2/addendum X2).

Covers:
- (a) a reservation against a topology whose switch carries intent provisions
  exactly the intent, readable back via GET /reservations/{id}/fork.
- (b) a fork save that changes one route on an already-provisioned switch drives
  one remove_route and one configure_route (Decision 3's delta), and the fork's
  l3_routes reflects the new set.
- (c) a save that removes all intent from a switch that stays wired leaves the
  applied set untouched (addendum X4: no surprise teardown), while the fork's
  l3_routes for that device goes empty.
- (d) cancelling the reservation removes exactly the applied (intent-derived) set.
- addendum X-B: two Layer 3 switches joined by a single trunk hop, both carrying
  intent, both provision (the inter-switch "assumed provisioned" skip is
  overridden by explicit intent on either or both ends).

Requires a running stack with NATS reachable from the host; without either it
skips or errors at connect time, which is expected. Live-gated at review.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ._l3_helpers import create_connection as _create_connection
from ._l3_helpers import create_device as _create_device
from ._l3_helpers import create_l3_driver, create_l3_template
from ._nats_helpers import probe_nats

pytestmark = pytest.mark.asyncio

# "zone" is required by the Layer 3 Switch registry schema
# (herd_common/device_config.py) since mock_l3 publishes no config_schema() of
# its own; "ip" stays prefixed so a route's next_hop can be verified inside it.
INTERFACES = [
    {"name": "eth0", "ip": "10.0.0.1/24", "zone": "trust"},
    {"name": "eth1", "ip": "10.0.1.1/24", "zone": "trust"},
]
ROUTE_A = {"destination": "10.20.0.0/24", "next_hop": "10.0.0.2", "interface": "eth0"}
ROUTE_B = {"destination": "10.21.0.0/24", "next_hop": "10.0.1.2", "interface": "eth1"}


@pytest.fixture(scope="session")
async def l3_driver(base_url, admin_token):
    """Re-declared locally with its own uniquely-named driver (session-scoped
    fixtures do not cross test modules), matching the sibling L3 integration
    files' convention."""
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        driver = await create_l3_driver(client, f"mock-l3-exec-{uuid.uuid4().hex[:8]}")
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def l3_template(base_url, admin_token, l3_driver):
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    ) as client:
        template = await create_l3_template(
            client, l3_driver["id"], f"mock-l3-exec-tmpl-{uuid.uuid4().hex[:8]}"
        )
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture
async def l3_switch(admin_client, l3_template):
    """A Layer 3 Switch device whose config publishes INTERFACES but NO routes:
    any configure_route/remove_route observed against it is proof intent drove
    it, since the config-fallback path has nothing to provision."""
    switch = await _create_device(
        admin_client, l3_template["id"], f"mock-l3-exec-{uuid.uuid4().hex[:8]}"
    )
    resp = await admin_client.post(
        f"/inventory/devices/{switch['id']}/config-versions",
        json={"config": {"interfaces": INTERFACES}, "description": "l3 execution integration"},
    )
    assert resp.status_code == 201, f"config-version create failed: {resp.status_code} {resp.text}"
    yield switch
    await admin_client.delete(f"/inventory/devices/{switch['id']}")


def _canvas_with_l3(dut_id: str, switch_id: str, routes: list[dict] | None) -> dict:
    switch_data: dict = {"device": {"id": switch_id}}
    if routes is not None:
        switch_data["l3"] = {"routes": routes}
    return {
        "nodes": [
            {"id": "nDut", "data": {"device": {"id": dut_id}}},
            {"id": "nSwitch", "data": switch_data},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nDut",
                "target": "nSwitch",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


def _trunk_canvas_with_l3(
    switch_a_id: str, routes_a: list[dict] | None, switch_b_id: str, routes_b: list[dict] | None
) -> dict:
    """Two L3 switches joined by one edge, each optionally carrying intent (the
    X-B trunk-override scenario)."""
    a_data: dict = {"device": {"id": switch_a_id}}
    if routes_a is not None:
        a_data["l3"] = {"routes": routes_a}
    b_data: dict = {"device": {"id": switch_b_id}}
    if routes_b is not None:
        b_data["l3"] = {"routes": routes_b}
    return {
        "nodes": [
            {"id": "nA", "data": a_data},
            {"id": "nB", "data": b_data},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


async def _create_topology(client, canvas: dict) -> str:
    resp = await client.post(
        "/cabling/topologies", json={"name": f"int-l3-exec-{uuid.uuid4().hex[:8]}"}
    )
    resp.raise_for_status()
    topology_id = resp.json()["id"]
    put = await client.put(f"/cabling/topologies/{topology_id}", json={"canvas_data": canvas})
    put.raise_for_status()
    return topology_id


async def _reserve(client, device_ids: list[str], topology_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": "l3 routing intent execution integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _save_fork(client, reservation_id, canvas):
    return await client.post(
        f"/reservations/{reservation_id}/fork/save", json={"canvas_data": canvas}
    )


async def _poll_active(client, reservation_id: str, *, timeout: float = 25.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == "ACTIVE":
            return True
        await asyncio.sleep(0.5)
    return False


async def _runs(client, reservation_id: str, action: str, status: str = "SUCCESS") -> list[dict]:
    resp = await client.get(
        "/execution/runs",
        params={"reservation_id": reservation_id, "status": status, "limit": 300},
    )
    if resp.status_code != 200:
        return []
    return [r for r in resp.json().get("items", []) if r["action"] == action]


async def _poll_route_run(client, reservation_id, action, destination, *, timeout=30.0):
    """Poll for a SUCCESS run of `action` whose route destination (packed into
    port_a by _route_run_identity) matches; returns it or None on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for r in await _runs(client, reservation_id, action):
            if r.get("port_a") == destination:
                return r
        await asyncio.sleep(0.5)
    return None


async def _no_route_run(client, reservation_id, action, *, window=6.0) -> bool:
    """True if NO SUCCESS run of `action` appears within `window` seconds."""
    deadline = asyncio.get_event_loop().time() + window
    while asyncio.get_event_loop().time() < deadline:
        if await _runs(client, reservation_id, action):
            return False
        await asyncio.sleep(0.5)
    return True


async def _get_fork(client, reservation_id):
    resp = await client.get(f"/reservations/{reservation_id}/fork")
    resp.raise_for_status()
    return resp.json()


# --- (a) a reservation whose switch carries intent provisions exactly the intent ---


async def test_reservation_with_intent_provisions_exactly_the_intent(
    admin_client, l3_switch, fresh_device
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    connection = None
    topology_id = None
    reservation_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], l3_switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], l3_switch["id"], [ROUTE_A])
        )
        reservation = await _reserve(
            admin_client, [fresh_device["id"], l3_switch["id"]], topology_id
        )
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"

        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.20.0.0/24"
        ), "the intent route was never configured"
        # No OTHER route was ever driven (the config carries no `routes` at all, so
        # anything beyond the intent would be a precedence bug).
        runs = await _runs(admin_client, reservation_id, "configure_route")
        assert {r["port_a"] for r in runs} == {"10.20.0.0/24"}

        fork = await _get_fork(admin_client, reservation_id)
        assert len(fork["l3_routes"]) == 1
        assert fork["l3_routes"][0]["destination"] == "10.20.0.0/24"
        assert fork["l3_routes"][0]["device_id"] == l3_switch["id"]
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")


# --- (b) a fork save that changes intent drives one remove, one configure ---


async def test_fork_save_changing_one_route_drives_delta_and_pin_advances(
    admin_client, l3_switch, fresh_device
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    connection = None
    topology_id = None
    reservation_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], l3_switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], l3_switch["id"], [ROUTE_A])
        )
        reservation = await _reserve(
            admin_client, [fresh_device["id"], l3_switch["id"]], topology_id
        )
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"
        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.20.0.0/24"
        ), "the initial intent route was never configured"

        # Save a canvas replacing ROUTE_A with ROUTE_B on the same switch: the delta
        # is exactly one remove_route (10.20.0.0/24) and one configure_route
        # (10.21.0.0/24).
        saved = await _save_fork(
            admin_client,
            reservation_id,
            _canvas_with_l3(fresh_device["id"], l3_switch["id"], [ROUTE_B]),
        )
        assert saved.status_code == 200, saved.text

        assert await _poll_route_run(
            admin_client, reservation_id, "remove_route", "10.20.0.0/24"
        ), "the departed route was never removed"
        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.21.0.0/24"
        ), "the arriving route was never configured"
        # Never a SECOND configure_route for the route that was already there and
        # never left (an unchanged-route no-op, S5's delta-gating shape end to end).
        configure_runs = await _runs(admin_client, reservation_id, "configure_route")
        assert {r["port_a"] for r in configure_runs} == {"10.20.0.0/24", "10.21.0.0/24"}, (
            "each route configures exactly once across the reservation's lifetime"
        )

        fork = await _get_fork(admin_client, reservation_id)
        assert len(fork["l3_routes"]) == 1
        assert fork["l3_routes"][0]["destination"] == "10.21.0.0/24"
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")


# --- (c) a save removing all intent leaves the applied set (addendum X4) ---


async def test_save_removing_all_intent_leaves_the_applied_set(
    admin_client, l3_switch, fresh_device
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    connection = None
    topology_id = None
    reservation_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], l3_switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], l3_switch["id"], [ROUTE_A])
        )
        reservation = await _reserve(
            admin_client, [fresh_device["id"], l3_switch["id"]], topology_id
        )
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"
        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.20.0.0/24"
        ), "the initial intent route was never configured"

        # Save the SAME wiring but with the switch's `l3` key dropped entirely: the
        # switch stays adjacent (still wired), but carries no intent any more.
        saved = await _save_fork(
            admin_client,
            reservation_id,
            _canvas_with_l3(fresh_device["id"], l3_switch["id"], None),
        )
        assert saved.status_code == 200, saved.text

        assert await _no_route_run(admin_client, reservation_id, "remove_route"), (
            "intent disappearing while the switch stays wired must not tear down its routes"
        )

        fork = await _get_fork(admin_client, reservation_id)
        assert fork["l3_routes"] == [], "cabling's resolved intent for the device is gone"

        # The originally-applied route is still the only one ever configured (no
        # re-derive, no phantom removal).
        configure_runs = await _runs(admin_client, reservation_id, "configure_route")
        assert {r["port_a"] for r in configure_runs} == {"10.20.0.0/24"}
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")


# --- (d) cancel removes exactly the applied (intent-derived) set ---


async def test_cancel_removes_exactly_the_applied_intent_derived_set(
    admin_client, l3_switch, fresh_device
):
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    connection = None
    topology_id = None
    reservation_id = None
    try:
        connection = await _create_connection(
            admin_client, fresh_device["id"], l3_switch["id"], "ge-0/0/1"
        )
        topology_id = await _create_topology(
            admin_client, _canvas_with_l3(fresh_device["id"], l3_switch["id"], [ROUTE_A])
        )
        reservation = await _reserve(
            admin_client, [fresh_device["id"], l3_switch["id"]], topology_id
        )
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"
        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.20.0.0/24"
        ), "reservation never provisioned its intent route, cannot test removal"

        resp = await admin_client.delete(f"/reservations/{reservation_id}")
        assert resp.status_code == 204, resp.text

        assert await _poll_route_run(
            admin_client, reservation_id, "remove_route", "10.20.0.0/24"
        ), "the applied intent route was never removed on cancel"
        remove_runs = await _runs(admin_client, reservation_id, "remove_route")
        assert {r["port_a"] for r in remove_runs} == {"10.20.0.0/24"}, (
            "exactly the applied set was removed, nothing else"
        )
    finally:
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")


# --- addendum X-B: explicit intent overrides the inter-switch trunk inference ---


async def test_trunk_hop_with_intent_on_both_ends_drives_both_switches(admin_client, l3_template):
    """The exact scenario from the phase 3 brief's addendum X-B: two Layer 3
    switches joined by one trunk hop, both carrying intent, both provision (no
    trunk skip)."""
    nats_err = await probe_nats()
    if nats_err:
        pytest.skip(nats_err)
    suffix = uuid.uuid4().hex[:8]
    switch_a = await _create_device(admin_client, l3_template["id"], f"mock-l3-trunk-a-{suffix}")
    switch_b = await _create_device(admin_client, l3_template["id"], f"mock-l3-trunk-b-{suffix}")
    connection = None
    topology_id = None
    reservation_id = None
    try:
        for switch in (switch_a, switch_b):
            resp = await admin_client.post(
                f"/inventory/devices/{switch['id']}/config-versions",
                json={
                    "config": {"interfaces": INTERFACES},
                    "description": "l3 execution integration trunk",
                },
            )
            assert resp.status_code == 201, resp.text

        connection = await _create_connection(admin_client, switch_a["id"], switch_b["id"], "eth1")
        route_on_a = {"destination": "10.30.0.0/24", "next_hop": "10.0.1.2", "interface": "eth1"}
        route_on_b = {"destination": "10.31.0.0/24", "next_hop": "10.0.1.1", "interface": "eth1"}
        topology_id = await _create_topology(
            admin_client,
            _trunk_canvas_with_l3(switch_a["id"], [route_on_a], switch_b["id"], [route_on_b]),
        )
        reservation = await _reserve(admin_client, [switch_a["id"], switch_b["id"]], topology_id)
        reservation_id = reservation["id"]
        assert await _poll_active(admin_client, reservation_id), "reservation never activated"

        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.30.0.0/24"
        ), "switch A's intent route was never configured (trunk skip not overridden)"
        assert await _poll_route_run(
            admin_client, reservation_id, "configure_route", "10.31.0.0/24"
        ), "switch B's intent route was never configured (trunk skip not overridden)"

        fork = await _get_fork(admin_client, reservation_id)
        assert {r["device_id"] for r in fork["l3_routes"]} == {switch_a["id"], switch_b["id"]}
    finally:
        if reservation_id:
            await admin_client.delete(f"/reservations/{reservation_id}")
        if topology_id:
            await admin_client.delete(f"/cabling/topologies/{topology_id}")
        if connection:
            await admin_client.delete(f"/cabling/connections/{connection['id']}")
        await admin_client.delete(f"/inventory/devices/{switch_a['id']}")
        await admin_client.delete(f"/inventory/devices/{switch_b['id']}")
