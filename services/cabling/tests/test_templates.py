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
    # Sqlite reports unique violation as a 500 unless caught; ensure the API
    # at least does not silently succeed with two rows.
    assert b.status_code != 201


@pytest.mark.asyncio
async def test_instantiate_not_found(user_client):
    resp = await user_client.post(
        f"/templates/{uuid.uuid4()}/instantiate",
        json={"name": "x", "role_assignments": {}},
    )
    assert resp.status_code == 404
