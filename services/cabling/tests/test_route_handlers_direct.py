"""Direct route-handler and service-function tests for cabling.

These call the FastAPI route handlers and service functions directly (no
ASGITransport) so pytest-cov credits the handler bodies. SQLAlchemy 2.x async
runs its DB work inside a greenlet that the default coverage tracer does not
follow through the ASGI request path, so the equivalent ASGI tests in the other
suites exercise the same code without coverage attribution. This file mirrors
the pattern established in tests/test_service_unit.py.

Cross-service HTTP calls (reservations, inventory) are mocked at the
httpx.AsyncClient boundary, matching the unittest.mock pattern used elsewhere in
the suite.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models import *  # noqa: F401, F403  (register every table on Base.metadata)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()


def _payload(sub=USER_ID, username="viewer", role="user"):
    return {"sub": str(sub), "username": username, "role": role}


class _FakeRequest:
    """Minimal Request stand-in; the handlers only read request.headers."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mock_httpx_client(*, status_code=200, json_data=None, raise_exc=None):
    """Build a patch target for httpx.AsyncClient used as an async context manager.

    Returns a MagicMock suitable for `patch("...httpx.AsyncClient", new=...)`.
    The returned client's `.get`/`.post` resolve to a response with the given
    status_code and json payload, or raise `raise_exc` when set.
    """
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data if json_data is not None else {})
    response.raise_for_status = MagicMock()

    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
        client.post = AsyncMock(side_effect=raise_exc)
    else:
        client.get = AsyncMock(return_value=response)
        client.post = AsyncMock(return_value=response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    return factory, client, response


# --- services/reservation_guard.py -----------------------------------------


@pytest.mark.asyncio
async def test_reservation_guard_filters_blocking_status():
    from app.services import reservation_guard

    topology_id = uuid.uuid4()
    other_topology = uuid.uuid4()
    items = [
        {"id": "r1", "topology_id": str(topology_id), "status": "ACTIVE"},
        {"id": "r2", "topology_id": str(topology_id), "status": "PENDING"},
        {"id": "r3", "topology_id": str(topology_id), "status": "COMPLETED"},
        {"id": "r4", "topology_id": str(other_topology), "status": "ACTIVE"},
    ]
    factory, _, _ = _mock_httpx_client(status_code=200, json_data=items)
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(topology_id)
    ids = {r["id"] for r in result}
    # Only ACTIVE/PENDING/PENDING_PROVISION rows for THIS topology are blocking.
    assert ids == {"r1", "r2"}


@pytest.mark.asyncio
async def test_reservation_guard_bad_response_returns_empty():
    from app.services import reservation_guard

    topology_id = uuid.uuid4()
    factory, _, _ = _mock_httpx_client(status_code=503, json_data=[])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(topology_id)
    assert result == []


@pytest.mark.asyncio
async def test_reservation_guard_unreachable_fails_open():
    from app.services import reservation_guard

    topology_id = uuid.uuid4()
    factory, _, _ = _mock_httpx_client(raise_exc=RuntimeError("connection refused"))
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(topology_id)
    assert result == []


# --- services/device_resolver.py -------------------------------------------


@pytest.mark.asyncio
async def test_device_resolver_empty_names_short_circuits():
    from app.services import device_resolver

    # No HTTP call should be made when every name is falsy (empty or None).
    factory, client, _ = _mock_httpx_client(json_data={"resolved": {}})
    with patch.object(device_resolver.httpx, "AsyncClient", factory):
        result = await device_resolver.resolve_device_names(["", None])  # type: ignore[list-item]
    assert result == {}
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_device_resolver_returns_resolved_map():
    from app.services import device_resolver

    da, db_ = str(uuid.uuid4()), str(uuid.uuid4())
    factory, client, _ = _mock_httpx_client(
        json_data={"resolved": {"switch-a": da, "switch-b": db_}}
    )
    with patch.object(device_resolver.httpx, "AsyncClient", factory):
        result = await device_resolver.resolve_device_names(["switch-b", "switch-a", "switch-a"])
    assert result == {"switch-a": da, "switch-b": db_}
    # Names are de-duplicated and sorted before the single inventory call.
    client.post.assert_awaited_once()
    sent = client.post.await_args.kwargs["json"]["names"]
    assert sent == ["switch-a", "switch-b"]


@pytest.mark.asyncio
async def test_device_resolver_raises_on_transport_failure():
    from app.services import device_resolver

    factory, _, _ = _mock_httpx_client(raise_exc=RuntimeError("inventory down"))
    with patch.object(device_resolver.httpx, "AsyncClient", factory):
        with pytest.raises(RuntimeError):
            await device_resolver.resolve_device_names(["switch-a"])


# --- services/device_group_guard.py ----------------------------------------


@pytest.mark.asyncio
async def test_device_group_guard_returns_group_ids():
    from app.services import device_group_guard

    g1, g2 = str(uuid.uuid4()), str(uuid.uuid4())
    factory, _, _ = _mock_httpx_client(json_data=[{"id": g1}, {"id": g2}])
    with patch.object(device_group_guard.httpx, "AsyncClient", factory):
        result = await device_group_guard.fetch_device_group_ids(uuid.uuid4(), "tok")
    assert result == {g1, g2}


@pytest.mark.asyncio
async def test_device_group_guard_bad_response_returns_none():
    from app.services import device_group_guard

    factory, _, _ = _mock_httpx_client(status_code=500, json_data=[])
    with patch.object(device_group_guard.httpx, "AsyncClient", factory):
        result = await device_group_guard.fetch_device_group_ids(uuid.uuid4(), "tok")
    assert result is None


@pytest.mark.asyncio
async def test_device_group_guard_404_raises_device_not_found():
    """A 404 from inventory means the device is confirmed gone, not merely
    unverifiable; this must raise rather than fall into the None fail-open
    branch above (issue #392)."""
    from app.services import device_group_guard

    device_id = uuid.uuid4()
    factory, _, _ = _mock_httpx_client(status_code=404, json_data={"detail": "not found"})
    with patch.object(device_group_guard.httpx, "AsyncClient", factory):
        with pytest.raises(device_group_guard.DeviceNotFoundError) as exc:
            await device_group_guard.fetch_device_group_ids(device_id, "tok")
    assert exc.value.device_id == device_id


@pytest.mark.asyncio
async def test_device_group_guard_unreachable_returns_none():
    from app.services import device_group_guard

    factory, _, _ = _mock_httpx_client(raise_exc=RuntimeError("inventory down"))
    with patch.object(device_group_guard.httpx, "AsyncClient", factory):
        result = await device_group_guard.fetch_device_group_ids(uuid.uuid4(), "tok")
    assert result is None


# --- routes/pathfind.py -----------------------------------------------------


async def _seed_cable(db, a, port_a, b, port_b):
    from app.models.connection import Connection

    db.add(
        Connection(device_a_id=a, port_a=port_a, device_b_id=b, port_b=port_b, created_by="seed")
    )
    await db.commit()


@pytest.mark.asyncio
async def test_pathfind_handler_reachable():
    from app.routes.pathfind import pathfind_endpoint
    from app.schemas.pathfind import PathfindRequest

    a, b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        result = await pathfind_endpoint(
            body=PathfindRequest(source_device_id=a, target_device_id=b),
            _=_payload(),
            db=db,
        )
    assert result.reachable is True
    assert result.hop_count == 2


@pytest.mark.asyncio
async def test_pathfind_handler_unreachable():
    from app.routes.pathfind import pathfind_endpoint
    from app.schemas.pathfind import PathfindRequest

    a, b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        result = await pathfind_endpoint(
            body=PathfindRequest(source_device_id=a, target_device_id=b),
            _=_payload(),
            db=db,
        )
    assert result.reachable is False
    assert result.hop_count == 0
    assert result.paths == []


# --- routes/fabric.py -------------------------------------------------------


@pytest.mark.asyncio
async def test_fabric_handler_valid_token():
    from app.config import settings
    from app.routes.fabric import get_fabric_internal

    a, b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        with patch.object(settings, "internal_api_token", "tok"):
            result = await get_fabric_internal(device_id=a, x_internal_token="tok", db=db)
    assert result.device_id == a
    assert result.component_size == 2


@pytest.mark.asyncio
async def test_fabric_handler_invalid_token():
    from app.config import settings
    from app.routes.fabric import get_fabric_internal
    from fastapi import HTTPException

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "right"):
            with pytest.raises(HTTPException) as exc:
                await get_fabric_internal(device_id=uuid.uuid4(), x_internal_token="wrong", db=db)
    assert exc.value.status_code == 403


# --- routes/templates.py ----------------------------------------------------


async def _make_template(db, *, name="tmpl", canvas=None, payload=None):
    from app.routes.templates import create_template
    from app.schemas.template import TemplateCreate

    return await create_template(
        body=TemplateCreate(name=name, canvas_data=canvas),
        payload=payload or _payload(),
        db=db,
    )


@pytest.mark.asyncio
async def test_template_create_and_list_and_get():
    from app.routes.templates import get_template, list_templates

    async with TestSession() as db:
        await _make_template(db, name="A")
        await _make_template(db, name="B")
        listed = await list_templates(skip=0, limit=50, payload=_payload(), db=db)
        assert listed.total == 2
        tid = listed.items[0].id
        got = await get_template(template_id=tid, payload=_payload(), db=db)
        assert got.id == tid


