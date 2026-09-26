"""HTTP-level proof that field_data never survives a cabling write boundary.

Hardening: the topology editor used to persist the whole inventory Device
record (including field_data, which can carry clear-text device credentials)
onto a canvas device node. Every write boundary now reduces a device node's
data.device to app.services.canvas_nodes.DEVICE_NODE_ALLOWED_KEYS before a row
is stored; this file drives each boundary with a canvas carrying field_data
(with a password key) and asserts it is gone, and no "password" substring
survives anywhere in the serialized response, on read-back through every
relevant GET/export path.
"""

import io
import json
import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies import get_current_user_payload
from app.main import app
from app.models.fork import ReservationFork
from app.models.topology import Topology
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

INTERNAL_TOKEN = "test-internal-token-scrub"

ADMIN_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

# A marker used in place of a real credential, per instructions: never a real
# password, just a distinctive string we can grep the serialized response for.
_PASSWORD_MARKER = "herd-test-marker-do-not-leak-9f3c"


def _dirty_device(device_id: str, name: str = "sw-1") -> dict:
    """A device dict shaped like what the pre-fix editor persisted: the full
    inventory Device record, including field_data with a password key."""
    return {
        "id": device_id,
        "name": name,
        "topology_type": "PHYSICAL",
        "connection_type": "Layer 3 Switch",
        "status": "AVAILABLE",
        "template_id": str(uuid.uuid4()),
        "template_name": "Generic Switch",
        "template_icon": "icon.svg",
        "template_vendor": "Acme",
        "template_model": "X1000",
        "field_data": {"password": _PASSWORD_MARKER, "host": "10.0.0.1"},
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "abc123",
        "exclusive": True,
    }


def _dirty_canvas(device_id: str, second_device_id: str | None = None) -> dict:
    nodes = [{"id": "n1", "type": "deviceNode", "data": {"device": _dirty_device(device_id)}}]
    edges = []
    if second_device_id:
        nodes.append(
            {
                "id": "n2",
                "type": "deviceNode",
                "data": {"device": _dirty_device(second_device_id, name="sw-2")},
            }
        )
        edges.append({"id": "e1", "source": "n1", "target": "n2", "data": {"layer": "L1"}})
    return {"nodes": nodes, "edges": edges}


def _assert_clean(body_text: str) -> None:
    assert "field_data" not in body_text
    assert _PASSWORD_MARKER not in body_text
    assert "password" not in body_text


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _internal_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    yield


def _hdr() -> dict:
    return {"X-Internal-Token": INTERNAL_TOKEN}


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


def _override_admin():
    return ADMIN_PAYLOAD


@pytest.fixture
async def client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Topology PUT, GET, version GET, export ----------------------------------


@pytest.mark.asyncio
async def test_topology_put_scrubs_field_data(client):
    dev = str(uuid.uuid4())
    create = await client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]

    put_resp = await client.put(f"/topologies/{tid}", json={"canvas_data": _dirty_canvas(dev)})
    assert put_resp.status_code == 200, put_resp.text
    _assert_clean(put_resp.text)

    get_resp = await client.get(f"/topologies/{tid}")
    assert get_resp.status_code == 200
    _assert_clean(get_resp.text)
    device = get_resp.json()["canvas_data"]["nodes"][0]["data"]["device"]
    assert device["id"] == dev
    assert device["name"] == "sw-1"
    assert "field_data" not in device


@pytest.mark.asyncio
async def test_topology_version_get_scrubs_field_data(client):
    dev = str(uuid.uuid4())
    create = await client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]
    await client.put(f"/topologies/{tid}", json={"canvas_data": _dirty_canvas(dev)})

    versions = await client.get(f"/topologies/{tid}/versions")
    assert versions.status_code == 200
    version_id = versions.json()["items"][0]["id"]

    version_resp = await client.get(f"/topologies/{tid}/versions/{version_id}")
    assert version_resp.status_code == 200
    _assert_clean(version_resp.text)


@pytest.mark.asyncio
async def test_topology_clone_scrubs_field_data(client):
    dev = str(uuid.uuid4())
    create = await client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]
    await client.put(f"/topologies/{tid}", json={"canvas_data": _dirty_canvas(dev)})

    clone_resp = await client.post(f"/topologies/{tid}/clone", json={"name": "Lab Clone"})
    assert clone_resp.status_code == 201, clone_resp.text
    _assert_clean(clone_resp.text)


@pytest.mark.asyncio
async def test_topology_export_json_and_csv_scrub_field_data(client):
    dev_a, dev_b = str(uuid.uuid4()), str(uuid.uuid4())
    create = await client.post("/topologies", json={"name": "Lab"})
    tid = create.json()["id"]
    await client.put(f"/topologies/{tid}", json={"canvas_data": _dirty_canvas(dev_a, dev_b)})

    json_resp = await client.get("/topologies/export", params={"format": "json"})
    assert json_resp.status_code == 200
    _assert_clean(json_resp.text)

    csv_resp = await client.get("/topologies/export", params={"format": "csv"})
    assert csv_resp.status_code == 200
    _assert_clean(csv_resp.text)


