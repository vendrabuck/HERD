"""Tests for /templates router (roadmap item #8 iteration 2)."""

import uuid

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}
OTHER_PAYLOAD = {"sub": OTHER_ID, "username": "other", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


def _override_other():
    return OTHER_PAYLOAD


test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_blank_template(user_client):
    resp = await user_client.post(
        "/templates",
        json={"name": "Standard 2-Spine", "description": "demo"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Standard 2-Spine"
    assert data["created_by"] == USER_ID
    assert data["owner_name"] == "viewer"
    assert data["canvas_data"] is None


@pytest.mark.asyncio
async def test_list_templates_paginated(user_client):
    for i in range(3):
        await user_client.post("/templates", json={"name": f"T{i}"})
    resp = await user_client.get("/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_get_template_not_found(user_client):
    resp = await user_client.get(f"/templates/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_from_topology_extracts_roles(user_client):
    """from-topology walks the canvas and replaces device ids with `<template>-N` roles."""
    create = await user_client.post("/topologies", json={"name": "Source"})
    topology_id = create.json()["id"]
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "data": {
                    "device": {"id": "uuid-1", "template_name": "PA-VM"},
                    "label": "fw-a",
                },
            },
            {
                "id": "n2",
                "data": {
                    "device": {"id": "uuid-2", "template_name": "PA-VM"},
                    "label": "fw-b",
                },
            },
            {
                "id": "n3",
                "data": {
                    "device": {"id": "uuid-3", "template_name": "Leaf"},
                    "label": "leaf-1",
                },
            },
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n3"}],
    }
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(
        f"/templates/from-topology/{topology_id}",
        json={"name": "fab-tmpl"},
    )
    assert resp.status_code == 201
    data = resp.json()
    devices = [n["data"]["device"] for n in data["canvas_data"]["nodes"]]
    assert devices == [
        {"role": "pa-vm-1"},
        {"role": "pa-vm-2"},
        {"role": "leaf-1"},
    ]
    # Edges preserved verbatim.
    assert data["canvas_data"]["edges"] == canvas["edges"]


@pytest.mark.asyncio
async def test_from_topology_not_found(user_client):
    resp = await user_client.post(
        f"/templates/from-topology/{uuid.uuid4()}",
        json={"name": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_instantiate_substitutes_devices_and_creates_v1_snapshot(user_client):
    # Build a template with two roles.
    canvas = {
        "nodes": [
            {"id": "a", "data": {"device": {"role": "pa-vm-1"}}},
            {"id": "b", "data": {"device": {"role": "pa-vm-2"}}},
        ],
        "edges": [{"id": "e", "source": "a", "target": "b"}],
    }
    create = await user_client.post(
        "/templates",
        json={"name": "tmpl", "canvas_data": canvas},
    )
    template_id = create.json()["id"]

    dev_a = str(uuid.uuid4())
    dev_b = str(uuid.uuid4())
    resp = await user_client.post(
        f"/templates/{template_id}/instantiate",
        json={
            "name": "Live Lab",
            "role_assignments": {"pa-vm-1": dev_a, "pa-vm-2": dev_b},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Live Lab"
    devices = [n["data"]["device"] for n in data["canvas_data"]["nodes"]]
    assert devices == [
        {"role": "pa-vm-1", "id": dev_a},
        {"role": "pa-vm-2", "id": dev_b},
    ]
    # Edges preserved.
    assert len(data["canvas_data"]["edges"]) == 1

    # v1 snapshot exists.
    versions = (await user_client.get(f"/topologies/{data['id']}/versions")).json()
    assert versions["total"] == 1


@pytest.mark.asyncio
async def test_instantiate_missing_role_assignment(user_client):
    canvas = {
        "nodes": [{"id": "a", "data": {"device": {"role": "pa-vm-1"}}}],
        "edges": [],
    }
    create = await user_client.post("/templates", json={"name": "t", "canvas_data": canvas})
    template_id = create.json()["id"]
    resp = await user_client.post(
        f"/templates/{template_id}/instantiate",
        json={"name": "Lab", "role_assignments": {}},
    )
    assert resp.status_code == 422
    assert "pa-vm-1" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_template_owner(user_client):
    create = await user_client.post("/templates", json={"name": "orig"})
    tid = create.json()["id"]
    resp = await user_client.put(f"/templates/{tid}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"


@pytest.mark.asyncio
async def test_update_template_other_user_forbidden(user_client):
    create = await user_client.post("/templates", json={"name": "orig"})
    tid = create.json()["id"]
    app.dependency_overrides[get_current_user_payload] = _override_other
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(f"/templates/{tid}", json={"name": "hijack"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_template_admin_can_edit(user_client):
    create = await user_client.post("/templates", json={"name": "orig"})
    tid = create.json()["id"]
    app.dependency_overrides[get_current_user_payload] = _override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(f"/templates/{tid}", json={"name": "admin-edit"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "admin-edit"


@pytest.mark.asyncio
async def test_delete_template_owner(user_client):
    create = await user_client.post("/templates", json={"name": "doomed"})
    tid = create.json()["id"]
    resp = await user_client.delete(f"/templates/{tid}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_template_other_user_forbidden(user_client):
    create = await user_client.post("/templates", json={"name": "owned"})
    tid = create.json()["id"]
    app.dependency_overrides[get_current_user_payload] = _override_other
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/templates/{tid}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unique_template_name(user_client):
    a = await user_client.post("/templates", json={"name": "dup"})
    assert a.status_code == 201
    b = await user_client.post("/templates", json={"name": "dup"})
    assert b.status_code == 409
    assert b.json()["detail"] == "Template name 'dup' already exists"


@pytest.mark.asyncio
async def test_instantiate_not_found(user_client):
    resp = await user_client.post(
        f"/templates/{uuid.uuid4()}/instantiate",
        json={"name": "x", "role_assignments": {}},
    )
    assert resp.status_code == 404


_DUP_DETAIL = "Template name 'taken' already exists"


@pytest.mark.asyncio
async def test_duplicate_name_is_409_from_every_write_route(user_client):
    """Issue #1005: create, update, and from-topology answer the same 409."""
    taken = await user_client.post("/templates", json={"name": "taken"})
    assert taken.status_code == 201

    create = await user_client.post("/templates", json={"name": "taken"})
    assert create.status_code == 409
    assert create.json()["detail"] == _DUP_DETAIL

    other = await user_client.post("/templates", json={"name": "other"})
    update = await user_client.put(f"/templates/{other.json()['id']}", json={"name": "taken"})
    assert update.status_code == 409
    assert update.json()["detail"] == _DUP_DETAIL

    topo = await user_client.post("/topologies", json={"name": "Src"})
    from_topo = await user_client.post(
        f"/templates/from-topology/{topo.json()['id']}", json={"name": "taken"}
    )
    assert from_topo.status_code == 409
    assert from_topo.json()["detail"] == _DUP_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "x" * 101])
async def test_template_name_bounds_are_422_on_every_write_route(user_client, name):
    """Issue #1005: names take the topology bound (1 to 100)."""
    assert (await user_client.post("/templates", json={"name": name})).status_code == 422
    ok = await user_client.post("/templates", json={"name": "ok"})
    assert (
        await user_client.put(f"/templates/{ok.json()['id']}", json={"name": name})
    ).status_code == 422
    topo = await user_client.post("/topologies", json={"name": "Src"})
    assert (
        await user_client.post(f"/templates/from-topology/{topo.json()['id']}", json={"name": name})
    ).status_code == 422
    assert (
        await user_client.post(
            f"/templates/{ok.json()['id']}/instantiate",
            json={"name": name, "role_assignments": {}},
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_template_name_at_cap_accepted(user_client):
    resp = await user_client.post("/templates", json={"name": "x" * 100})
    assert resp.status_code == 201


_ELEMENT_ID = "11111111-1111-1111-1111-111111111111"


def _device_element_canvas(device_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": "d1",
                "type": "deviceNode",
                "data": {"device": {"id": device_id, "template_name": "Leaf"}},
            },
            {
                "id": "el1",
                "type": "networkElementNode",
                "data": {
                    "element": {
                        "id": _ELEMENT_ID,
                        "element_type": "vlan",
                        "label": "VLAN 10",
                        "attrs": {"vlan_id": 10},
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "att1",
                "source": "d1",
                "target": "el1",
                "data": {"source_port_name": "ge-0/0/1"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_element_survives_save_as_template_and_instantiate(user_client):
    """Issue #1005: an element node is not a role; it round-trips intact."""
    topo = await user_client.post("/topologies", json={"name": "Src"})
    topology_id = topo.json()["id"]
    canvas = _device_element_canvas(str(uuid.uuid4()))
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    saved = await user_client.post(
        f"/templates/from-topology/{topology_id}", json={"name": "with-element"}
    )
    assert saved.status_code == 201
    nodes = {n["id"]: n for n in saved.json()["canvas_data"]["nodes"]}
    assert nodes["d1"]["data"]["device"] == {"role": "leaf-1"}
    assert nodes["el1"] == canvas["nodes"][1]
    assert saved.json()["canvas_data"]["edges"] == canvas["edges"]

    dev = str(uuid.uuid4())
    inst = await user_client.post(
        f"/templates/{saved.json()['id']}/instantiate",
        json={"name": "Lab", "role_assignments": {"leaf-1": dev}},
    )
    assert inst.status_code == 201
    out = {n["id"]: n for n in inst.json()["canvas_data"]["nodes"]}
    assert out["d1"]["data"]["device"] == {"role": "leaf-1", "id": dev}
    assert out["el1"] == canvas["nodes"][1]
    assert "device" not in out["el1"]["data"]
    assert inst.json()["canvas_data"]["edges"] == canvas["edges"]


@pytest.mark.asyncio
async def test_legacy_template_role_on_element_is_ignored(user_client):
    """A pre-#1005 stored template with a role on an element node: reads drop it and
    instantiate neither demands nor assigns a device for it (no data migration)."""
    from app.models.template import TopologyTemplate

    legacy = _device_element_canvas("unused")
    legacy["nodes"][0]["data"]["device"] = {"role": "leaf-1"}
    legacy["nodes"][1]["data"]["device"] = {"role": "vlan-10-1"}
    async with TestSessionLocal() as session:
        row = TopologyTemplate(
            name="legacy",
            canvas_data=legacy,
            created_by=uuid.UUID(USER_ID),
            owner_name="viewer",
        )
        session.add(row)
        await session.commit()
        template_id = str(row.id)

    read = await user_client.get(f"/templates/{template_id}")
    assert read.status_code == 200
    el = [n for n in read.json()["canvas_data"]["nodes"] if n["id"] == "el1"][0]
    assert "device" not in el["data"]

    dev = str(uuid.uuid4())
    inst = await user_client.post(
        f"/templates/{template_id}/instantiate",
        json={"name": "Lab", "role_assignments": {"leaf-1": dev}},
    )
    assert inst.status_code == 201
    out = {n["id"]: n for n in inst.json()["canvas_data"]["nodes"]}
    assert "device" not in out["el1"]["data"]
    assert out["el1"]["data"]["element"]["id"] == _ELEMENT_ID