@pytest.mark.asyncio
async def test_template_get_not_found():
    from app.routes.templates import get_template
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await get_template(template_id=uuid.uuid4(), payload=_payload(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_template_create_duplicate_name_409():
    from app.routes.templates import create_template
    from app.schemas.template import TemplateCreate
    from fastapi import HTTPException

    async with TestSession() as db:
        await _make_template(db, name="dup")
        with pytest.raises(HTTPException) as exc:
            await create_template(body=TemplateCreate(name="dup"), payload=_payload(), db=db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_template_from_topology_extracts_roles():
    from app.routes.templates import create_template_from_topology
    from app.schemas.template import TemplateFromTopologyRequest

    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": "u1", "template_name": "PA-VM"}}},
            {"id": "n2", "data": {"device": {"id": "u2", "template_name": "PA-VM"}}},
            {"id": "n3", "data": {"label": "Leaf Switch"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n3"}],
    }
    async with TestSession() as db:
        topology = await _make_topology(db, name="Src", canvas=canvas)
        result = await create_template_from_topology(
            topology_id=topology.id,
            body=TemplateFromTopologyRequest(name="tmpl"),
            payload=_payload(),
            db=db,
        )
    roles = [n["data"]["device"]["role"] for n in result.canvas_data["nodes"]]
    assert roles == ["pa-vm-1", "pa-vm-2", "leaf-switch-1"]
    assert result.canvas_data["edges"] == canvas["edges"]


@pytest.mark.asyncio
async def test_template_from_topology_empty_canvas():
    from app.routes.templates import create_template_from_topology
    from app.schemas.template import TemplateFromTopologyRequest

    async with TestSession() as db:
        topology = await _make_topology(db, name="Empty", canvas=None)
        result = await create_template_from_topology(
            topology_id=topology.id,
            body=TemplateFromTopologyRequest(name="from-empty"),
            payload=_payload(),
            db=db,
        )
    assert result.canvas_data == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_template_from_topology_not_found():
    from app.routes.templates import create_template_from_topology
    from app.schemas.template import TemplateFromTopologyRequest
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await create_template_from_topology(
                topology_id=uuid.uuid4(),
                body=TemplateFromTopologyRequest(name="x"),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_template_update_by_owner_and_fields():
    from app.routes.templates import update_template
    from app.schemas.template import TemplateUpdate

    async with TestSession() as db:
        tmpl = await _make_template(db, name="orig")
        updated = await update_template(
            template_id=tmpl.id,
            body=TemplateUpdate(
                name="renamed", description="d", canvas_data={"nodes": [], "edges": []}
            ),
            payload=_payload(),
            db=db,
        )
    assert updated.name == "renamed"
    assert updated.description == "d"
    assert updated.canvas_data == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_template_update_not_found():
    from app.routes.templates import update_template
    from app.schemas.template import TemplateUpdate
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await update_template(
                template_id=uuid.uuid4(),
                body=TemplateUpdate(name="x"),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_template_update_forbidden_for_other_user():
    from app.routes.templates import update_template
    from app.schemas.template import TemplateUpdate
    from fastapi import HTTPException

    async with TestSession() as db:
        tmpl = await _make_template(db, name="owned", payload=_payload(USER_ID))
        with pytest.raises(HTTPException) as exc:
            await update_template(
                template_id=tmpl.id,
                body=TemplateUpdate(name="hijack"),
                payload=_payload(OTHER_ID),
                db=db,
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_template_update_admin_can_edit_others():
    from app.routes.templates import update_template
    from app.schemas.template import TemplateUpdate

    async with TestSession() as db:
        tmpl = await _make_template(db, name="owned", payload=_payload(USER_ID))
        updated = await update_template(
            template_id=tmpl.id,
            body=TemplateUpdate(name="admin-edit"),
            payload=_payload(ADMIN_ID, username="admin", role="admin"),
            db=db,
        )
    assert updated.name == "admin-edit"


@pytest.mark.asyncio
async def test_template_update_duplicate_name_409():
    from app.routes.templates import update_template
    from app.schemas.template import TemplateUpdate
    from fastapi import HTTPException

    async with TestSession() as db:
        await _make_template(db, name="taken")
        tmpl = await _make_template(db, name="mine")
        with pytest.raises(HTTPException) as exc:
            await update_template(
                template_id=tmpl.id,
                body=TemplateUpdate(name="taken"),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_template_delete_by_owner():
    from app.routes.templates import delete_template, get_template
    from fastapi import HTTPException

    async with TestSession() as db:
        tmpl = await _make_template(db, name="doomed")
        await delete_template(template_id=tmpl.id, payload=_payload(), db=db)
        with pytest.raises(HTTPException) as exc:
            await get_template(template_id=tmpl.id, payload=_payload(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_template_delete_not_found():
    from app.routes.templates import delete_template
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await delete_template(template_id=uuid.uuid4(), payload=_payload(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_template_delete_forbidden_for_other_user():
    from app.routes.templates import delete_template
    from fastapi import HTTPException

    async with TestSession() as db:
        tmpl = await _make_template(db, name="owned", payload=_payload(USER_ID))
        with pytest.raises(HTTPException) as exc:
            await delete_template(template_id=tmpl.id, payload=_payload(OTHER_ID), db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_template_instantiate_substitutes_devices_and_snapshot():
    from app.routes.templates import instantiate_template
    from app.schemas.template import InstantiateRequest

    canvas = {
        "nodes": [
            {"id": "a", "data": {"device": {"role": "pa-vm-1"}}},
            {"id": "b", "data": {"device": {"role": "pa-vm-2"}}},
            {"id": "c", "data": {}},  # no device/role: kept verbatim
        ],
        "edges": [{"id": "e", "source": "a", "target": "b"}],
    }
    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        tmpl = await _make_template(db, name="tmpl", canvas=canvas)
        result = await instantiate_template(
            template_id=tmpl.id,
            body=InstantiateRequest(
                name="Live", role_assignments={"pa-vm-1": dev_a, "pa-vm-2": dev_b}
            ),
            payload=_payload(),
            db=db,
        )
    assert result.name == "Live"
    devices = [n["data"].get("device") for n in result.canvas_data["nodes"]]
    assert devices[0] == {"role": "pa-vm-1", "id": str(dev_a)}
    assert devices[1] == {"role": "pa-vm-2", "id": str(dev_b)}

    # A v1 snapshot was written for the instantiated topology.
    from app.models.topology import TopologyVersion
    from sqlalchemy import select

    async with TestSession() as db:
        versions = (
            (
                await db.execute(
                    select(TopologyVersion).where(TopologyVersion.topology_id == result.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(versions) == 1
    assert versions[0].version_number == 1


@pytest.mark.asyncio
async def test_template_instantiate_missing_role_assignment_422():
    from app.routes.templates import instantiate_template
    from app.schemas.template import InstantiateRequest
    from fastapi import HTTPException

    canvas = {"nodes": [{"id": "a", "data": {"device": {"role": "pa-vm-1"}}}], "edges": []}
    async with TestSession() as db:
        tmpl = await _make_template(db, name="t", canvas=canvas)
        with pytest.raises(HTTPException) as exc:
            await instantiate_template(
                template_id=tmpl.id,
                body=InstantiateRequest(name="Lab", role_assignments={}),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 422
    assert "pa-vm-1" in exc.value.detail


@pytest.mark.asyncio
async def test_template_instantiate_empty_canvas():
    from app.routes.templates import instantiate_template
    from app.schemas.template import InstantiateRequest

    async with TestSession() as db:
        tmpl = await _make_template(db, name="blank", canvas=None)
        result = await instantiate_template(
            template_id=tmpl.id,
            body=InstantiateRequest(name="Lab", role_assignments={}),
            payload=_payload(),
            db=db,
        )
    assert result.canvas_data == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_template_instantiate_not_found():
    from app.routes.templates import instantiate_template
    from app.schemas.template import InstantiateRequest
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await instantiate_template(
                template_id=uuid.uuid4(),
                body=InstantiateRequest(name="x", role_assignments={}),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 404


# --- routes/topologies.py ---------------------------------------------------


async def _make_topology(db, *, name="Topo", canvas=None, payload=None):
    from app.models.topology import Topology

    p = payload or _payload()
    topo = Topology(
        name=name,
        created_by=uuid.UUID(p["sub"]),
        owner_name=p.get("username", ""),
        canvas_data=canvas,
    )
    db.add(topo)
    await db.commit()
    await db.refresh(topo)
    return topo


@pytest.mark.asyncio
async def test_topology_clone_handler_with_snapshot():
    from app.routes.topologies import clone_topology
    from app.schemas.topology import TopologyClone

    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    async with TestSession() as db:
        source = await _make_topology(db, name="Src", canvas=canvas)
        clone = await clone_topology(
            topology_id=source.id,
            body=TopologyClone(name="Src (copy)"),
            payload=_payload(OTHER_ID, username="other"),
            db=db,
        )
    assert clone.name == "Src (copy)"
    assert clone.created_by == OTHER_ID
    assert clone.canvas_data == canvas
    assert clone.id != source.id


@pytest.mark.asyncio
async def test_topology_clone_null_canvas():
    from app.routes.topologies import clone_topology
    from app.schemas.topology import TopologyClone

    async with TestSession() as db:
        source = await _make_topology(db, name="Empty", canvas=None)
        clone = await clone_topology(
            topology_id=source.id,
            body=TopologyClone(name="copy"),
            payload=_payload(),
            db=db,
        )
    assert clone.canvas_data is None


@pytest.mark.asyncio
async def test_topology_clone_not_found():
    from app.routes.topologies import clone_topology
    from app.schemas.topology import TopologyClone
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await clone_topology(
                topology_id=uuid.uuid4(),
                body=TopologyClone(name="x"),
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_topology_update_canvas_creates_version_snapshot():
    from app.routes.topologies import update_topology
    from app.schemas.topology import TopologyUpdate

    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    async with TestSession() as db:
        topo = await _make_topology(db, name="T", canvas=None)
        updated = await update_topology(
            topology_id=topo.id,
            body=TopologyUpdate(canvas_data=canvas, description="first"),
            request=_FakeRequest(),
            payload=_payload(),
            db=db,
        )
    assert updated.canvas_data == canvas

    from app.models.topology import TopologyVersion
    from sqlalchemy import select

    async with TestSession() as db:
        versions = (
            (
                await db.execute(
                    select(TopologyVersion).where(TopologyVersion.topology_id == topo.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(versions) == 1
    assert versions[0].version_number == 1
    assert versions[0].description == "first"


@pytest.mark.asyncio
async def test_topology_update_canvas_blocked_by_other_users_reservation():
    from app.routes import topologies as topo_routes
    from app.schemas.topology import TopologyUpdate
    from fastapi import HTTPException

    canvas = {"nodes": [{"id": "x"}], "edges": []}
    blocking = [{"id": "r1", "user_id": str(OTHER_ID), "status": "ACTIVE", "end_time": "t"}]
    async with TestSession() as db:
        topo = await _make_topology(db, name="Reserved", canvas=None, payload=_payload(USER_ID))
        with patch.object(
            topo_routes, "find_blocking_reservations", new=AsyncMock(return_value=blocking)
        ):
            with pytest.raises(HTTPException) as exc:
                await topo_routes.update_topology(
                    topology_id=topo.id,
                    body=TopologyUpdate(canvas_data=canvas),
                    request=_FakeRequest({"authorization": "Bearer tok"}),
                    payload=_payload(USER_ID),
                    db=db,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail["reservations"][0]["id"] == "r1"


@pytest.mark.asyncio
async def test_topology_update_canvas_allowed_for_reservation_owner():
    from app.routes import topologies as topo_routes
    from app.schemas.topology import TopologyUpdate

    canvas = {"nodes": [{"id": "x"}], "edges": []}
    # The blocking reservation belongs to the editing user, so the edit proceeds.
    blocking = [{"id": "r1", "user_id": str(USER_ID), "status": "ACTIVE", "end_time": "t"}]
    async with TestSession() as db:
        topo = await _make_topology(db, name="Mine", canvas=None, payload=_payload(USER_ID))
        with patch.object(
            topo_routes, "find_blocking_reservations", new=AsyncMock(return_value=blocking)
        ):
            updated = await topo_routes.update_topology(
                topology_id=topo.id,
                body=TopologyUpdate(canvas_data=canvas),
                request=_FakeRequest({"authorization": "Bearer tok"}),
                payload=_payload(USER_ID),
                db=db,
            )
    assert updated.canvas_data == canvas


@pytest.mark.asyncio
async def test_topology_validate_internal_handler():
    from app.config import settings
    from app.routes.topologies import validate_topology_internal

    async with TestSession() as db:
        topo = await _make_topology(db, name="V", canvas={"nodes": [], "edges": []})
        with patch.object(settings, "internal_api_token", "tok"):
            result = await validate_topology_internal(
                topology_id=topo.id, x_internal_token="tok", db=db
            )
    assert result.valid is True
    assert result.invalid_edges == []


@pytest.mark.asyncio
async def test_topology_validate_internal_wrong_token():
    from app.config import settings
    from app.routes.topologies import validate_topology_internal
    from fastapi import HTTPException

    async with TestSession() as db:
        topo = await _make_topology(db, name="V", canvas=None)
        with patch.object(settings, "internal_api_token", "right"):
            with pytest.raises(HTTPException) as exc:
                await validate_topology_internal(
                    topology_id=topo.id, x_internal_token="wrong", db=db
                )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_topology_validate_internal_not_found():
    from app.config import settings
    from app.routes.topologies import validate_topology_internal
    from fastapi import HTTPException

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await validate_topology_internal(
                    topology_id=uuid.uuid4(), x_internal_token="tok", db=db
                )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_topology_validate_handler_reports_unreachable_and_missing():
    from app.routes.topologies import validate_topology

    a, b = uuid.uuid4(), uuid.uuid4()
    canvas = {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": str(a)}}},
            {"id": "nB", "data": {"device": {"id": str(b)}}},
            {"id": "nC", "data": {}},  # no device id
        ],
        "edges": [
            {"id": "no-path", "source": "nA", "target": "nB", "data": {"layer": "L2"}},
            {"id": "missing", "source": "nA", "target": "nC", "data": {"layer": "L2"}},
            {"id": "proposal", "source": "nA", "target": "nB", "data": {"isProposal": True}},
        ],
    }
    async with TestSession() as db:
        topo = await _make_topology(db, name="Bad", canvas=canvas, payload=_payload(USER_ID))
        result = await validate_topology(topology_id=topo.id, payload=_payload(USER_ID), db=db)
    assert result.valid is False
    reasons = {e.edge_id: e.reason for e in result.invalid_edges}
    assert reasons["no-path"] == "no_path"
    assert reasons["missing"] == "missing_device"
    assert "proposal" not in reasons


@pytest.mark.asyncio
async def test_topology_validate_handler_forbidden_for_non_owner():
    from app.routes.topologies import validate_topology
    from fastapi import HTTPException

    async with TestSession() as db:
        topo = await _make_topology(db, name="Owned", canvas=None, payload=_payload(USER_ID))
        with pytest.raises(HTTPException) as exc:
            await validate_topology(topology_id=topo.id, payload=_payload(OTHER_ID), db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_topology_validate_handler_not_found():
    from app.routes.topologies import validate_topology
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await validate_topology(topology_id=uuid.uuid4(), payload=_payload(), db=db)
    assert exc.value.status_code == 404


# --- routes/versions.py -----------------------------------------------------


async def _topology_with_versions(db, n: int, payload=None):
    """Create a topology plus n canvas versions; returns (topology, [versions])."""
    from app.models.topology import TopologyVersion

    p = payload or _payload()
    topo = await _make_topology(db, name="Versioned", canvas=None, payload=p)
    versions = []
    for i in range(1, n + 1):
        canvas = {"nodes": [{"id": f"n{i}"}], "edges": []}
        v = TopologyVersion(
            topology_id=topo.id,
            version_number=i,
            canvas_data=canvas,
            name=topo.name,
            description=f"v{i}",
            created_by=uuid.UUID(p["sub"]),
            author_name=p.get("username", ""),
        )
        db.add(v)
        versions.append(v)
    topo.canvas_data = versions[-1].canvas_data
    await db.commit()
    for v in versions:
        await db.refresh(v)
    await db.refresh(topo)
    return topo, versions


@pytest.mark.asyncio
async def test_versions_list_handler():
    from app.routes.versions import list_versions

    async with TestSession() as db:
        topo, _ = await _topology_with_versions(db, 3)
        result = await list_versions(
            topology_id=topo.id, skip=0, limit=2, payload=_payload(), db=db
        )
    assert result.total == 3
    assert len(result.items) == 2
    assert [v.version_number for v in result.items] == [3, 2]


@pytest.mark.asyncio
async def test_versions_list_topology_not_found():
    from app.routes.versions import list_versions
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await list_versions(
                topology_id=uuid.uuid4(), skip=0, limit=50, payload=_payload(), db=db
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_versions_get_handler_and_wrong_topology():
    from app.routes.versions import get_version
    from fastapi import HTTPException

    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 1)
        got = await get_version(
            topology_id=topo.id, version_id=versions[0].id, payload=_payload(), db=db
        )
        assert got.id == versions[0].id

        # A version id that does not belong to this topology is a 404.
        other_topo, other_versions = await _topology_with_versions(db, 1)
        with pytest.raises(HTTPException) as exc:
            await get_version(
                topology_id=topo.id,
                version_id=other_versions[0].id,
                payload=_payload(),
                db=db,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_versions_diff_handler():
    from app.routes.versions import diff_versions

    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 2)
        result = await diff_versions(
            topology_id=topo.id,
            a=versions[0].id,
            b=versions[1].id,
            payload=_payload(),
            db=db,
        )
    # v1 has n1, v2 has n2: one added, one removed.
    assert [n["id"] for n in result.nodes_added] == ["n2"]
    assert [n["id"] for n in result.nodes_removed] == ["n1"]


@pytest.mark.asyncio
async def test_versions_restore_handler_applies_and_snapshots():
    from app.routes import versions as ver_routes
    from app.schemas.topology import TopologyRestoreRequest

    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 2, payload=_payload(USER_ID))
        v_one = versions[0]
        with patch.object(ver_routes, "find_blocking_reservations", new=AsyncMock(return_value=[])):
            result = await ver_routes.restore_version(
                topology_id=topo.id,
                version_id=v_one.id,
                body=TopologyRestoreRequest(description="rollback", restore_name=True),
                request=_FakeRequest({"authorization": "Bearer tok"}),
                payload=_payload(USER_ID),
                db=db,
            )
    assert result.canvas_data == v_one.canvas_data

    from app.models.topology import TopologyVersion
    from sqlalchemy import select

    async with TestSession() as db:
        latest = (
            (
                await db.execute(
                    select(TopologyVersion)
                    .where(TopologyVersion.topology_id == topo.id)
                    .order_by(TopologyVersion.version_number.desc())
                )
            )
            .scalars()
            .first()
        )
    assert latest.version_number == 3
    assert latest.restored_from_id == v_one.id
    assert latest.description == "rollback"


@pytest.mark.asyncio
async def test_versions_restore_default_description():
    from app.routes import versions as ver_routes
    from app.schemas.topology import TopologyRestoreRequest
    from sqlalchemy import select

    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 2, payload=_payload(USER_ID))
        with patch.object(ver_routes, "find_blocking_reservations", new=AsyncMock(return_value=[])):
            await ver_routes.restore_version(
                topology_id=topo.id,
                version_id=versions[0].id,
                body=TopologyRestoreRequest(),
                request=_FakeRequest(),
                payload=_payload(USER_ID),
                db=db,
            )
        from app.models.topology import TopologyVersion

        latest = (
            (
                await db.execute(
                    select(TopologyVersion)
                    .where(TopologyVersion.topology_id == topo.id)
                    .order_by(TopologyVersion.version_number.desc())
                )
            )
            .scalars()
            .first()
        )
    # No token in the request, so the guard is skipped; default description used.
    assert latest.description == "Restored from v1"


@pytest.mark.asyncio
async def test_versions_restore_blocked_by_active_reservation():
    from app.routes import versions as ver_routes
    from app.schemas.topology import TopologyRestoreRequest
    from fastapi import HTTPException

    blocking = [{"id": "r1", "status": "ACTIVE", "end_time": "t"}]
    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 1, payload=_payload(USER_ID))
        with patch.object(
            ver_routes, "find_blocking_reservations", new=AsyncMock(return_value=blocking)
        ):
            with pytest.raises(HTTPException) as exc:
                await ver_routes.restore_version(
                    topology_id=topo.id,
                    version_id=versions[0].id,
                    body=TopologyRestoreRequest(),
                    request=_FakeRequest({"authorization": "Bearer tok"}),
                    payload=_payload(USER_ID),
                    db=db,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail["reservations"][0]["id"] == "r1"


@pytest.mark.asyncio
async def test_versions_restore_forbidden_for_non_owner():
    from app.routes import versions as ver_routes
    from app.schemas.topology import TopologyRestoreRequest
    from fastapi import HTTPException

    async with TestSession() as db:
        topo, versions = await _topology_with_versions(db, 1, payload=_payload(USER_ID))
        with pytest.raises(HTTPException) as exc:
            await ver_routes.restore_version(
                topology_id=topo.id,
                version_id=versions[0].id,
                body=TopologyRestoreRequest(),
                request=_FakeRequest(),
                payload=_payload(OTHER_ID),
                db=db,
            )
    assert exc.value.status_code == 403


# --- routes/bulk.py + services/bulk_service.py -----------------------------


@pytest.mark.asyncio
async def test_bulk_export_handler_json_and_csv():
    from app.routes.bulk import export_topologies

    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(uuid.uuid4()), "name": "sw-a"}}},
            {"id": "n2", "data": {"device": {"id": str(uuid.uuid4()), "name": "sw-b"}}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }
    async with TestSession() as db:
        await _make_topology(db, name="Lab", canvas=canvas)
        json_resp = await export_topologies(format="json", db=db, _=_payload())
        csv_resp = await export_topologies(format="csv", db=db, _=_payload())
    assert json_resp.media_type == "application/json"
    assert b"sw-a" in json_resp.body
    assert json_resp.headers["Content-Disposition"].endswith('.json"')
    assert csv_resp.media_type == "text/csv"
    assert b"sw-a" in csv_resp.body and b"L1" in csv_resp.body


@pytest.mark.asyncio
async def test_bulk_import_handler_creates_topology():
    import io
    import json

    from app.routes import bulk as bulk_routes

    da, db_ = str(uuid.uuid4()), str(uuid.uuid4())
    # Seed a cable so the imported edge is reachable.
    async with TestSession() as db:
        await _seed_cable(db, uuid.UUID(da), "eth0", uuid.UUID(db_), "eth0")

    items = [
        {
            "name": "Imported",
            "canvas": {
                "nodes": [
                    {"id": "n1", "data": {"device": {"name": "sw-a"}}},
                    {"id": "n2", "data": {"device": {"name": "sw-b"}}},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
            },
        }
    ]
    upload = MagicMock()
    upload.read = AsyncMock(return_value=json.dumps(items).encode())

    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={"sw-a": da, "sw-b": db_}),
        ):
            report = await bulk_routes.import_topologies_endpoint(
                file=upload, format="json", dry_run=False, db=db, payload=_payload()
            )
    assert report.created == 1
    assert report.rejected == 0
    # io import retained for symmetry with the file-based suite; silence linters.
    _ = io


# --- services/bulk_service.py: parsing/error branches ----------------------


def test_parse_json_invalid_json_raises_422():
    from app.services.bulk_service import parse_json_topologies
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b"{not json")
    assert exc.value.status_code == 422
    # The detail prefixes the underlying json.JSONDecodeError message verbatim.
    assert exc.value.detail.startswith("Invalid JSON: ")


def test_parse_json_bare_list_accepted():
    from app.services.bulk_service import parse_json_topologies

    assert parse_json_topologies(b'[{"name": "A"}]') == [{"name": "A"}]


def test_parse_json_wrong_shape_raises_422():
    from app.services.bulk_service import parse_json_topologies
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b'{"resource": "topologies"}')
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "JSON import must be a list of topologies or an object with an 'items' list"
    )


def test_parse_json_items_not_a_list_raises_422():
    from app.services.bulk_service import parse_json_topologies
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b'{"items": "nope"}')
    assert exc.value.status_code == 422
    assert exc.value.detail == "'items' must be a list"


def test_parse_csv_groups_rows_into_topologies():
    from app.services.bulk_service import parse_csv_topologies

    raw = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "Lab,sw-a,eth0,sw-b,eth1,L1\n"
        ",ignored,,,,\n"  # blank topology_name row is skipped
    ).encode()
    records = parse_csv_topologies(raw)
    assert len(records) == 1
    rec = records[0]
    assert rec["name"] == "Lab"
    names = {n["data"]["device"]["name"] for n in rec["canvas"]["nodes"]}
    assert names == {"sw-a", "sw-b"}
    assert len(rec["canvas"]["edges"]) == 1


def test_parse_csv_merges_same_name_topology_rows():
    """Two CSV rows sharing a topology_name merge into one record.

    parse_csv_topologies keys buckets by topology_name via setdefault, so both
    rows land in the same bucket: every distinct device name becomes one node
    (de-duplicated), and each row contributes one edge.
    """
    from app.services.bulk_service import parse_csv_topologies

    raw = (
        "topology_name,source_device,source_port,target_device,target_port,layer\n"
        "Lab,sw-a,eth0,sw-b,eth1,L1\n"
        "Lab,sw-b,eth2,sw-c,eth3,L2\n"
    ).encode()
    records = parse_csv_topologies(raw)

    # Both rows merge: a single topology record, not two.
    assert len(records) == 1
    rec = records[0]
    assert rec["name"] == "Lab"

    # sw-b appears in both rows but is de-duplicated to a single node.
    names = sorted(n["data"]["device"]["name"] for n in rec["canvas"]["nodes"])
    assert names == ["sw-a", "sw-b", "sw-c"]

    # Both edges are retained, keyed to the synthesized node ids.
    edges = rec["canvas"]["edges"]
    assert len(edges) == 2
    endpoints = {(e["source"], e["target"]) for e in edges}
    assert endpoints == {("node-sw-a", "node-sw-b"), ("node-sw-b", "node-sw-c")}
    # Per-row edge attributes survive the merge.
    layers = sorted(e["data"]["layer"] for e in edges)
    assert layers == ["L1", "L2"]


def test_records_to_csv_null_canvas_emits_header_only():
    """Exporting a topology with canvas_data=None yields valid header-only CSV.

    topology_to_csv_rows reads `topology.canvas_data or {}`, so a None canvas
    flattens to zero edge rows without raising; the writer still emits the
    column header.
    """
    from app.models.topology import Topology
    from app.services.bulk_service import TOPOLOGY_CSV_COLUMNS, records_to_csv

    topo = Topology(name="Empty Lab", created_by=USER_ID, canvas_data=None)
    out = records_to_csv([topo])

    lines = out.splitlines()
    # Exactly the header row, no edge rows.
    assert lines[0] == ",".join(TOPOLOGY_CSV_COLUMNS)
    assert len(lines) == 1


def test_rewrite_canvas_names_reports_unresolved():
    from app.services.bulk_service import rewrite_canvas_names_to_ids

    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"name": "known"}}},
            {"id": "n2", "data": {"device": {"name": "ghost"}}},
            {"id": "n3", "data": {}},  # no device name: skipped
        ],
        "edges": [],
    }
    rewritten, unresolved = rewrite_canvas_names_to_ids(canvas, {"known": "id-1"})
    assert unresolved == ["ghost"]
    assert rewritten["nodes"][0]["data"]["device"]["id"] == "id-1"


@pytest.mark.asyncio
async def test_import_unknown_format_raises_422():
    from app.services.bulk_service import import_topologies
    from fastapi import HTTPException

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await import_topologies(db, b"", "xml", False, USER_ID, "viewer")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_import_resolver_failure_raises_503():
    import json

    from app.services.bulk_service import import_topologies
    from fastapi import HTTPException

    raw = json.dumps(
        [
            {
                "name": "Lab",
                "canvas": {"nodes": [{"id": "n1", "data": {"device": {"name": "x"}}}], "edges": []},
            }
        ]
    ).encode()
    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(side_effect=RuntimeError("inventory down")),
        ):
            with pytest.raises(HTTPException) as exc:
                await import_topologies(db, raw, "json", True, USER_ID, "viewer")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_import_validation_failure_rejects_row():
    import json

    from app.services.bulk_service import import_topologies

    da, db_ = str(uuid.uuid4()), str(uuid.uuid4())
    # Both names resolve, but no cable is seeded so the A-B edge has no path:
    # the validator rejects the row.
    raw = json.dumps(
        [
            {
                "name": "Unreachable",
                "canvas": {
                    "nodes": [
                        {"id": "n1", "data": {"device": {"name": "a"}}},
                        {"id": "n2", "data": {"device": {"name": "b"}}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}
                    ],
                },
            }
        ]
    ).encode()
    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={"a": da, "b": db_}),
        ):
            report = await import_topologies(db, raw, "json", True, USER_ID, "viewer")
    assert report.rejected == 1
    assert "topology validation failed" in report.rows[0].reason


@pytest.mark.asyncio
async def test_import_unexpected_exception_rejects_row():
    import json

    from app.services import bulk_service

    raw = json.dumps([{"name": "Boom", "canvas": {"nodes": [], "edges": []}}]).encode()
    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={}),
        ):
            with patch(
                "app.routes.topologies._run_topology_validation",
                new=AsyncMock(side_effect=RuntimeError("kaboom")),
            ):
                # not a dry run, so the loop rolls back and records the failure.
                report = await bulk_service.import_topologies(
                    db, raw, "json", False, USER_ID, "viewer"
                )
    assert report.rejected == 1
    assert "kaboom" in report.rows[0].reason


@pytest.mark.asyncio
async def test_import_http_exception_inside_loop_rejects_row():
    import json

    from app.services import bulk_service
    from fastapi import HTTPException

    raw = json.dumps([{"name": "BadHttp", "canvas": {"nodes": [], "edges": []}}]).encode()
    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={}),
        ):
            with patch(
                "app.routes.topologies._run_topology_validation",
                new=AsyncMock(side_effect=HTTPException(status_code=400, detail="nope")),
            ):
                report = await bulk_service.import_topologies(
                    db, raw, "json", False, USER_ID, "viewer"
                )
    assert report.rejected == 1
    assert "nope" in report.rows[0].reason


@pytest.mark.asyncio
async def test_validate_skips_malformed_device_uuid():
    """A node whose device id is not a valid UUID is skipped (treated as no device)."""
    from app.routes.topologies import validate_topology

    canvas = {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": "not-a-uuid"}}},
            {"id": "nB", "data": {"device": {"id": str(uuid.uuid4())}}},
        ],
        "edges": [{"id": "e1", "source": "nA", "target": "nB", "data": {"layer": "L2"}}],
    }
    async with TestSession() as db:
        topo = await _make_topology(db, name="Malformed", canvas=canvas, payload=_payload(USER_ID))
        result = await validate_topology(topology_id=topo.id, payload=_payload(USER_ID), db=db)
    # nA's device id was unparseable, so the edge has a missing source device.
    assert result.valid is False
    assert result.invalid_edges[0].reason == "missing_device"


@pytest.mark.asyncio
async def test_import_missing_name_and_unresolved_rejected():
    import json

    from app.services.bulk_service import import_topologies

    raw = json.dumps(
        [
            {"canvas": {"nodes": [], "edges": []}},  # missing name
            {
                "name": "Ghosts",
                "canvas": {
                    "nodes": [{"id": "n1", "data": {"device": {"name": "ghost"}}}],
                    "edges": [],
                },
            },
        ]
    ).encode()
    async with TestSession() as db:
        with patch(
            "app.services.bulk_service.resolve_device_names",
            new=AsyncMock(return_value={}),
        ):
            report = await import_topologies(db, raw, "json", True, USER_ID, "viewer")
    assert report.rejected == 2
    reasons = [r.reason for r in report.rows]
    assert any("name" in (r or "") for r in reasons)
    assert any("unresolved device names" in (r or "") for r in reasons)


# --- routes/connections.py --------------------------------------------------


@pytest.mark.asyncio
async def test_connections_internal_handler_valid_token():
    """The /connections/internal handler returns the page on a matching token."""
    from app.config import settings
    from app.routes.connections import list_connections_internal

    a, b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth1")
        with patch.object(settings, "internal_api_token", "tok"):
            result = await list_connections_internal(
                device_id=None, skip=0, limit=50, x_internal_token="tok", db=db
            )
    assert result.total == 1
    assert result.items[0].device_a_id == a


@pytest.mark.asyncio
async def test_connections_internal_handler_wrong_token():
    from app.config import settings
    from app.routes.connections import list_connections_internal
    from fastapi import HTTPException

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "right"):
            with pytest.raises(HTTPException) as exc:
                await list_connections_internal(
                    device_id=None, skip=0, limit=50, x_internal_token="wrong", db=db
                )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid internal token"


