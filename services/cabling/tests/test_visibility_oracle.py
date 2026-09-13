"""Device-visibility filtering on validate and pathfind (issue #763).

The user-facing `POST /topologies/{id}/validate` and the two pathfind routes
answered questions about any device uuid a caller could name: whether it is
physically reachable, and (since ADR 0014 phase 1) whether it is a Layer 3
switch, which interfaces it has, and which subnets those interfaces carry.
Non-admin device visibility is device-group gated everywhere else, and the
pathfind hop list handed a non-admin the ids to ask about, so the pair was a
config-content oracle for gear outside the caller's visibility.

These tests drive the route handlers DIRECTLY (the convention of
test_route_handlers_direct.py: no ASGITransport, so coverage credits the handler
bodies) with inventory mocked at two seams: `fetch_visible_device_ids` (the
cabling-side visibility client, patched on `app.services.visible_devices`
because `resolve_caller_visibility` calls it as a module global) and
`l3_validation.call_service` (the device-type
and config reads the L3 pass makes). Every test states what an ADMIN caller sees
for the same canvas or pair, since the filter must be invisible to admins.
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base
from app.models.connection import Connection
from app.models.topology import Topology
from app.routes.pathfind import pathfind_batch_endpoint, pathfind_endpoint
from app.routes.topologies import validate_topology
from app.schemas.pathfind import PathfindBatchRequest, PathfindRequest
from app.services import l3_validation, visible_devices
from app.services.visible_devices import VisibleDevicesUnavailableError
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
DUT_A = uuid.uuid4()
DUT_B = uuid.uuid4()
HIDDEN_SWITCH = uuid.uuid4()

VISIBLE = {DUT_A, DUT_B}
AUTH = "Bearer caller-token"


def _payload(role="user"):
    return {"sub": str(USER_ID), "username": "viewer", "role": role}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_chain(db):
    """DUT_A to HIDDEN_SWITCH to DUT_B: the switch is a transit hop between two
    devices the caller can see, which is exactly how a hidden device's id
    reached a non-admin in the first place."""
    db.add(
        Connection(
            device_a_id=DUT_A,
            port_a="eth0",
            device_b_id=HIDDEN_SWITCH,
            port_b="eth1",
            created_by="tester",
        )
    )
    db.add(
        Connection(
            device_a_id=HIDDEN_SWITCH,
            port_a="eth2",
            device_b_id=DUT_B,
            port_b="eth0",
            created_by="tester",
        )
    )
    await db.commit()


def _canvas() -> dict:
    """Two visible DUTs plus a hidden Layer 3 switch carrying routing intent,
    edged to both DUTs."""
    return {
        "nodes": [
            {"id": "a", "data": {"device": {"id": str(DUT_A)}}},
            {"id": "b", "data": {"device": {"id": str(DUT_B)}}},
            {
                "id": "sw",
                "data": {
                    "device": {"id": str(HIDDEN_SWITCH)},
                    "l3": {
                        "routes": [
                            {
                                "destination": "10.20.0.0/24",
                                "next_hop": "10.0.0.2",
                                "interface": "eth1",
                            }
                        ]
                    },
                },
            },
        ],
        "edges": [
            {"id": "a-sw", "source": "a", "target": "sw", "data": {"layer": "L1"}},
            {"id": "sw-b", "source": "sw", "target": "b", "data": {"layer": "L1"}},
            {"id": "a-b", "source": "a", "target": "b", "data": {"layer": "L2"}},
        ],
    }


async def _make_topology(db, canvas) -> Topology:
    topology = Topology(
        name="visibility",
        created_by=USER_ID,
        owner_name="viewer",
        canvas_data=canvas,
    )
    db.add(topology)
    await db.commit()
    await db.refresh(topology)
    return topology


def _mock_inventory(*, connection_type="Layer 3 Switch", interfaces=None):
    """Patch target for l3_validation.call_service: the device-type batch POST
    and the per-device config GET, plus a record of every call made, so a test
    can assert the L3 pass never asked inventory anything at all."""
    calls: list[tuple[str, str]] = []

    async def _fake(base_url, method, path, *, json_body=None, timeout=None, auth=None):
        calls.append((method, path))
        if method == "POST":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": str(HIDDEN_SWITCH),
                        "name": "sw1",
                        "connection_type": connection_type,
                        "status": "AVAILABLE",
                    }
                ],
            )
        return httpx.Response(200, json={"config": {"interfaces": interfaces or []}})

    return AsyncMock(side_effect=_fake), calls


def _visible_mock():
    return AsyncMock(return_value=set(VISIBLE))


# --- validate: the canvas is redacted before either pass runs ---------------


@pytest.mark.asyncio
async def test_validate_non_admin_reports_hidden_node_as_missing_device():
    """Both edges touching the hidden switch come back `missing_device`, the
    visible-to-visible edge keeps its real verdict, and the hidden device id is
    absent from `device_ids`."""
    inventory, calls = _mock_inventory()
    async with TestSession() as db:
        await _seed_chain(db)
        topology = await _make_topology(db, _canvas())
        with patch.object(visible_devices, "fetch_visible_device_ids", _visible_mock()):
            with patch.object(l3_validation, "call_service", inventory):
                result = await validate_topology(
                    topology_id=topology.id,
                    payload=_payload(),
                    authorization=AUTH,
                    db=db,
                )
    reasons = {edge.edge_id: edge.reason for edge in result.invalid_edges}
    assert reasons["a-sw"] == "missing_device"
    assert reasons["sw-b"] == "missing_device"
    # DUT_A to DUT_B is physically reachable THROUGH the hidden switch; the
    # redaction removes the node, not the cabling, so this edge stays valid.
    assert "a-b" not in reasons
    assert result.device_ids == sorted({DUT_A, DUT_B})
    assert HIDDEN_SWITCH not in result.device_ids
    # The L3 pass never ran for the hidden switch: no inventory call at all.
    assert calls == []
    assert result.invalid_routes == []


@pytest.mark.asyncio
async def test_validate_admin_sees_the_real_l3_reasons():
    """The same canvas, same fixtures, admin caller: the switch is judged for
    real, so the L3 pass runs and reports the actual reason. This is the
    control that proves the non-admin result above is the filter at work and
    not an empty canvas."""
    inventory, calls = _mock_inventory(interfaces=[{"name": "eth9", "ip": "10.0.0.1/24"}])
    async with TestSession() as db:
        await _seed_chain(db)
        topology = await _make_topology(db, _canvas())
        with patch.object(l3_validation, "call_service", inventory):
            result = await validate_topology(
                topology_id=topology.id,
                payload=_payload(role="admin"),
                authorization=AUTH,
                db=db,
            )
    reasons = {edge.edge_id: edge.reason for edge in result.invalid_edges}
    assert "a-sw" not in reasons
    assert "sw-b" not in reasons
    assert [r.reason for r in result.invalid_routes] == ["l3_unknown_interface"]
    assert result.device_ids == sorted({DUT_A, DUT_B, HIDDEN_SWITCH})
    assert calls  # admins are not filtered, so inventory IS consulted


@pytest.mark.asyncio
async def test_validate_admin_never_calls_the_visibility_lookup():
    fetch = AsyncMock(return_value=set())
    inventory, _calls = _mock_inventory()
    async with TestSession() as db:
        await _seed_chain(db)
        topology = await _make_topology(db, _canvas())
        with patch.object(visible_devices, "fetch_visible_device_ids", fetch):
            with patch.object(l3_validation, "call_service", inventory):
                await validate_topology(
                    topology_id=topology.id,
                    payload=_payload(role="superadmin"),
                    authorization=AUTH,
                    db=db,
                )
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_non_admin_fails_closed_when_visibility_unavailable():
    """An unanswerable visibility lookup is a 503 with NO partial result: the
    handler must never fall back to validating the unfiltered canvas."""
    fetch = AsyncMock(side_effect=VisibleDevicesUnavailableError("inventory down"))
    inventory, calls = _mock_inventory()
    async with TestSession() as db:
        await _seed_chain(db)
        topology = await _make_topology(db, _canvas())
        with patch.object(visible_devices, "fetch_visible_device_ids", fetch):
            with patch.object(l3_validation, "call_service", inventory):
                with pytest.raises(HTTPException) as exc:
                    await validate_topology(
                        topology_id=topology.id,
                        payload=_payload(),
                        authorization=AUTH,
                        db=db,
                    )
    assert exc.value.status_code == 503
    assert "device visibility" in exc.value.detail
    assert calls == []


# --- pathfind: hidden endpoints refused, hidden transit redacted ------------


@pytest.mark.asyncio
async def test_pathfind_non_admin_refuses_hidden_endpoint():
    """A pair naming a device the caller cannot see answers the same 404 an id
    that does not exist answers for that caller, and resolves nothing."""
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", _visible_mock()):
            with pytest.raises(HTTPException) as exc:
                await pathfind_endpoint(
                    body=PathfindRequest(source_device_id=DUT_A, target_device_id=HIDDEN_SWITCH),
                    payload=_payload(),
                    authorization=AUTH,
                    db=db,
                )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Device not found"


@pytest.mark.asyncio
async def test_pathfind_non_admin_refuses_unknown_device_identically():
    """The refusal itself must not be an oracle: an id that exists but is
    hidden and an id that exists nowhere produce the same 404."""
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", _visible_mock()):
            with pytest.raises(HTTPException) as exc:
                await pathfind_endpoint(
                    body=PathfindRequest(source_device_id=DUT_A, target_device_id=uuid.uuid4()),
                    payload=_payload(),
                    authorization=AUTH,
                    db=db,
                )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Device not found"


@pytest.mark.asyncio
async def test_pathfind_non_admin_redacts_hidden_transit_hop():
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", _visible_mock()):
            result = await pathfind_endpoint(
                body=PathfindRequest(source_device_id=DUT_A, target_device_id=DUT_B),
                payload=_payload(),
                authorization=AUTH,
                db=db,
            )
    # Reachability and hop count are untouched: the editor still works.
    assert result.reachable is True
    assert result.hop_count == 3
    hops = result.paths[0]
    assert [hop.device_id for hop in hops] == [DUT_A, None, DUT_B]
    assert [hop.hidden for hop in hops] == [False, True, False]
    # The hidden device's port names go with its id.
    assert hops[1].port_in is None and hops[1].port_out is None
    # The visible endpoints keep their own ports.
    assert hops[0].port_out == "eth0"
    assert hops[2].port_in == "eth0"


@pytest.mark.asyncio
async def test_pathfind_admin_sees_the_transit_hop():
    async with TestSession() as db:
        await _seed_chain(db)
        result = await pathfind_endpoint(
            body=PathfindRequest(source_device_id=DUT_A, target_device_id=DUT_B),
            payload=_payload(role="admin"),
            authorization=AUTH,
            db=db,
        )
    hops = result.paths[0]
    assert [hop.device_id for hop in hops] == [DUT_A, HIDDEN_SWITCH, DUT_B]
    assert [hop.hidden for hop in hops] == [False, False, False]
    assert hops[1].port_in == "eth1"


@pytest.mark.asyncio
async def test_pathfind_non_admin_fails_closed_when_visibility_unavailable():
    fetch = AsyncMock(side_effect=VisibleDevicesUnavailableError("inventory down"))
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", fetch):
            with pytest.raises(HTTPException) as exc:
                await pathfind_endpoint(
                    body=PathfindRequest(source_device_id=DUT_A, target_device_id=DUT_B),
                    payload=_payload(),
                    authorization=AUTH,
                    db=db,
                )
    assert exc.value.status_code == 503


# --- pathfind batch --------------------------------------------------------


def _batch(*pairs) -> PathfindBatchRequest:
    return PathfindBatchRequest(
        pairs=[PathfindRequest(source_device_id=src, target_device_id=tgt) for src, tgt in pairs]
    )


@pytest.mark.asyncio
async def test_batch_non_admin_refuses_hidden_pair_and_resolves_the_rest():
    """One refused pair does not sink the batch: it is reported in place, in
    request order, with the single route's wording."""
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", _visible_mock()):
            response = await pathfind_batch_endpoint(
                body=_batch((DUT_A, HIDDEN_SWITCH), (DUT_A, DUT_B)),
                payload=_payload(),
                authorization=AUTH,
                db=db,
            )
    refused, allowed = response.results
    assert refused.source_device_id == DUT_A
    assert refused.target_device_id == HIDDEN_SWITCH
    assert refused.error == "Device not found"
    assert refused.reachable is False
    assert refused.hop_count == 0
    assert refused.paths == []
    # The surviving pair is resolved normally, with its transit hop redacted.
    assert allowed.error is None
    assert allowed.reachable is True
    assert allowed.hop_count == 3
    assert [hop.device_id for hop in allowed.paths[0]] == [DUT_A, None, DUT_B]
    assert allowed.paths[0][1].hidden is True