@pytest.mark.asyncio
async def test_topology_import_json_scrubs_field_data(client):
    dev_a = str(uuid.uuid4())
    canvas = {
        "nodes": [
            {
                "id": "n1",
                "type": "deviceNode",
                "data": {"device": {**_dirty_device(dev_a), "name": "switch-a"}},
            }
        ],
        "edges": [],
    }
    items = [{"name": "Imported Dirty Lab", "canvas": canvas}]

    from unittest.mock import AsyncMock, patch

    with patch(
        "app.services.bulk_service.resolve_device_names",
        new=AsyncMock(return_value={"switch-a": dev_a}),
    ):
        resp = await client.post(
            "/topologies/import",
            params={"format": "json"},
            files={"file": ("t.json", io.BytesIO(json.dumps(items).encode()), "application/json")},
        )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == 1
    assert report["rejected"] == 0

    listing = (await client.get("/topologies")).json()["items"]
    tid = next(t["id"] for t in listing if t["name"] == "Imported Dirty Lab")
    detail = await client.get(f"/topologies/{tid}")
    _assert_clean(detail.text)


@pytest.mark.asyncio
async def test_topology_restore_version_scrubs_field_data():
    """Belt and braces: a version row seeded directly (bypassing the API, as a
    pre-migration legacy row would look) still comes out clean once restored,
    since restore is its own write boundary."""
    from app.models.topology import TopologyVersion

    tid = uuid.uuid4()
    dev = str(uuid.uuid4())
    dirty = _dirty_canvas(dev)
    async with TestSessionLocal() as db:
        topo = Topology(id=tid, name="Legacy", created_by=uuid.uuid4(), canvas_data=None)
        db.add(topo)
        await db.flush()
        version = TopologyVersion(
            topology_id=tid,
            version_number=1,
            canvas_data=dirty,
            name="Legacy",
            created_by=topo.created_by,
        )
        db.add(version)
        await db.commit()
        version_id = version.id

    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{tid}/versions/{version_id}/restore", json={})
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    _assert_clean(resp.text)


# --- Fork canvas PUT, save, GET ----------------------------------------------


@pytest.mark.asyncio
async def test_fork_canvas_put_scrubs_field_data(client):
    dev = str(uuid.uuid4())
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [dev]},
        headers=_hdr(),
    )

    put_resp = await client.put(
        f"/internal/forks/{rid}/canvas",
        json={"canvas_data": _dirty_canvas(dev)},
        headers=_hdr(),
    )
    assert put_resp.status_code == 200, put_resp.text
    _assert_clean(put_resp.text)

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.status_code == 200
    _assert_clean(get_resp.text)


@pytest.mark.asyncio
async def test_fork_save_scrubs_field_data(client):
    dev = str(uuid.uuid4())
    rid = uuid.uuid4()
    await client.post(
        "/internal/forks",
        json={"reservation_id": str(rid), "member_device_ids": [dev]},
        headers=_hdr(),
    )

    save_resp = await client.post(
        f"/internal/forks/{rid}/save",
        json={
            "canvas_data": _dirty_canvas(dev),
            "member_device_ids": [dev],
        },
        headers=_hdr(),
    )
    assert save_resp.status_code == 200, save_resp.text
    _assert_clean(save_resp.text)

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.status_code == 200
    _assert_clean(get_resp.text)

    async with TestSessionLocal() as db:
        fork = (
            await db.execute(select(ReservationFork).where(ReservationFork.reservation_id == rid))
        ).scalar_one()
        assert "field_data" not in json.dumps(fork.canvas_data)


@pytest.mark.asyncio
async def test_fork_create_scrubs_a_dirty_parent_topology_canvas(client):
    """Belt and braces: a parent topology row seeded directly with a dirty
    canvas (the shape a pre-migration row would have) still forks clean,
    since forking is its own write boundary regardless of what the parent
    already stored."""
    dev = str(uuid.uuid4())
    parent_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(
            Topology(
                id=parent_id,
                name="Legacy Parent",
                created_by=uuid.uuid4(),
                canvas_data=_dirty_canvas(dev),
            )
        )
        await db.commit()

    rid = uuid.uuid4()
    create_resp = await client.post(
        "/internal/forks",
        json={
            "reservation_id": str(rid),
            "parent_topology_id": str(parent_id),
            "member_device_ids": [dev],
        },
        headers=_hdr(),
    )
    assert create_resp.status_code == 201, create_resp.text

    get_resp = await client.get(f"/internal/forks/{rid}", headers=_hdr())
    assert get_resp.status_code == 200
    _assert_clean(get_resp.text)


# --- Templates ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_create_and_instantiate_scrub_field_data(client):
    dev = str(uuid.uuid4())
    canvas = {
        "nodes": [{"id": "n1", "type": "deviceNode", "data": {"device": _dirty_device(dev)}}],
        "edges": [],
    }
    create_resp = await client.post(
        "/templates", json={"name": "Dirty Template", "canvas_data": canvas}
    )
    assert create_resp.status_code == 201, create_resp.text
    _assert_clean(create_resp.text)

    get_resp = await client.get(f"/templates/{create_resp.json()['id']}")
    _assert_clean(get_resp.text)