@pytest.mark.asyncio
async def test_connections_get_handler_found_and_404():
    from app.routes.connections import get_connection_endpoint
    from fastapi import HTTPException

    a, b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth1")
        from app.models.connection import Connection
        from sqlalchemy import select

        existing = (await db.execute(select(Connection))).scalars().one()
        got = await get_connection_endpoint(connection_id=existing.id, _=_payload(), db=db)
        assert got.id == existing.id

        with pytest.raises(HTTPException) as exc:
            await get_connection_endpoint(connection_id=uuid.uuid4(), _=_payload(), db=db)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Connection not found"


@pytest.mark.asyncio
async def test_connections_delete_handler_removes_and_404s():
    """Deleting an existing connection succeeds and logs; a second delete of the
    same id then raises a 404 with the exact wording."""
    from app.models.connection import Connection
    from app.routes.connections import delete_connection_endpoint
    from fastapi import HTTPException
    from sqlalchemy import select

    a, b = uuid.uuid4(), uuid.uuid4()
    admin = _payload(ADMIN_ID, username="admin", role="admin")
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth1")
        existing = (await db.execute(select(Connection))).scalars().one()
        # Successful delete: returns None (204) and logs the removal.
        result = await delete_connection_endpoint(connection_id=existing.id, payload=admin, db=db)
        assert result is None
        gone = (await db.execute(select(Connection))).scalars().all()
        assert gone == []

        with pytest.raises(HTTPException) as exc:
            await delete_connection_endpoint(connection_id=existing.id, payload=admin, db=db)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Connection not found"


