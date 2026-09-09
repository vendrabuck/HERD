"""Unit tests for the L3 routing-intent validation pass (ADR 0014 phase 1, issue #34).

Drives `run_full_topology_validation` (`app.services.topology_validation`) against
a real sqlite-backed physical Connection graph, with inventory's calls mocked at
the `app.services.l3_validation.call_service` seam (R6 review fix: the module now
uses `herd_common.internal_client.call_service`, not raw `httpx.AsyncClient`).

`_validate_one_route` also gets direct pure-function tests (R9): they need no
inventory double or event loop at all.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.connection import Connection
from app.services import l3_validation
from app.services.l3_intent import RouteSpec
from app.services.l3_validation import _validate_one_route
from app.services.topology_validation import run_full_topology_validation
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

SWITCH = uuid.uuid4()
DUT = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _canvas(l3: dict | None, *, with_edge: bool = True) -> dict:
    switch_data: dict = {"device": {"id": str(SWITCH)}}
    if l3 is not None:
        switch_data["l3"] = l3
    nodes = [
        {"id": "switch", "data": switch_data},
        {"id": "dut", "data": {"device": {"id": str(DUT)}}},
    ]
    edges = []
    if with_edge:
        edges.append({"id": "e1", "source": "switch", "target": "dut", "data": {"layer": "L1"}})
    return {"nodes": nodes, "edges": edges}


async def _seed_physical_connection():
    async with TestSession() as db:
        db.add(
            Connection(
                device_a_id=SWITCH,
                port_a="eth0",
                device_b_id=DUT,
                port_b="eth0",
                created_by="tester",
            )
        )
        await db.commit()


def _device_batch_entry(connection_type="Layer 3 Switch"):
    return [
        {
            "id": str(SWITCH),
            "name": "sw1",
            "connection_type": connection_type,
            "status": "AVAILABLE",
        }
    ]


def _config_with_interfaces(interfaces):
    return {"config": {"interfaces": interfaces}}


def _mock_call_service(
    *, batch_status=200, batch_json=None, config_status=200, config_json=None, raise_exc=None
):
    """A patch target for l3_validation.call_service distinguishing the batch
    device-type POST from the per-device config GET by HTTP method (path)."""
    calls: list[tuple[str, str]] = []

    async def _fake_call_service(
        base_url, method, path, *, json_body=None, timeout=None, auth=None
    ):
        calls.append((method, path))
        if raise_exc is not None:
            raise raise_exc
        resp = httpx.Response(
            batch_status if method == "POST" else config_status,
            json=(batch_json if batch_json is not None else [])
            if method == "POST"
            else (config_json if config_json is not None else {}),
        )
        return resp

    return AsyncMock(side_effect=_fake_call_service), calls


async def _validate(canvas):
    async with TestSession() as db:
        return await run_full_topology_validation(canvas, db)


# --- Positive control ---


@pytest.mark.asyncio
async def test_valid_route_reports_no_invalid_routes():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.20.0.0/24", "next_hop": "10.0.0.2", "interface": "eth1"}]}
    )
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.valid is True
    assert result.invalid_routes == []


# --- No data.l3 anywhere: no inventory call ---


@pytest.mark.asyncio
async def test_no_l3_data_makes_no_inventory_call():
    await _seed_physical_connection()
    canvas = _canvas(None)
    mock, calls = _mock_call_service()
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes == []
    assert calls == []


# --- Empty routes list: no intent at all (R10) ---


@pytest.mark.asyncio
async def test_empty_routes_list_is_no_intent_makes_no_inventory_call():
    """data.l3 = {"routes": []} is no intent (R10): no switch-level checks fire
    and no inventory call is made, even though the node carries an l3 key."""
    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    mock, calls = _mock_call_service()
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.valid is True
    assert result.invalid_routes == []
    assert calls == []


# --- l3_malformed ---


@pytest.mark.asyncio
async def test_malformed_shape_reports_l3_malformed_and_suppresses_per_route():
    await _seed_physical_connection()
    canvas = _canvas({"bad": "shape"})
    mock, calls = _mock_call_service()
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.valid is False
    assert len(result.invalid_routes) == 1
    entry = result.invalid_routes[0]
    assert entry.reason == "l3_malformed"
    assert entry.index is None
    assert entry.detail is not None
    # A malformed shape is caught by the parser alone: no inventory call at all.
    assert calls == []


# --- l3_not_a_router ---


@pytest.mark.asyncio
async def test_not_a_router_suppresses_per_route_reasons():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "not-an-ip", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(connection_type="Layer 2 Switch")
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert len(result.invalid_routes) == 1
    assert result.invalid_routes[0].reason == "l3_not_a_router"
    assert result.invalid_routes[0].index is None


@pytest.mark.asyncio
async def test_not_a_router_when_device_missing_from_batch_response():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(batch_json=[])  # inventory omits the device entirely
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_not_a_router"


# --- l3_switch_unconfigured ---


@pytest.mark.asyncio
async def test_unconfigured_when_no_config_version_404():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(batch_json=_device_batch_entry(), config_status=404)
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unconfigured"


@pytest.mark.asyncio
async def test_unconfigured_when_config_has_no_interfaces():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(), config_json=_config_with_interfaces([])
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unconfigured"


@pytest.mark.asyncio
async def test_unconfigured_when_interfaces_entries_are_not_dicts():
    """R6: a non-dict interfaces entry (a driver-published schema may store any
    shape) is skipped, never raises; no usable names is l3_switch_unconfigured."""
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json={"config": {"interfaces": ["not-a-dict", {"no_name": True}]}},
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unconfigured"


# --- l3_switch_unattached ---


@pytest.mark.asyncio
async def test_unattached_when_no_valid_edge_touches_switch():
    canvas = _canvas(
        {"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]}, with_edge=False
    )
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unattached"


@pytest.mark.asyncio
async def test_unattached_when_only_element_attachment_touches_switch():
    """An element attachment does not count as wiring (ADR 0014 Decision 5); R2:
    touched_devices comes from resolve_canvas_wiring's WireSpec endpoints, which
    an element attachment never contributes to."""
    switch_data = {
        "device": {"id": str(SWITCH)},
        "l3": {"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]},
    }
    canvas = {
        "nodes": [
            {"id": "switch", "data": switch_data},
            {
                "id": "elem",
                "type": "networkElementNode",
                "data": {"element": {"id": "e1"}},
            },
        ],
        "edges": [
            {
                "id": "attach",
                "source": "switch",
                "target": "elem",
                "data": {"source_port_name": "eth0"},
            }
        ],
    }
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unattached"


@pytest.mark.asyncio
async def test_transit_device_on_multi_hop_path_counts_as_attached():
    """R2: a device that is never itself an edge endpoint, but sits mid-path on a
    resolved multi-hop wire, counts as touched (resolve_canvas_wiring emits one
    WireSpec per physical hop, including the transit device's endpoints)."""
    transit = uuid.uuid4()
    async with TestSession() as db:
        db.add(
            Connection(
                device_a_id=DUT, port_a="eth0", device_b_id=transit, port_b="p1", created_by="t"
            )
        )
        db.add(
            Connection(
                device_a_id=transit, port_a="p2", device_b_id=SWITCH, port_b="eth0", created_by="t"
            )
        )
        await db.commit()

    canvas = {
        "nodes": [
            {"id": "dut", "data": {"device": {"id": str(DUT)}}},
            {
                "id": "switch",
                "data": {
                    "device": {"id": str(SWITCH)},
                    "l3": {"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]},
                },
            },
        ],
        "edges": [{"id": "e1", "source": "dut", "target": "switch"}],
    }
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes == []


@pytest.mark.asyncio
async def test_port_constrained_edge_with_no_cable_leaves_switch_unattached():
    """R2: a port-constrained edge whose named port has no matching physical
    cable resolves to no hop, so the switch it names is NOT touched even though
    an (unconstrained) edge nominally connects it in the canvas."""
    await _seed_physical_connection()  # SWITCH to DUT wired on eth0/eth0
    canvas = {
        "nodes": [
            {"id": "dut", "data": {"device": {"id": str(DUT)}}},
            {
                "id": "switch",
                "data": {
                    "device": {"id": str(SWITCH)},
                    "l3": {"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]},
                },
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "dut",
                "target": "switch",
                "data": {"source_port_name": "eth0", "target_port_name": "eth9"},
            }
        ],
    }
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unattached"


# --- Per-route reasons (via _validate_one_route directly, R9) ---


def test_validate_one_route_bad_destination():
    route = RouteSpec(destination="not-an-ip", next_hop=None, interface="eth1", virtual_router=None)
    assert _validate_one_route(route, {"eth1": None}) == "l3_bad_destination"


def test_validate_one_route_bad_next_hop():
    route = RouteSpec(
        destination="10.0.0.0/24", next_hop="not-an-ip", interface="eth1", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": "10.0.0.1/24"}) == "l3_bad_next_hop"


def test_validate_one_route_unknown_interface():
    route = RouteSpec(
        destination="10.0.0.0/24", next_hop=None, interface="eth9", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": "10.0.0.1/24"}) == "l3_unknown_interface"


def test_validate_one_route_next_hop_unverifiable_no_ip():
    route = RouteSpec(
        destination="10.0.0.0/24", next_hop="10.0.0.5", interface="eth1", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": None}) == "l3_next_hop_unverifiable"


def test_validate_one_route_next_hop_unverifiable_no_prefix_length():
    route = RouteSpec(
        destination="10.0.0.0/24", next_hop="10.0.0.5", interface="eth1", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": "10.0.0.1"}) == "l3_next_hop_unverifiable"


def test_validate_one_route_next_hop_outside_interface():
    route = RouteSpec(
        destination="10.0.0.0/24", next_hop="192.168.1.5", interface="eth1", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": "10.0.0.1/24"}) == "l3_next_hop_outside_interface"


def test_validate_one_route_interface_route_skips_next_hop_checks():
    """A route with no next_hop never triggers l3_next_hop_unverifiable even
    though the interface it names has no ip at all."""
    route = RouteSpec(destination="0.0.0.0/0", next_hop=None, interface="eth1", virtual_router=None)
    assert _validate_one_route(route, {"eth1": None}) is None


def test_validate_one_route_clean_route_returns_none():
    route = RouteSpec(
        destination="10.20.0.0/24", next_hop="10.0.0.2", interface="eth1", virtual_router=None
    )
    assert _validate_one_route(route, {"eth1": "10.0.0.1/24"}) is None


@pytest.mark.asyncio
async def test_per_route_reasons_all_reported():
    await _seed_physical_connection()
    canvas = _canvas(
        {
            "routes": [
                {"destination": "not-an-ip", "interface": "eth1"},
                {"destination": "10.0.0.0/24", "interface": "eth9"},
            ]
        }
    )
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    reasons = {(r.index, r.reason) for r in result.invalid_routes}
    assert reasons == {(0, "l3_bad_destination"), (1, "l3_unknown_interface")}


# --- 503 on inventory outage ---


@pytest.mark.asyncio
async def test_503_on_device_batch_transport_error():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(raise_exc=httpx.ConnectError("connection refused"))
    with patch.object(l3_validation, "call_service", mock):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "l3_config_unavailable"}


@pytest.mark.asyncio
async def test_503_on_device_batch_5xx():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(batch_status=500)
    with patch.object(l3_validation, "call_service", mock):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_503_on_config_fetch_5xx():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(batch_json=_device_batch_entry(), config_status=500)
    with patch.object(l3_validation, "call_service", mock):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_503_on_missing_internal_token_short_circuits_to_l3_config_unavailable():
    """InternalTokenAuth raises RuntimeError (not httpx.HTTPError) when no token
    is configured; L3InventoryContext must still fail closed with 503."""
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    mock, _calls = _mock_call_service(raise_exc=RuntimeError("internal_api_token not configured"))
    with patch.object(l3_validation, "call_service", mock):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503


# --- S12: l3_duplicate_route is informational, reported with the ORIGINAL index ---


@pytest.mark.asyncio
async def test_duplicate_route_reported_as_informational_and_does_not_invalidate():
    await _seed_physical_connection()
    canvas = _canvas(
        {
            "routes": [
                {"destination": "10.0.0.0/24", "interface": "eth1"},
                {"destination": "10.0.0.0/24", "interface": "eth1"},  # duplicate, index 1
                {"destination": "10.20.0.0/24", "interface": "eth1", "next_hop": "10.0.0.2"},
            ]
        }
    )
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    assert result.valid is True  # informational only
    reasons = {(r.index, r.reason) for r in result.invalid_routes}
    assert reasons == {(1, "l3_duplicate_route")}


@pytest.mark.asyncio
async def test_original_index_survives_a_collapse_for_a_later_bad_route():
    """S12: a bad route AFTER a collapsed duplicate reports its ORIGINAL
    position, not its post-collapse position."""
    await _seed_physical_connection()
    canvas = _canvas(
        {
            "routes": [
                {"destination": "10.0.0.0/24", "interface": "eth1"},  # index 0, kept
                {"destination": "10.0.0.0/24", "interface": "eth1"},  # index 1, duplicate
                {"destination": "not-an-ip", "interface": "eth1"},  # index 2, bad
            ]
        }
    )
    mock, _calls = _mock_call_service(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation, "call_service", mock):
        result = await _validate(canvas)
    reasons = {(r.index, r.reason) for r in result.invalid_routes}
    assert reasons == {(1, "l3_duplicate_route"), (2, "l3_bad_destination")}
    assert result.valid is False  # l3_bad_destination is real, unlike the duplicate


# --- S13: deadline budget ---


@pytest.mark.asyncio
async def test_l3_pass_deadline_trips_into_503():
    """A validation call that never returns (a hung inventory) trips the
    whole-pass deadline and fails closed with 503, same shape as any other
    L3ConfigUnavailable."""
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})

    async def _hang(base_url, method, path, *, json_body=None, timeout=None, auth=None):
        await asyncio.sleep(3600)

    with (
        patch.object(l3_validation, "call_service", AsyncMock(side_effect=_hang)),
        patch.object(l3_validation, "_L3_PASS_DEADLINE_SECONDS", 0.05),
    ):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "l3_config_unavailable"}


def test_inventory_call_timeout_constant_is_pinned():
    assert l3_validation._INVENTORY_CALL_TIMEOUT_SECONDS == 4.0


def test_l3_pass_deadline_constant_is_pinned():
    assert l3_validation._L3_PASS_DEADLINE_SECONDS == 12.0
