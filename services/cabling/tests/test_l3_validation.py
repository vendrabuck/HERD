"""Unit tests for the L3 routing-intent validation pass (ADR 0014 phase 1, issue #34).

Drives `_run_topology_validation` directly (no ASGI transport) against a real
sqlite-backed physical Connection graph, with inventory's httpx calls mocked at the
`app.services.l3_validation.httpx.AsyncClient` boundary, matching the pattern in
test_route_handlers_direct.py.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base
from app.models.connection import Connection
from app.models.topology import Topology
from app.routes.topologies import _run_topology_validation
from app.services import l3_validation
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


def _mock_client(
    *, batch_status=200, batch_json=None, config_status=200, config_json=None, raise_exc=None
):
    """A patch target for l3_validation.httpx.AsyncClient distinguishing the batch
    device-type POST from the per-device config GET by HTTP method."""

    async def _get(url, headers=None, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        resp = MagicMock()
        resp.status_code = config_status
        resp.json = MagicMock(return_value=config_json if config_json is not None else {})
        return resp

    async def _post(url, headers=None, json=None, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        resp = MagicMock()
        resp.status_code = batch_status
        resp.json = MagicMock(return_value=batch_json if batch_json is not None else [])
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return factory, client


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


async def _validate(canvas):
    async with TestSession() as db:
        return await _run_topology_validation(Topology(canvas_data=canvas), db)


# --- Positive control ---


@pytest.mark.asyncio
async def test_valid_route_reports_no_invalid_routes():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.20.0.0/24", "next_hop": "10.0.0.2", "interface": "eth1"}]}
    )
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.valid is True
    assert result.invalid_routes == []


# --- No data.l3 anywhere: no inventory call ---


@pytest.mark.asyncio
async def test_no_l3_data_makes_no_inventory_call():
    await _seed_physical_connection()
    canvas = _canvas(None)
    factory, client = _mock_client()
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes == []
    client.get.assert_not_called()
    client.post.assert_not_called()


# --- l3_malformed ---


@pytest.mark.asyncio
async def test_malformed_shape_reports_l3_malformed_and_suppresses_per_route():
    await _seed_physical_connection()
    canvas = _canvas({"bad": "shape"})
    factory, client = _mock_client()
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.valid is False
    assert len(result.invalid_routes) == 1
    entry = result.invalid_routes[0]
    assert entry.reason == "l3_malformed"
    assert entry.index is None
    assert entry.detail is not None
    # Malformed is a switch-level refusal, so no CONFIG fetch is needed to detect
    # it (the device-type batch call still runs up front, batched for every
    # l3-carrying node in one validation call regardless of individual shape).
    client.get.assert_not_called()


# --- l3_not_a_router ---


@pytest.mark.asyncio
async def test_not_a_router_suppresses_per_route_reasons():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "not-an-ip", "interface": "eth1"}]})
    factory, _ = _mock_client(batch_json=_device_batch_entry(connection_type="Layer 2 Switch"))
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert len(result.invalid_routes) == 1
    assert result.invalid_routes[0].reason == "l3_not_a_router"
    assert result.invalid_routes[0].index is None


@pytest.mark.asyncio
async def test_not_a_router_when_device_missing_from_batch_response():
    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(batch_json=[])  # inventory omits the device entirely
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_not_a_router"


# --- l3_switch_unconfigured ---


@pytest.mark.asyncio
async def test_unconfigured_when_no_config_version_404():
    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(batch_json=_device_batch_entry(), config_status=404)
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unconfigured"


@pytest.mark.asyncio
async def test_unconfigured_when_config_has_no_interfaces():
    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(), config_json=_config_with_interfaces([])
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unconfigured"


# --- l3_switch_unattached ---


@pytest.mark.asyncio
async def test_unattached_when_no_valid_edge_touches_switch():
    canvas = _canvas({"routes": []}, with_edge=False)
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_switch_unattached"


@pytest.mark.asyncio
async def test_unattached_when_only_element_attachment_touches_switch():
    """An element attachment does not count as wiring (ADR 0014 Decision 5)."""
    switch_data = {"device": {"id": str(SWITCH)}, "l3": {"routes": []}}
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
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    async with TestSession() as db:
        with patch.object(l3_validation.httpx, "AsyncClient", factory):
            result = await _run_topology_validation(Topology(canvas_data=canvas), db)
    assert result.invalid_routes[0].reason == "l3_switch_unattached"


# --- Per-route reasons ---


@pytest.mark.asyncio
async def test_bad_destination():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "not-an-ip", "interface": "eth1"}]})
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_bad_destination"
    assert result.invalid_routes[0].index == 0


@pytest.mark.asyncio
async def test_bad_next_hop():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.0.0.0/24", "interface": "eth1", "next_hop": "not-an-ip"}]}
    )
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_bad_next_hop"


@pytest.mark.asyncio
async def test_unknown_interface():
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "10.0.0.0/24", "interface": "eth9"}]})
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_unknown_interface"


@pytest.mark.asyncio
async def test_next_hop_unverifiable_when_interface_has_no_ip():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.0.0.0/24", "interface": "eth1", "next_hop": "10.0.0.5"}]}
    )
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_next_hop_unverifiable"


@pytest.mark.asyncio
async def test_next_hop_unverifiable_when_ip_has_no_prefix_length():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.0.0.0/24", "interface": "eth1", "next_hop": "10.0.0.5"}]}
    )
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_next_hop_unverifiable"


@pytest.mark.asyncio
async def test_next_hop_outside_interface():
    await _seed_physical_connection()
    canvas = _canvas(
        {"routes": [{"destination": "10.0.0.0/24", "interface": "eth1", "next_hop": "192.168.1.5"}]}
    )
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes[0].reason == "l3_next_hop_outside_interface"


@pytest.mark.asyncio
async def test_interface_route_skips_next_hop_checks():
    """A route with no next_hop never triggers l3_next_hop_unverifiable even though
    the interface it names has no ip at all."""
    await _seed_physical_connection()
    canvas = _canvas({"routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}]})
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    assert result.invalid_routes == []


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
    factory, _ = _mock_client(
        batch_json=_device_batch_entry(),
        config_json=_config_with_interfaces([{"name": "eth1", "ip": "10.0.0.1/24"}]),
    )
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        result = await _validate(canvas)
    reasons = {(r.index, r.reason) for r in result.invalid_routes}
    assert reasons == {(0, "l3_bad_destination"), (1, "l3_unknown_interface")}


# --- 503 on inventory outage ---


@pytest.mark.asyncio
async def test_503_on_device_batch_transport_error():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(raise_exc=httpx.ConnectError("connection refused"))
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "l3_config_unavailable"}


@pytest.mark.asyncio
async def test_503_on_device_batch_5xx():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(batch_status=500)
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_503_on_config_fetch_5xx():
    from fastapi import HTTPException

    await _seed_physical_connection()
    canvas = _canvas({"routes": []})
    factory, _ = _mock_client(batch_json=_device_batch_entry(), config_status=500)
    with patch.object(l3_validation.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as exc:
            await _validate(canvas)
    assert exc.value.status_code == 503