# --- services/version_diff.py ----------------------------------------------


def test_version_diff_index_skips_non_dict_and_missing_id():
    """_index_by_id drops non-dict members and dicts with no id (lines 8, 11)."""
    from app.services.version_diff import diff_collection

    before = [
        {"id": "keep", "v": 1},
        "not-a-dict",  # non-dict member: skipped (line 8)
        {"no": "id"},  # dict without an id: skipped (line 11)
    ]
    after = [{"id": "keep", "v": 2}]
    added, removed, modified = diff_collection(before, after)
    # Only the id-bearing dict survives indexing, so the malformed entries
    # contribute nothing to added/removed.
    assert added == []
    assert removed == []
    assert [m["id"] for m in modified] == ["keep"]


def test_version_diff_canvas_tolerates_malformed_collections():
    """diff_canvas runs the same skipping logic for both nodes and edges."""
    from app.services.version_diff import diff_canvas

    before = {"nodes": [None, {"id": "n1"}], "edges": [{"weird": True}]}
    after = {"nodes": [{"id": "n1"}], "edges": [{"id": "e1"}]}
    result = diff_canvas(before, after)
    assert result["nodes_added"] == []
    assert result["nodes_removed"] == []
    assert [e["id"] for e in result["edges_added"]] == ["e1"]


