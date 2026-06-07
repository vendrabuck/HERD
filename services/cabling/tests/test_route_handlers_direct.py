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
        result = await reservation_guard.find_blocking_reservations(topology_id, "tok")
    ids = {r["id"] for r in result}
    # Only ACTIVE/PENDING/PENDING_PROVISION rows for THIS topology are blocking.
    assert ids == {"r1", "r2"}


@pytest.mark.asyncio
async def test_reservation_guard_bad_response_returns_empty():
    from app.services import reservation_guard

    topology_id = uuid.uuid4()
    factory, _, _ = _mock_httpx_client(status_code=503, json_data=[])
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(topology_id, "tok")
    assert result == []


@pytest.mark.asyncio
async def test_reservation_guard_unreachable_fails_open():
    from app.services import reservation_guard

    topology_id = uuid.uuid4()
    factory, _, _ = _mock_httpx_client(raise_exc=RuntimeError("connection refused"))
    with patch.object(reservation_guard.httpx, "AsyncClient", factory):
        result = await reservation_guard.find_blocking_reservations(topology_id, "tok")
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


def test_parse_json_bare_list_accepted():
    from app.services.bulk_service import parse_json_topologies

    assert parse_json_topologies(b'[{"name": "A"}]') == [{"name": "A"}]


def test_parse_json_wrong_shape_raises_422():
    from app.services.bulk_service import parse_json_topologies
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b'{"resource": "topologies"}')
    assert exc.value.status_code == 422


def test_parse_json_items_not_a_list_raises_422():
    from app.services.bulk_service import parse_json_topologies
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_json_topologies(b'{"items": "nope"}')
    assert exc.value.status_code == 422


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
async def test_import_resolver_failure_raises_502():
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
    assert exc.value.status_code == 502


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