@pytest.mark.asyncio
async def test_batch_admin_resolves_the_hidden_pair():
    async with TestSession() as db:
        await _seed_chain(db)
        response = await pathfind_batch_endpoint(
            body=_batch((DUT_A, HIDDEN_SWITCH), (DUT_A, DUT_B)),
            payload=_payload(role="admin"),
            authorization=AUTH,
            db=db,
        )
    first, second = response.results
    assert first.error is None
    assert first.reachable is True
    assert [hop.device_id for hop in first.paths[0]] == [DUT_A, HIDDEN_SWITCH]
    assert [hop.device_id for hop in second.paths[0]] == [DUT_A, HIDDEN_SWITCH, DUT_B]


@pytest.mark.asyncio
async def test_batch_unreachable_pair_stays_distinguishable_from_a_refused_one():
    """A genuinely unreachable visible pair reports error=None, so a client can
    still tell "no path" from "not yours to ask about"."""
    lonely = uuid.uuid4()
    fetch = AsyncMock(return_value={DUT_A, DUT_B, lonely})
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", fetch):
            response = await pathfind_batch_endpoint(
                body=_batch((DUT_A, lonely), (DUT_A, HIDDEN_SWITCH)),
                payload=_payload(),
                authorization=AUTH,
                db=db,
            )
    unreachable, refused = response.results
    assert unreachable.reachable is False
    assert unreachable.error is None
    assert refused.reachable is False
    assert refused.error == "Device not found"


@pytest.mark.asyncio
async def test_batch_non_admin_fails_closed_when_visibility_unavailable():
    fetch = AsyncMock(side_effect=VisibleDevicesUnavailableError("inventory down"))
    async with TestSession() as db:
        await _seed_chain(db)
        with patch.object(visible_devices, "fetch_visible_device_ids", fetch):
            with pytest.raises(HTTPException) as exc:
                await pathfind_batch_endpoint(
                    body=_batch((DUT_A, DUT_B)),
                    payload=_payload(),
                    authorization=AUTH,
                    db=db,
                )
    assert exc.value.status_code == 503