def test_version_diff_reports_modified_edge():
    """An edge present in both canvases but changed appears in edges_modified.

    diff_collection matches by id; a same-id dict whose value differs yields a
    modified entry carrying the id plus its before/after snapshots.
    """
    from app.services.version_diff import diff_canvas

    before = {
        "nodes": [{"id": "n1"}, {"id": "n2"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}}],
    }
    after = {
        "nodes": [{"id": "n1"}, {"id": "n2"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L2"}}],
    }
    result = diff_canvas(before, after)

    # Same edge id on both sides, so it is neither added nor removed.
    assert result["edges_added"] == []
    assert result["edges_removed"] == []

    modified = result["edges_modified"]
    assert len(modified) == 1
    entry = modified[0]
    assert entry["id"] == "e1"
    assert entry["before"]["data"]["layer"] == "L1"
    assert entry["after"]["data"]["layer"] == "L2"


# --- services/fork_service.py + routes/forks.py ----------------------------


async def _make_parent_topology(db, canvas, *, with_version=True):
    """Create a parent Topology, optionally with a v1 TopologyVersion.

    Returns (topology_id, version_id_or_None).
    """
    from app.models.topology import Topology, TopologyVersion

    topo = Topology(name="parent", created_by=uuid.uuid4(), canvas_data=canvas)
    db.add(topo)
    await db.flush()
    version_id = None
    if with_version:
        version = TopologyVersion(
            topology_id=topo.id,
            version_number=1,
            canvas_data=canvas,
            name="parent",
            created_by=uuid.uuid4(),
        )
        db.add(version)
        await db.flush()
        version_id = version.id
    await db.commit()
    return topo.id, version_id


@pytest.mark.asyncio
async def test_fork_resolve_explicit_version_pins_it():
    """_resolve_parent_canvas with an explicit parent_version_id pins that version
    (fork_service.py lines 55-57)."""
    from app.services.fork_service import _resolve_parent_canvas

    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    async with TestSession() as db:
        topo_id, version_id = await _make_parent_topology(db, canvas)
        resolved_canvas, pinned = await _resolve_parent_canvas(
            db, parent_topology_id=None, parent_version_id=version_id
        )
    assert resolved_canvas == canvas
    assert pinned == version_id


@pytest.mark.asyncio
async def test_fork_resolve_topology_without_versions_uses_live_canvas():
    """A parent topology with no versions falls back to its live canvas, unpinned
    (fork_service.py lines 70-72)."""
    from app.services.fork_service import _resolve_parent_canvas

    canvas = {"nodes": [{"id": "live"}], "edges": []}
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas, with_version=False)
        resolved_canvas, pinned = await _resolve_parent_canvas(
            db, parent_topology_id=topo_id, parent_version_id=None
        )
    assert resolved_canvas == canvas
    assert pinned is None


@pytest.mark.asyncio
async def test_fork_resolve_unknown_topology_returns_none():
    """An unknown parent topology with no version yields (None, None): nothing to copy."""
    from app.services.fork_service import _resolve_parent_canvas

    async with TestSession() as db:
        resolved_canvas, pinned = await _resolve_parent_canvas(
            db, parent_topology_id=uuid.uuid4(), parent_version_id=None
        )
    assert resolved_canvas is None
    assert pinned is None


@pytest.mark.asyncio
async def test_fork_resolve_missing_explicit_version_falls_through():
    """An explicit parent_version_id that does not exist falls through to the
    topology branch rather than returning the missing version."""
    from app.services.fork_service import _resolve_parent_canvas

    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas, with_version=False)
        resolved_canvas, pinned = await _resolve_parent_canvas(
            db, parent_topology_id=topo_id, parent_version_id=uuid.uuid4()
        )
    # The bogus version id resolved to nothing, so resolution used the topology's
    # current canvas instead (unpinned, since the topology has no versions).
    assert resolved_canvas == canvas
    assert pinned is None


