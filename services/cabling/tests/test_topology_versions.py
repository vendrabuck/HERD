import uuid
from unittest.mock import patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}
OTHER_USER_PAYLOAD = {"sub": OTHER_USER_ID, "username": "other", "role": "user"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


def _override_other_user():
    return OTHER_USER_PAYLOAD


test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncSession:
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def _noop_reservation_guard():
    """Default: no active reservations, so restore proceeds."""
    with patch("app.routes.versions.find_blocking_reservations", return_value=[]) as m:
        yield m


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _make_topology(client: AsyncClient, name: str = "My Lab") -> str:
    resp = await client.post("/topologies", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _save_canvas(client: AsyncClient, topology_id: str, canvas: dict, **extra) -> dict:
    body = {"canvas_data": canvas, **extra}
    resp = await client.put(f"/topologies/{topology_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_first_save_creates_version_one(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas, description="first")

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["version_number"] == 1
    assert data["items"][0]["description"] == "first"
    assert data["items"][0]["author_name"] == "viewer"
    assert data["items"][0]["created_by"] == USER_ID


@pytest.mark.asyncio
async def test_second_distinct_save_creates_version_two(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    await _save_canvas(
        user_client, topology_id, {"nodes": [{"id": "n1"}, {"id": "n2"}], "edges": []}
    )

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    data = resp.json()
    assert data["total"] == 2
    version_numbers = [v["version_number"] for v in data["items"]]
    assert version_numbers == [2, 1]


@pytest.mark.asyncio
async def test_identical_save_is_deduped(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)
    await _save_canvas(user_client, topology_id, canvas)

    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_name_only_save_does_not_create_version(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    resp = await user_client.put(f"/topologies/{topology_id}", json={"name": "Renamed"})
    assert resp.status_code == 200

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()
    assert versions["total"] == 1


@pytest.mark.asyncio
async def test_list_omits_canvas_data_detail_includes_it(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)

    listing = (await user_client.get(f"/topologies/{topology_id}/versions")).json()
    assert "canvas_data" not in listing["items"][0]

    version_id = listing["items"][0]["id"]
    detail = (await user_client.get(f"/topologies/{topology_id}/versions/{version_id}")).json()
    assert detail["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_diff_detects_add_remove_modify(user_client):
    topology_id = await _make_topology(user_client)
    canvas_a = {
        "nodes": [{"id": "n1", "position": {"x": 0, "y": 0}}, {"id": "n2"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    canvas_b = {
        "nodes": [{"id": "n1", "position": {"x": 50, "y": 0}}, {"id": "n3"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    await _save_canvas(user_client, topology_id, canvas_a)
    await _save_canvas(user_client, topology_id, canvas_b)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    v_b = versions[0]["id"]
    v_a = versions[1]["id"]

    resp = await user_client.get(
        f"/topologies/{topology_id}/versions/diff", params={"a": v_a, "b": v_b}
    )
    assert resp.status_code == 200
    diff = resp.json()
    assert [n["id"] for n in diff["nodes_added"]] == ["n3"]
    assert [n["id"] for n in diff["nodes_removed"]] == ["n2"]
    assert [m["id"] for m in diff["nodes_modified"]] == ["n1"]
    assert diff["edges_added"] == []
    assert diff["edges_removed"] == []
    assert diff["edges_modified"] == []


@pytest.mark.asyncio
async def test_diff_identical_versions_empty(user_client):
    topology_id = await _make_topology(user_client)
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas)
    # Save something different then revert via save of same canvas - won't dedupe
    # because we check against current state. Force a second version by changing.
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n2"}], "edges": []})
    await _save_canvas(user_client, topology_id, canvas)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    # v1 and v3 both have the original canvas - diffing them should be empty.
    resp = await user_client.get(
        f"/topologies/{topology_id}/versions/diff",
        params={"a": versions[2]["id"], "b": versions[0]["id"]},
    )
    diff = resp.json()
    assert diff["nodes_added"] == []
    assert diff["nodes_removed"] == []
    assert diff["nodes_modified"] == []


@pytest.mark.asyncio
async def test_restore_applies_snapshot_and_creates_new_version(user_client):
    topology_id = await _make_topology(user_client)
    canvas_a = {"nodes": [{"id": "n1"}], "edges": []}
    canvas_b = {"nodes": [{"id": "n2"}], "edges": []}
    await _save_canvas(user_client, topology_id, canvas_a)
    await _save_canvas(user_client, topology_id, canvas_b)

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    v_one = versions[1]  # version_number 1, canvas_a

    resp = await user_client.post(
        f"/topologies/{topology_id}/versions/{v_one['id']}/restore", json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canvas_data"] == canvas_a

    versions = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"]
    newest = versions[0]
    assert newest["version_number"] == 3
    assert newest["restored_from_id"] == v_one["id"]
    assert newest["description"] == "Restored from v1"


@pytest.mark.asyncio
async def test_restore_blocked_by_active_reservation(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    with patch(
        "app.routes.versions.find_blocking_reservations",
        return_value=[{"id": "r1", "status": "ACTIVE", "end_time": "2026-05-01T00:00:00Z"}],
    ):
        # Pass a bearer token to trigger the guard call path
        resp = await user_client.post(
            f"/topologies/{topology_id}/versions/{version_id}/restore",
            json={},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reservations"][0]["id"] == "r1"


@pytest.mark.asyncio
async def test_non_creator_can_read_but_not_restore(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Read OK
        list_resp = await ac.get(f"/topologies/{topology_id}/versions")
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1
        # Restore forbidden
        restore_resp = await ac.post(
            f"/topologies/{topology_id}/versions/{version_id}/restore", json={}
        )
        assert restore_resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_restore(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    version_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{topology_id}/versions/{version_id}/restore", json={})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_topology_cascades_versions(user_client):
    topology_id = await _make_topology(user_client)
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n2"}], "edges": []})

    resp = await user_client.delete(f"/topologies/{topology_id}")
    assert resp.status_code == 204

    # Versions endpoint 404s because topology is gone
    resp = await user_client.get(f"/topologies/{topology_id}/versions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_versions_list_pagination(user_client):
    topology_id = await _make_topology(user_client)
    for i in range(3):
        await _save_canvas(user_client, topology_id, {"nodes": [{"id": f"n{i}"}], "edges": []})

    resp = await user_client.get(
        f"/topologies/{topology_id}/versions", params={"skip": 0, "limit": 2}
    )
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert [v["version_number"] for v in data["items"]] == [3, 2]


@pytest.mark.asyncio
async def test_restore_with_description_and_restore_name(user_client):
    topology_id = await _make_topology(user_client, name="Original")
    await _save_canvas(user_client, topology_id, {"nodes": [{"id": "n1"}], "edges": []})
    v_one_id = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0][
        "id"
    ]
    # Rename topology, save again so names diverge
    await _save_canvas(
        user_client,
        topology_id,
        {"nodes": [{"id": "n2"}], "edges": []},
        name="Renamed",
    )

    resp = await user_client.post(
        f"/topologies/{topology_id}/versions/{v_one_id}/restore",
        json={"description": "Manual rollback", "restore_name": True},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Original"

    latest = (await user_client.get(f"/topologies/{topology_id}/versions")).json()["items"][0]
    assert latest["description"] == "Manual rollback"
    assert latest["name"] == "Original"


@pytest.mark.asyncio
async def test_version_on_missing_topology(user_client):
    fake = str(uuid.uuid4())
    resp = await user_client.get(f"/topologies/{fake}/versions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_version_detail_wrong_topology(user_client):
    t1 = await _make_topology(user_client, "T1")
    t2 = await _make_topology(user_client, "T2")
    await _save_canvas(user_client, t1, {"nodes": [{"id": "n1"}], "edges": []})
    v_id = (await user_client.get(f"/topologies/{t1}/versions")).json()["items"][0]["id"]

    # Looking up t1's version under t2 returns 404
    resp = await user_client.get(f"/topologies/{t2}/versions/{v_id}")
    assert resp.status_code == 404