def test_fork_node_to_device_map_skips_malformed_nodes():
    """node_to_device_map drops nodes missing an id or device id and nodes whose
    device id is not a UUID. The shared resolver moved to fork_save_service in P3a."""
    from app.services.fork_save_service import node_to_device_map as _node_to_device_map

    good = uuid.uuid4()
    canvas = {
        "nodes": [
            {"id": "n1", "data": {"device": {"id": str(good)}}},
            {"id": "n2", "data": {"device": {}}},  # no device id: skipped (line 85)
            {"data": {"device": {"id": str(uuid.uuid4())}}},  # no node id: skipped (85)
            {"id": "n4", "data": {"device": {"id": "not-a-uuid"}}},  # bad UUID (88-89)
        ]
    }
    mapping = _node_to_device_map(canvas)
    assert mapping == {"n1": good}


@pytest.mark.asyncio
async def test_fork_create_snapshots_multi_hop_path_and_dedupes():
    """create_fork snapshots a two-hop path A-X-B as two L1 fork_connections, each
    carrying its backing physical connection id, and a second canvas edge sharing a
    hop is de-duplicated (fork_service.py 126-151, 169-194, and the seen-key skip)."""
    from app.models.connection import Connection
    from app.models.fork import ForkConnection, ReservationFork
    from app.services.fork_service import create_fork
    from sqlalchemy import select

    dev_a, dev_x, dev_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, dev_a, "a-x", dev_x, "x-a")
        await _seed_cable(db, dev_x, "x-b", dev_b, "b-x")
        all_cables = (await db.execute(select(Connection))).scalars().all()
        phys_by_pair = {}
        for c in all_cables:
            phys_by_pair[(c.device_a_id, c.device_b_id)] = c.id

    # Two canvas edges A-B: both resolve to the same A-X-B path, so the second
    # edge's hops are already in `seen` and must not double-insert.
    canvas = {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": str(dev_a)}}},
            {"id": "nX", "data": {"device": {"id": str(dev_x)}}},
            {"id": "nB", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [
            {"id": "e1", "source": "nA", "target": "nB"},
            {"id": "e2", "source": "nA", "target": "nB"},
        ],
    }
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas)
        fork = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids={dev_a, dev_x, dev_b},
            created_by="booker",
        )
    async with TestSession() as db:
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
    # Two distinct hops on the path, de-duplicated across the two canvas edges.
    assert len(conns) == 2
    pairs = {(c.device_a_id, c.device_b_id) for c in conns}
    assert pairs == {(dev_a, dev_x), (dev_x, dev_b)}
    for c in conns:
        assert c.layer == "L1"
        assert c.created_by == "booker"
        assert c.physical_connection_id == phys_by_pair[(c.device_a_id, c.device_b_id)]
    # The fork itself was persisted and is ACTIVE.
    async with TestSession() as db:
        stored = (
            await db.execute(select(ReservationFork).where(ReservationFork.id == fork.id))
        ).scalar_one()
        from app.models.fork import ForkStatus_ACTIVE

        assert stored.status == ForkStatus_ACTIVE


@pytest.mark.asyncio
async def test_fork_snapshot_skips_proposal_and_unresolvable_edges():
    """_snapshot_connections skips proposal edges (155-156) and edges whose
    endpoints do not map to devices (159-160), wiring nothing for either."""
    from app.models.fork import ForkConnection
    from app.services.fork_service import create_fork
    from sqlalchemy import select

    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, dev_a, "eth0", dev_b, "eth1")
    canvas = {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": str(dev_a)}}},
            {"id": "nB", "data": {"device": {"id": str(dev_b)}}},
            {"id": "nGhost", "data": {}},  # no device: unresolvable endpoint
        ],
        "edges": [
            # A proposal edge is skipped even though the path exists.
            {"id": "prop", "source": "nA", "target": "nB", "data": {"isProposal": True}},
            # An edge into the ghost node has an unresolvable target.
            {"id": "ghost", "source": "nA", "target": "nGhost"},
        ],
    }
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas)
        fork = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids={dev_a, dev_b},
        )
    async with TestSession() as db:
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
    assert conns == []


@pytest.mark.asyncio
async def test_fork_snapshot_empty_component_when_no_devices_resolve():
    """A canvas with edges but no resolvable device nodes leaves an empty component:
    the phys-lookup else branch runs and no fork_connections are written
    (fork_service.py line 144-145)."""
    from app.models.fork import ForkConnection
    from app.services.fork_service import create_fork
    from sqlalchemy import select

    canvas = {
        "nodes": [
            {"id": "nA", "data": {}},  # no device id
            {"id": "nB", "data": {}},
        ],
        "edges": [{"id": "e1", "source": "nA", "target": "nB"}],
    }
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas)
        fork = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids=set(),
        )
    async with TestSession() as db:
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
    assert conns == []


@pytest.mark.asyncio
async def test_fork_snapshot_skips_hop_with_null_port():
    """A defensive guard: a path hop carrying a None port is skipped rather than
    written as a half-wired fork_connection (the resolver's port-None skip).

    Real connections never have NULL ports (the column is NOT NULL), so this guard
    is only reachable via a doctored path. We patch the pathfinder to return one
    so the branch is pinned rather than left dead. The resolver moved to
    fork_save_service in P3a; create_fork calls it there."""
    from app.models.fork import ForkConnection
    from app.schemas.pathfind import PathHop
    from app.services import fork_save_service, fork_service
    from sqlalchemy import select

    dev_a, dev_b = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, dev_a, "eth0", dev_b, "eth1")
    canvas = {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": str(dev_a)}}},
            {"id": "nB", "data": {"device": {"id": str(dev_b)}}},
        ],
        "edges": [{"id": "e1", "source": "nA", "target": "nB"}],
    }
    # The middle hop's port_in is None, so the (first, second) pair has pb=None.
    doctored = [
        [
            PathHop(device_id=dev_a, port_out="eth0"),
            PathHop(device_id=dev_b, port_in=None, port_out=None),
        ]
    ]
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas)
        with patch.object(
            fork_save_service,
            "find_all_shortest_paths_async",
            new=AsyncMock(return_value=doctored),
        ):
            fork = await fork_service.create_fork(
                db,
                reservation_id=rid,
                parent_topology_id=topo_id,
                parent_version_id=None,
                member_device_ids={dev_a, dev_b},
            )
    async with TestSession() as db:
        conns = (
            (await db.execute(select(ForkConnection).where(ForkConnection.fork_id == fork.id)))
            .scalars()
            .all()
        )
    assert conns == []


@pytest.mark.asyncio
async def test_fork_create_is_idempotent_returns_existing():
    """A second create_fork for the same reservation returns the first fork without
    building a second (fork_service.py lines 216-217)."""
    from app.models.fork import ReservationFork
    from app.services.fork_service import create_fork
    from sqlalchemy import select

    canvas = {"nodes": [], "edges": []}
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, _ = await _make_parent_topology(db, canvas)
        first = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids=set(),
        )
        second = await create_fork(
            db,
            reservation_id=rid,
            parent_topology_id=topo_id,
            parent_version_id=None,
            member_device_ids=set(),
        )
    assert first.id == second.id
    async with TestSession() as db:
        forks = (
            (await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid)))
            .scalars()
            .all()
        )
    assert len(forks) == 1


@pytest.mark.asyncio
async def test_fork_create_integrity_error_returns_concurrent_winner():
    """If the commit hits a unique-violation (a concurrent activation won the race),
    create_fork rolls back and returns the existing winner so the contract stays
    idempotent (fork_service.py lines 246-257)."""
    from app.models.fork import ReservationFork
    from app.services import fork_service
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    rid = uuid.uuid4()
    winner_holder = {"id": None}

    async with TestSession() as db:
        # create_fork's existence check finds nothing, so it builds and flushes its
        # own fork. The first commit raises as if a concurrent activation had
        # committed the same reservation_id first. create_fork then rolls back
        # (discarding its flushed row) and re-reads to find the winner; we splice the
        # winner in on the back of that real rollback so the recovery SELECT sees it.
        real_commit = db.commit
        real_rollback = db.rollback

        async def _commit_collides():
            raise IntegrityError("dup", {}, Exception("unique"))

        async def _rollback_then_seed_winner():
            await real_rollback()
            # Now the session is clean; persist the winner the recovery SELECT will
            # return, exactly as a concurrent committer would have left it.
            winner = ReservationFork(reservation_id=rid)
            db.add(winner)
            await real_commit()
            winner_holder["id"] = winner.id

        with patch.object(db, "commit", new=_commit_collides):
            with patch.object(db, "rollback", new=_rollback_then_seed_winner):
                result = await fork_service.create_fork(
                    db,
                    reservation_id=rid,
                    parent_topology_id=None,
                    parent_version_id=None,
                    member_device_ids=set(),
                )
    assert result.id == winner_holder["id"]
    # Exactly one fork survived: the concurrent winner, not a duplicate.
    async with TestSession() as db:
        forks = (
            (await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid)))
            .scalars()
            .all()
        )
    assert len(forks) == 1


@pytest.mark.asyncio
async def test_fork_create_integrity_error_no_winner_reraises():
    """If the commit fails with IntegrityError but no existing fork is found on
    recovery, create_fork re-raises rather than silently swallowing (lines 255-256)."""
    from app.services import fork_service
    from sqlalchemy.exc import IntegrityError

    rid = uuid.uuid4()
    async with TestSession() as db:

        async def _always_fail():
            raise IntegrityError("dup", {}, Exception("unique"))

        with patch.object(db, "commit", new=_always_fail):
            with pytest.raises(IntegrityError):
                await fork_service.create_fork(
                    db,
                    reservation_id=rid,
                    parent_topology_id=None,
                    parent_version_id=None,
                    member_device_ids=set(),
                )


@pytest.mark.asyncio
async def test_forks_route_handler_returns_version_number():
    """The create_fork_internal handler builds the ForkCreateResponse with the fork
    id and v1 version number (routes/forks.py lines 51-57)."""
    from app.config import settings
    from app.routes.forks import create_fork_internal
    from app.schemas.fork import ForkCreate

    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    rid = uuid.uuid4()
    async with TestSession() as db:
        topo_id, version_id = await _make_parent_topology(db, canvas)
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await create_fork_internal(
                body=ForkCreate(
                    reservation_id=rid, parent_topology_id=topo_id, member_device_ids=[]
                ),
                x_internal_token="tok",
                db=db,
            )
    assert resp.version_number == 1
    assert resp.fork_id is not None


@pytest.mark.asyncio
async def test_forks_route_handler_rejects_bad_token():
    from app.config import settings
    from app.routes.forks import create_fork_internal
    from app.schemas.fork import ForkCreate
    from fastapi import HTTPException

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "right"):
            with pytest.raises(HTTPException) as exc:
                await create_fork_internal(
                    body=ForkCreate(reservation_id=uuid.uuid4(), member_device_ids=[]),
                    x_internal_token="wrong",
                    db=db,
                )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid internal token"


# --- routes/forks.py: remaining handlers, called directly ------------------
#
# coverage.py's tracer loses line attribution for these handler bodies when they
# run only through the ASGI transport path (the async/greenlet post-await gap
# documented for this repo): test_forks.py, test_fork_versions.py, and
# test_fork_prune.py already pin every one of these behaviors over HTTP, and this
# section adds one direct call per handler purely so pytest-cov credits the
# bodies it already proved. Each test still asserts on real response content, not
# just "it returns".


async def _direct_create_fork(
    db, rid, *, parent_topology_id=None, parent_version_id=None, member_device_ids=frozenset()
):
    from app.config import settings
    from app.routes.forks import create_fork_internal
    from app.schemas.fork import ForkCreate

    with patch.object(settings, "internal_api_token", "tok"):
        return await create_fork_internal(
            body=ForkCreate(
                reservation_id=rid,
                parent_topology_id=parent_topology_id,
                parent_version_id=parent_version_id,
                member_device_ids=list(member_device_ids),
            ),
            x_internal_token="tok",
            db=db,
        )


@pytest.mark.asyncio
async def test_list_active_forks_handler_reports_latest_version_per_fork():
    """list_active_forks_internal pairs each ACTIVE reservation_id with its own
    latest fork_version, and both list shapes describe the same page
    (routes/forks.py lines 143-181)."""
    from app.config import settings
    from app.routes.forks import list_active_forks_internal
    from app.services.fork_save_service import save_fork

    a, b = uuid.uuid4(), uuid.uuid4()
    saved_rid, fresh_rid = uuid.uuid4(), uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        await _direct_create_fork(db, saved_rid)
        fresh_fork = await _direct_create_fork(db, fresh_rid)
        fresh_fork_id = fresh_fork.fork_id

    async with TestSession() as db:
        from app.models.fork import ReservationFork
        from sqlalchemy import select

        saved_fork = (
            await db.execute(
                select(ReservationFork).where(ReservationFork.reservation_id == saved_rid)
            )
        ).scalar_one()
        canvas = {
            "nodes": [
                {"id": "n0", "data": {"device": {"id": str(a)}}},
                {"id": "n1", "data": {"device": {"id": str(b)}}},
            ],
            "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
        }
        result = await save_fork(
            db, saved_fork, canvas_data=canvas, member_device_ids={a, b}, created_by="tester"
        )
    assert result.version_number == 2

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await list_active_forks_internal(
                skip=0, limit=200, x_internal_token="tok", db=db
            )

    assert resp.total == 2
    assert set(resp.reservation_ids) == {saved_rid, fresh_rid}
    versions = {e.reservation_id: e.latest_fork_version for e in resp.forks}
    assert versions[saved_rid] == 2
    assert versions[fresh_rid] == 1
    assert set(versions) == set(resp.reservation_ids)
    # fork_ids list-comprehension branch (line 166) ran for a non-empty page; the
    # id used to key it is not itself part of the response, only reservation_id is.
    assert fresh_fork_id is not None


@pytest.mark.asyncio
async def test_get_fork_handler_returns_full_detail():
    """get_fork_internal returns metadata, canvas, connections, and versions
    together for a fork with real wiring (routes/forks.py lines 206-237)."""
    from app.config import settings
    from app.routes.forks import get_fork_internal

    a, b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        canvas = {
            "nodes": [
                {"id": "n0", "data": {"device": {"id": str(a)}}},
                {"id": "n1", "data": {"device": {"id": str(b)}}},
            ],
            "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
        }
        topo_id, _ = await _make_parent_topology(db, canvas)
        await _direct_create_fork(db, rid, parent_topology_id=topo_id, member_device_ids={a, b})

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await get_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)

    assert resp.reservation_id == rid
    assert resp.status == "ACTIVE"
    assert len(resp.connections) == 1
    assert resp.connections[0].device_a_id == a
    assert len(resp.versions) == 1
    assert resp.versions[0].version_number == 1


@pytest.mark.asyncio
async def test_get_fork_handler_404_when_absent():
    from app.config import settings
    from app.routes.forks import get_fork_internal
    from fastapi import HTTPException

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await get_fork_internal(reservation_id=uuid.uuid4(), x_internal_token="tok", db=db)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Fork not found"


@pytest.mark.asyncio
async def test_get_fork_version_handler_returns_own_canvas():
    """get_fork_version_internal returns the version row's own canvas_data, distinct
    from the fork's current draft (routes/forks.py lines 264-274)."""
    from app.config import settings
    from app.routes.forks import get_fork_version_internal

    rid = uuid.uuid4()
    async with TestSession() as db:
        created = await _direct_create_fork(db, rid)

    async with TestSession() as db:
        from app.models.fork import ForkVersion
        from sqlalchemy import select

        version = (
            await db.execute(select(ForkVersion).where(ForkVersion.fork_id == created.fork_id))
        ).scalar_one()
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await get_fork_version_internal(
                reservation_id=rid, version_id=version.id, x_internal_token="tok", db=db
            )

    assert resp.version_number == 1
    assert resp.fork_id == created.fork_id
    assert resp.restored_from_id is None


@pytest.mark.asyncio
async def test_get_fork_version_handler_404_for_unknown_version():
    from app.config import settings
    from app.routes.forks import get_fork_version_internal
    from fastapi import HTTPException

    rid = uuid.uuid4()
    async with TestSession() as db:
        await _direct_create_fork(db, rid)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await get_fork_version_internal(
                    reservation_id=rid, version_id=uuid.uuid4(), x_internal_token="tok", db=db
                )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Version not found"


@pytest.mark.asyncio
async def test_restore_fork_version_handler_sets_marker_and_reports_validation():
    """restore_fork_version_internal copies the version's canvas onto the draft,
    sets draft_restored_from_id, and reports validation without gating on it
    (routes/forks.py lines 306-334)."""
    from app.config import settings
    from app.routes.forks import restore_fork_version_internal

    rid = uuid.uuid4()
    async with TestSession() as db:
        created = await _direct_create_fork(db, rid)

    async with TestSession() as db:
        from app.models.fork import ForkVersion
        from sqlalchemy import select

        version = (
            await db.execute(select(ForkVersion).where(ForkVersion.fork_id == created.fork_id))
        ).scalar_one()
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await restore_fork_version_internal(
                reservation_id=rid, version_id=version.id, x_internal_token="tok", db=db
            )

    assert resp.id == created.fork_id
    assert resp.draft_restored_from_id == version.id
    assert resp.valid is True
    assert resp.invalid_edges == []

    # The marker actually persisted on the fork row, not just on the response.
    async with TestSession() as db:
        from app.models.fork import ReservationFork

        fork_row = await db.get(ReservationFork, created.fork_id)
        assert fork_row.draft_restored_from_id == version.id


@pytest.mark.asyncio
async def test_restore_fork_version_handler_refuses_archived():
    from app.config import settings
    from app.routes.forks import archive_fork_internal, restore_fork_version_internal
    from fastapi import HTTPException

    rid = uuid.uuid4()
    async with TestSession() as db:
        created = await _direct_create_fork(db, rid)

    async with TestSession() as db:
        from app.models.fork import ForkVersion
        from sqlalchemy import select

        version = (
            await db.execute(select(ForkVersion).where(ForkVersion.fork_id == created.fork_id))
        ).scalar_one()
        version_id = version.id
        with patch.object(settings, "internal_api_token", "tok"):
            await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await restore_fork_version_internal(
                    reservation_id=rid, version_id=version_id, x_internal_token="tok", db=db
                )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_update_fork_canvas_handler_stores_draft_and_reports_invalid_edge():
    """update_fork_canvas_internal stores whatever canvas it is given, even one
    with an unreachable edge, and reports (not gates on) that invalidity
    (routes/forks.py lines 356-378)."""
    from app.config import settings
    from app.routes.forks import update_fork_canvas_internal
    from app.schemas.fork import ForkCanvasUpdate

    a, b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        await _direct_create_fork(db, rid)

    # a and b share no cable, so this edge cannot resolve to any path.
    bad_canvas = {
        "nodes": [
            {"id": "n0", "data": {"device": {"id": str(a)}}},
            {"id": "n1", "data": {"device": {"id": str(b)}}},
        ],
        "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
    }
    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await update_fork_canvas_internal(
                reservation_id=rid,
                body=ForkCanvasUpdate(canvas_data=bad_canvas),
                x_internal_token="tok",
                db=db,
            )

    assert resp.valid is False
    assert len(resp.invalid_edges) == 1
    assert resp.invalid_edges[0].edge_id == "e0"

    # The invalid draft still stored (drafts are cheap: no gating on validity).
    async with TestSession() as db:
        from app.models.fork import ReservationFork
        from sqlalchemy import select

        fork_row = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert fork_row.canvas_data == bad_canvas


@pytest.mark.asyncio
async def test_update_fork_canvas_handler_refuses_archived():
    from app.config import settings
    from app.routes.forks import archive_fork_internal, update_fork_canvas_internal
    from app.schemas.fork import ForkCanvasUpdate
    from fastapi import HTTPException

    rid = uuid.uuid4()
    async with TestSession() as db:
        await _direct_create_fork(db, rid)
        with patch.object(settings, "internal_api_token", "tok"):
            await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await update_fork_canvas_internal(
                    reservation_id=rid,
                    body=ForkCanvasUpdate(canvas_data={"nodes": [], "edges": []}),
                    x_internal_token="tok",
                    db=db,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_save_fork_handler_builds_wire_and_bumps_version():
    """save_fork_internal reconciles the submitted canvas, appends a version, and
    reports the built delta (routes/forks.py lines 419-445)."""
    from app.config import settings
    from app.routes.forks import save_fork_internal
    from app.schemas.fork import ForkSaveRequest

    a, b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        await _direct_create_fork(db, rid)

    canvas = {
        "nodes": [
            {"id": "n0", "data": {"device": {"id": str(a)}}},
            {"id": "n1", "data": {"device": {"id": str(b)}}},
        ],
        "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
    }
    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await save_fork_internal(
                reservation_id=rid,
                body=ForkSaveRequest(
                    canvas_data=canvas, member_device_ids=[a, b], created_by="tester"
                ),
                x_internal_token="tok",
                db=db,
            )

    assert resp.version_number == 2
    assert len(resp.built) == 1
    assert resp.built[0].device_a_id == a
    assert resp.built[0].port_a == "eth0"
    assert resp.released == []
    assert resp.unchanged_count == 0


@pytest.mark.asyncio
async def test_save_fork_handler_refuses_archived():
    from app.config import settings
    from app.routes.forks import archive_fork_internal, save_fork_internal
    from app.schemas.fork import ForkSaveRequest
    from fastapi import HTTPException

    rid = uuid.uuid4()
    async with TestSession() as db:
        await _direct_create_fork(db, rid)
        with patch.object(settings, "internal_api_token", "tok"):
            await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await save_fork_internal(
                    reservation_id=rid,
                    body=ForkSaveRequest(
                        canvas_data={"nodes": [], "edges": []}, member_device_ids=[]
                    ),
                    x_internal_token="tok",
                    db=db,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_prune_fork_devices_handler_releases_wiring():
    """prune_fork_devices_internal releases a removed device's saved wiring and
    reports it as changed (routes/forks.py lines 460-475)."""
    from app.config import settings
    from app.routes.forks import prune_fork_devices_internal, save_fork_internal
    from app.schemas.fork import ForkPruneRequest, ForkSaveRequest

    a, b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        await _direct_create_fork(db, rid)

    canvas = {
        "nodes": [
            {"id": "n0", "data": {"device": {"id": str(a)}}},
            {"id": "n1", "data": {"device": {"id": str(b)}}},
        ],
        "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
    }
    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            await save_fork_internal(
                reservation_id=rid,
                body=ForkSaveRequest(
                    canvas_data=canvas, member_device_ids=[a, b], created_by="tester"
                ),
                x_internal_token="tok",
                db=db,
            )

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await prune_fork_devices_internal(
                reservation_id=rid,
                body=ForkPruneRequest(device_ids=[a]),
                x_internal_token="tok",
                db=db,
            )

    assert resp.changed is True
    assert resp.version_number == 3
    assert len(resp.released) == 1
    assert resp.released[0].device_a_id == a


@pytest.mark.asyncio
async def test_prune_fork_devices_handler_refuses_archived():
    from app.config import settings
    from app.routes.forks import archive_fork_internal, prune_fork_devices_internal
    from app.schemas.fork import ForkPruneRequest
    from fastapi import HTTPException

    rid = uuid.uuid4()
    async with TestSession() as db:
        await _direct_create_fork(db, rid)
        with patch.object(settings, "internal_api_token", "tok"):
            await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            with pytest.raises(HTTPException) as exc:
                await prune_fork_devices_internal(
                    reservation_id=rid,
                    body=ForkPruneRequest(device_ids=[uuid.uuid4()]),
                    x_internal_token="tok",
                    db=db,
                )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Fork is archived and cannot be edited"


@pytest.mark.asyncio
async def test_archive_fork_handler_freezes_fork_and_is_idempotent():
    """archive_fork_internal flips an ACTIVE fork to ARCHIVED and returns 200 with
    the frozen state on a repeat call rather than re-flipping it
    (routes/forks.py lines 466-480)."""
    from app.config import settings
    from app.models.fork import ForkStatus_ARCHIVED
    from app.routes.forks import archive_fork_internal

    rid = uuid.uuid4()
    async with TestSession() as db:
        created = await _direct_create_fork(db, rid)

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            first = await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)
    assert first.status == ForkStatus_ARCHIVED
    assert first.fork_id == created.fork_id
    assert first.reservation_id == rid

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            second = await archive_fork_internal(reservation_id=rid, x_internal_token="tok", db=db)
    assert second.status == ForkStatus_ARCHIVED


@pytest.mark.asyncio
async def test_archive_fork_handler_returns_204_for_unknown_reservation():
    from app.config import settings
    from app.routes.forks import archive_fork_internal
    from starlette.responses import Response

    async with TestSession() as db:
        with patch.object(settings, "internal_api_token", "tok"):
            resp = await archive_fork_internal(
                reservation_id=uuid.uuid4(), x_internal_token="tok", db=db
            )
    assert isinstance(resp, Response)
    assert resp.status_code == 204


def test_node_to_element_map_skips_element_node_with_no_id():
    """A networkElementNode entry with no node id is dropped rather than mapped
    under a falsy key (fork_save_service.py line 135's guard clause)."""
    from app.services.fork_save_service import node_to_element_map

    canvas = {
        "nodes": [
            {"type": "networkElementNode", "data": {"element": {"id": "el-1"}}},  # no "id"
            {"id": "n2", "type": "networkElementNode", "data": {"element": {"id": "el-2"}}},
        ]
    }
    mapping = node_to_element_map(canvas)
    assert mapping == {"n2": "el-2"}


def test_prune_canvas_for_devices_none_canvas_is_a_no_op():
    """A None canvas (a fork with no saved version yet) prunes to itself with
    empty edge-id sets, rather than raising on `.get` (fork_save_service.py
    line 606's early return)."""
    from app.services.fork_save_service import prune_canvas_for_devices

    pruned, changed, remaining, dropped = prune_canvas_for_devices(None, {"anything"})
    assert pruned is None
    assert changed is False
    assert remaining == set()
    assert dropped == set()


def test_prune_canvas_for_devices_empty_canvas_is_a_no_op():
    """An empty-dict canvas is falsy too and takes the same early-return branch."""
    from app.services.fork_save_service import prune_canvas_for_devices

    empty: dict = {}
    pruned, changed, remaining, dropped = prune_canvas_for_devices(empty, {"anything"})
    assert pruned is empty
    assert changed is False
    assert remaining == set()
    assert dropped == set()


@pytest.mark.asyncio
async def test_prune_fork_devices_no_release_no_draft_change_is_a_pure_replay():
    """Pruning a device that appears nowhere (not in the saved wiring, not in the
    draft) hits the `if not to_release` branch with draft_changed False: no commit,
    no version bump, changed False (fork_save_service.py lines 727-731, the
    branch test_prune_scrubs_draft_only_content_without_a_version does not
    reach since its draft always changes)."""
    from app.services.fork_save_service import prune_fork_devices

    a, b = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        await _seed_cable(db, a, "eth0", b, "eth0")
        created = await _direct_create_fork(db, rid)

    canvas = {
        "nodes": [
            {"id": "n0", "data": {"device": {"id": str(a)}}},
            {"id": "n1", "data": {"device": {"id": str(b)}}},
        ],
        "edges": [{"id": "e0", "source": "n0", "target": "n1"}],
    }
    async with TestSession() as db:
        from app.config import settings
        from app.routes.forks import save_fork_internal
        from app.schemas.fork import ForkSaveRequest

        with patch.object(settings, "internal_api_token", "tok"):
            await save_fork_internal(
                reservation_id=rid,
                body=ForkSaveRequest(
                    canvas_data=canvas, member_device_ids=[a, b], created_by="tester"
                ),
                x_internal_token="tok",
                db=db,
            )

    unrelated = uuid.uuid4()
    async with TestSession() as db:
        from app.models.fork import ReservationFork

        fork = await db.get(ReservationFork, created.fork_id)
        result = await prune_fork_devices(db, fork, [unrelated])

    assert result.changed is False
    assert result.released == []
    # Version stayed at the last save (2): no third version was appended for a
    # no-op prune.
    assert result.version_number == 2


@pytest.mark.asyncio
async def test_prune_fork_devices_scrubs_draft_only_device_without_a_version():
    """A device that exists only in the draft (never saved) releases nothing
    (to_release stays empty), but the draft is still scrubbed and committed
    (fork_save_service.py lines 727-731's draft_changed branch), matching
    test_prune_scrubs_draft_only_content_without_a_version's HTTP-level pin,
    called directly so the commit line gets coverage credit too."""
    from app.services.fork_save_service import prune_fork_devices

    a, dut = uuid.uuid4(), uuid.uuid4()
    rid = uuid.uuid4()
    async with TestSession() as db:
        created = await _direct_create_fork(db, rid)

    async with TestSession() as db:
        from app.models.fork import ReservationFork

        fork = await db.get(ReservationFork, created.fork_id)
        # A draft edge naming a device that was never part of any save.
        fork.canvas_data = {
            "nodes": [
                {"id": "nA", "data": {"device": {"id": str(a)}}},
                {"id": "nD", "data": {"device": {"id": str(dut)}}},
            ],
            "edges": [{"id": "eDraft", "source": "nA", "target": "nD"}],
        }
        await db.commit()

        result = await prune_fork_devices(db, fork, [dut])

    assert result.changed is False
    assert result.released == []
    assert result.version_number == 1

    async with TestSession() as db:
        from app.models.fork import ReservationFork

        fork_row = await db.get(ReservationFork, created.fork_id)
        node_ids = {n["id"] for n in fork_row.canvas_data["nodes"]}
        assert node_ids == {"nA"}
        assert fork_row.canvas_data["edges"] == []
