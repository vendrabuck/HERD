import uuid
from unittest.mock import patch

import pytest
from app.database import Base, get_db
from app.dependencies import get_current_user_payload, require_admin
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Test users
ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())

SUPERADMIN_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}
OTHER_USER_PAYLOAD = {"sub": OTHER_USER_ID, "username": "other", "role": "user"}
SUPERADMIN_PAYLOAD = {"sub": SUPERADMIN_ID, "username": "superadmin", "role": "superadmin"}


def _override_admin():
    return ADMIN_PAYLOAD


def _override_user():
    return USER_PAYLOAD


def _override_other_user():
    return OTHER_USER_PAYLOAD


def _override_superadmin():
    return SUPERADMIN_PAYLOAD


# DB fixtures
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


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def other_user_client():
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[require_admin] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_topology(user_client):
    resp = await user_client.post("/topologies", json={"name": "My Lab"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Lab"
    assert data["created_by"] == USER_ID
    assert data["owner_name"] == "viewer"
    assert data["canvas_data"] is None


@pytest.mark.asyncio
async def test_list_topologies(user_client):
    await user_client.post("/topologies", json={"name": "Lab 1"})
    await user_client.post("/topologies", json={"name": "Lab 2"})
    resp = await user_client.get("/topologies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_get_topology(user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    resp = await user_client.get(f"/topologies/{topology_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "My Lab"


@pytest.mark.asyncio
async def test_get_topology_not_found(user_client):
    fake_id = str(uuid.uuid4())
    resp = await user_client.get(f"/topologies/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_topology_by_creator(user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "1"}], "edges": []}
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Updated Lab", "canvas_data": canvas},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Lab"
    assert data["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_update_topology_by_admin(admin_client, user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    # Clear and set admin overrides
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(
            f"/topologies/{topology_id}",
            json={"name": "Admin Updated"},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Admin Updated"


@pytest.mark.asyncio
async def test_update_topology_forbidden_for_other_user(user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    # Switch to other user
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(
            f"/topologies/{topology_id}",
            json={"name": "Hijacked"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_topology(user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    resp = await user_client.delete(f"/topologies/{topology_id}")
    assert resp.status_code == 204
    # Verify gone
    resp = await user_client.get(f"/topologies/{topology_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_topology_not_found(user_client):
    fake_id = str(uuid.uuid4())
    resp = await user_client.delete(f"/topologies/{fake_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_topology_forbidden_for_other_user(user_client):
    create_resp = await user_client.post("/topologies", json={"name": "My Lab"})
    topology_id = create_resp.json()["id"]
    # Switch to other user
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/topologies/{topology_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_topologies_empty(user_client):
    resp = await user_client.get("/topologies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


# --- Superadmin client fixture ---


@pytest.fixture
async def superadmin_client():
    app.dependency_overrides[get_current_user_payload] = _override_superadmin
    app.dependency_overrides[require_admin] = _override_superadmin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Additional cabling tests ---


@pytest.mark.asyncio
async def test_health_endpoint_via_topology_client(user_client):
    """GET /health returns 200."""
    resp = await user_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_topology_empty_name(user_client):
    """name="" edge case."""
    resp = await user_client.post("/topologies", json={"name": ""})
    # Empty name may be accepted or rejected depending on validation
    # Document current behavior
    assert resp.status_code in (201, 422)


@pytest.mark.asyncio
async def test_other_user_can_read_topology(user_client):
    """Non-creator can GET topology (reads are public)."""
    create_resp = await user_client.post("/topologies", json={"name": "Public Lab"})
    assert create_resp.status_code == 201
    topology_id = create_resp.json()["id"]
    # Switch to other user
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/topologies/{topology_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Public Lab"


@pytest.mark.asyncio
async def test_update_topology_name_only(user_client):
    """Update name only, canvas_data unchanged."""
    create_resp = await user_client.post("/topologies", json={"name": "Original"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Original", "canvas_data": canvas},
    )
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_update_topology_canvas_only(user_client):
    """Update canvas_data only, name unchanged."""
    create_resp = await user_client.post("/topologies", json={"name": "Canvas Test"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"canvas_data": new_canvas},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Canvas Test"
    assert resp.json()["canvas_data"] == new_canvas


@pytest.mark.asyncio
async def test_update_topology_canvas_blocked_by_other_users_reservation(user_client):
    """A canvas edit is blocked (409) when another user holds a live reservation
    on the topology."""
    create_resp = await user_client.post("/topologies", json={"name": "Reserved Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    blocking = [
        {
            "id": "r1",
            "user_id": OTHER_USER_ID,
            "status": "ACTIVE",
            "end_time": "2026-12-01T00:00:00Z",
        }
    ]
    with patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=blocking,
    ):
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": new_canvas},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reservations"][0]["id"] == "r1"


@pytest.mark.asyncio
async def test_update_topology_canvas_allowed_for_reservation_owner(user_client):
    """The reservation owner may edit their own live topology's wiring."""
    create_resp = await user_client.post("/topologies", json={"name": "My Live Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    # The blocking reservation is owned by the editing user (USER_ID), so allowed.
    blocking = [
        {
            "id": "r1",
            "user_id": USER_ID,
            "status": "ACTIVE",
            "end_time": "2026-12-01T00:00:00Z",
        }
    ]
    with patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=blocking,
    ):
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": new_canvas},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 200
    assert resp.json()["canvas_data"] == new_canvas


@pytest.mark.asyncio
async def test_update_topology_canvas_admin_bypasses_reservation_lock(user_client):
    """An admin may edit a reserved topology's wiring regardless of owner; the
    guard is not even consulted for admins."""
    create_resp = await user_client.post("/topologies", json={"name": "Admin Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    guard = patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=[{"id": "r1", "user_id": OTHER_USER_ID, "status": "ACTIVE"}],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        with guard as guard_mock:
            resp = await ac.put(
                f"/topologies/{topology_id}",
                json={"canvas_data": new_canvas},
                headers={"Authorization": "Bearer faketoken"},
            )
    assert resp.status_code == 200
    guard_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_topology_name_only_skips_reservation_lock(user_client):
    """A name-only edit does not touch live wiring, so the reservation lock is
    not consulted even when a reservation exists."""
    create_resp = await user_client.post("/topologies", json={"name": "Original"})
    topology_id = create_resp.json()["id"]
    with patch(
        "app.routes.topologies.find_blocking_reservations",
    ) as guard_mock:
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"name": "Renamed"},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 200
    guard_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_topology_not_found(user_client):
    """PUT random UUID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await user_client.put(
        f"/topologies/{fake_id}",
        json={"name": "Ghost"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_topology_by_admin(user_client, admin_client):
    """Admin deletes another user's topology."""
    create_resp = await user_client.post("/topologies", json={"name": "To Delete"})
    topology_id = create_resp.json()["id"]
    # Switch to admin
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/topologies/{topology_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_update_topology_null_canvas_preserves_existing(user_client):
    """PUT with canvas_data: null should preserve the existing canvas."""
    create_resp = await user_client.post("/topologies", json={"name": "Canvas Lab"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await user_client.put(
        f"/topologies/{topology_id}",
        json={"canvas_data": canvas},
    )
    # Update with canvas_data omitted (null in JSON) should not wipe it
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Canvas Lab Renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_list_topologies_ordered_by_updated_at_desc(user_client):
    """Create A then B, update A after a delay; list should return A first."""
    import asyncio

    resp_a = await user_client.post("/topologies", json={"name": "Lab A"})
    await user_client.post("/topologies", json={"name": "Lab B"})
    a_id = resp_a.json()["id"]
    # SQLite CURRENT_TIMESTAMP has 1-second resolution; wait to ensure distinct timestamps
    await asyncio.sleep(1.1)
    # Update A so its updated_at is newest
    await user_client.put(f"/topologies/{a_id}", json={"name": "Lab A Updated"})
    # A should be first (most recently updated)
    list_resp = await user_client.get("/topologies")
    assert list_resp.status_code == 200
    assert list_resp.json()["items"][0]["name"] == "Lab A Updated"


@pytest.mark.asyncio
async def test_update_topology_by_superadmin(user_client, superadmin_client):
    """Superadmin can update another user's topology."""
    create_resp = await user_client.post("/topologies", json={"name": "User Lab"})
    topology_id = create_resp.json()["id"]
    # Switch to superadmin
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_superadmin
    app.dependency_overrides[require_admin] = _override_superadmin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(
            f"/topologies/{topology_id}",
            json={"name": "Superadmin Updated"},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Superadmin Updated"


@pytest.mark.asyncio
async def test_owner_name_in_list(user_client):
    """List topologies includes owner_name."""
    await user_client.post("/topologies", json={"name": "Owner Test"})
    resp = await user_client.get("/topologies")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["owner_name"] == "viewer"


@pytest.mark.asyncio
async def test_owner_name_preserved_on_update(user_client):
    """Updating topology name does not change owner_name."""
    create_resp = await user_client.post("/topologies", json={"name": "Original"})
    topology_id = create_resp.json()["id"]
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Renamed"},
    )
    assert resp.status_code == 200
    assert resp.json()["owner_name"] == "viewer"


@pytest.mark.asyncio
async def test_delete_topology_by_superadmin(user_client, superadmin_client):
    """Superadmin can delete another user's topology."""
    create_resp = await user_client.post("/topologies", json={"name": "To Delete"})
    topology_id = create_resp.json()["id"]
    # Switch to superadmin
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_superadmin
    app.dependency_overrides[require_admin] = _override_superadmin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/topologies/{topology_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_list_topologies_pagination(user_client):
    """GET /topologies supports skip/limit pagination."""
    for i in range(5):
        await user_client.post("/topologies", json={"name": f"Lab {i}"})
    resp = await user_client.get("/topologies?skip=0&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["skip"] == 0
    assert data["limit"] == 2

    resp2 = await user_client.get("/topologies?skip=4&limit=10")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["items"]) == 1


@pytest.mark.asyncio
async def test_unauthenticated_list_topologies():
    """No auth returns 401 for topology listing."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/topologies")
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthenticated_create_topology():
    """No auth returns 401 for topology creation."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/topologies", json={"name": "Unauthorized"})
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthenticated_get_topology():
    """No auth returns 401 for get topology."""
    app.dependency_overrides[get_db] = _override_get_db
    fake_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/topologies/{fake_id}")
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthenticated_update_topology():
    """No auth returns 401 for topology update."""
    app.dependency_overrides[get_db] = _override_get_db
    fake_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.put(f"/topologies/{fake_id}", json={"name": "Hack"})
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unauthenticated_delete_topology():
    """No auth returns 401 for topology deletion."""
    app.dependency_overrides[get_db] = _override_get_db
    fake_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete(f"/topologies/{fake_id}")
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_topology_modified_by_set_on_update(user_client):
    """modified_by is set when topology is updated."""
    create_resp = await user_client.post("/topologies", json={"name": "Track Mod"})
    topology_id = create_resp.json()["id"]
    resp = await user_client.put(
        f"/topologies/{topology_id}",
        json={"name": "Updated Track Mod"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["modified_by"] == USER_ID


@pytest.mark.asyncio
async def test_create_topology_preserves_all_fields(user_client):
    """Verify all response fields are present on create."""
    resp = await user_client.post("/topologies", json={"name": "Full Check"})
    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["name"] == "Full Check"
    assert data["created_by"] == USER_ID
    assert data["owner_name"] == "viewer"
    assert "created_at" in data
    assert "updated_at" in data


# --- /topologies/{id}/clone ---


@pytest.mark.asyncio
async def test_clone_topology_happy_path(user_client):
    """Clone duplicates canvas_data, transfers ownership to the caller, leaves source alone."""
    create_resp = await user_client.post("/topologies", json={"name": "Source"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "a"}], "edges": [{"id": "e1", "source": "a", "target": "a"}]}
    await user_client.put(
        f"/topologies/{topology_id}",
        json={"canvas_data": canvas},
    )

    # Switch to other user and clone.
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        clone_resp = await ac.post(
            f"/topologies/{topology_id}/clone",
            json={"name": "Source (copy)"},
        )
        assert clone_resp.status_code == 201
        clone = clone_resp.json()
        assert clone["name"] == "Source (copy)"
        assert clone["created_by"] == OTHER_USER_ID
        assert clone["owner_name"] == "other"
        assert clone["canvas_data"] == canvas
        assert clone["id"] != topology_id

        # Source unchanged.
        src = (await ac.get(f"/topologies/{topology_id}")).json()
        assert src["name"] == "Source"
        assert src["canvas_data"] == canvas
        assert src["owner_name"] == "viewer"


@pytest.mark.asyncio
async def test_clone_topology_not_found(user_client):
    fake_id = str(uuid.uuid4())
    resp = await user_client.post(f"/topologies/{fake_id}/clone", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clone_topology_writes_v1_snapshot(user_client):
    """A v1 TopologyVersion snapshot is written for the clone."""
    create_resp = await user_client.post("/topologies", json={"name": "Snap"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [], "edges": []}
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    clone_resp = await user_client.post(
        f"/topologies/{topology_id}/clone", json={"name": "Snap (copy)"}
    )
    clone_id = clone_resp.json()["id"]

    versions = (await user_client.get(f"/topologies/{clone_id}/versions")).json()
    assert versions["total"] == 1
    assert versions["items"][0]["version_number"] == 1
    assert versions["items"][0]["author_name"] == "viewer"
    assert "Cloned from Snap" in (versions["items"][0]["description"] or "")


@pytest.mark.asyncio
async def test_clone_topology_independent_canvas(user_client):
    """Editing the clone does not mutate the source canvas."""
    create_resp = await user_client.post("/topologies", json={"name": "Indep"})
    src_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "n1"}], "edges": []}
    await user_client.put(f"/topologies/{src_id}", json={"canvas_data": canvas})

    clone_resp = await user_client.post(
        f"/topologies/{src_id}/clone", json={"name": "Indep (copy)"}
    )
    clone_id = clone_resp.json()["id"]

    new_canvas = {"nodes": [{"id": "n1"}, {"id": "n2"}], "edges": []}
    await user_client.put(f"/topologies/{clone_id}", json={"canvas_data": new_canvas})

    src = (await user_client.get(f"/topologies/{src_id}")).json()
    assert src["canvas_data"] == canvas


@pytest.mark.asyncio
async def test_clone_topology_null_canvas(user_client):
    """Cloning a topology with null canvas_data succeeds and produces a null clone canvas."""
    create_resp = await user_client.post("/topologies", json={"name": "Empty"})
    src_id = create_resp.json()["id"]
    clone_resp = await user_client.post(
        f"/topologies/{src_id}/clone", json={"name": "Empty (copy)"}
    )
    assert clone_resp.status_code == 201
    assert clone_resp.json()["canvas_data"] is None


@pytest.mark.asyncio
async def test_clone_topology_unauthenticated():
    """No Authorization header on clone is rejected before reaching handler logic."""
    fake_id = str(uuid.uuid4())
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{fake_id}/clone", json={"name": "x"})
    assert resp.status_code in (401, 403)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_clone_topology_missing_name(user_client):
    """Clone body without `name` is rejected as a 422 validation error."""
    create_resp = await user_client.post("/topologies", json={"name": "Source"})
    src_id = create_resp.json()["id"]
    resp = await user_client.post(f"/topologies/{src_id}/clone", json={})
    assert resp.status_code == 422


# --- /topologies/{id}/validate ---


def _canvas_with_edge(
    source_node_id: str,
    target_node_id: str,
    source_device_id: uuid.UUID,
    target_device_id: uuid.UUID,
    *,
    layer: str = "L2",
    edge_id: str = "e1",
    is_proposal: bool = False,
) -> dict:
    """Build a minimal canvas_data shape mirroring the React Flow editor output."""
    return {
        "nodes": [
            {
                "id": source_node_id,
                "data": {"device": {"id": str(source_device_id)}},
            },
            {
                "id": target_node_id,
                "data": {"device": {"id": str(target_device_id)}},
            },
        ],
        "edges": [
            {
                "id": edge_id,
                "source": source_node_id,
                "target": target_node_id,
                "data": {"layer": layer, "isProposal": is_proposal},
            },
        ],
    }


async def _seed_connection(
    client, da_id: uuid.UUID, port_a: str, db_id: uuid.UUID, port_b: str
) -> None:
    resp = await client.post(
        "/connections",
        json={
            "device_a_id": str(da_id),
            "port_a": port_a,
            "device_b_id": str(db_id),
            "port_b": port_b,
            "connection_type": "ethernet",
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_validate_topology_empty_canvas(user_client):
    """A topology with no edges is trivially valid."""
    create = await user_client.post("/topologies", json={"name": "Empty"})
    topology_id = create.json()["id"]
    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []


@pytest.mark.asyncio
async def test_validate_topology_reachable_edge(admin_client, user_client):
    """A topology whose edge connects two physically cabled devices is valid."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    create = await user_client.post("/topologies", json={"name": "Cabled"})
    topology_id = create.json()["id"]
    canvas = _canvas_with_edge("nA", "nB", a, b)
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []


@pytest.mark.asyncio
async def test_validate_topology_unreachable_edge(admin_client, user_client):
    """Devices in physically isolated fabrics produce no_path edges."""
    # Two pairs cabled internally but not to each other (mirrors the seed's
    # multi-lab isolation).
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a1, "eth1", a2, "eth1")
    await _seed_connection(admin_client, b1, "eth1", b2, "eth1")

    create = await user_client.post("/topologies", json={"name": "Cross"})
    topology_id = create.json()["id"]
    canvas = _canvas_with_edge("nA", "nB", a1, b1, edge_id="cross-edge")
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["invalid_edges"]) == 1
    invalid = body["invalid_edges"][0]
    assert invalid["edge_id"] == "cross-edge"
    assert invalid["reason"] == "no_path"
    assert invalid["source_device_id"] == str(a1)
    assert invalid["target_device_id"] == str(b1)


@pytest.mark.asyncio
async def test_validate_topology_skips_proposal_edges(admin_client, user_client):
    """Proposal edges are speculative and must not block validation."""
    a, b = uuid.uuid4(), uuid.uuid4()
    # No connection seeded: a and b are unreachable.
    create = await user_client.post("/topologies", json={"name": "Proposal"})
    topology_id = create.json()["id"]
    canvas = _canvas_with_edge("nA", "nB", a, b, is_proposal=True)
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []


@pytest.mark.asyncio
async def test_validate_topology_missing_device_reference(user_client):
    """An edge whose source/target node has no device id is reported as missing_device."""
    create = await user_client.post("/topologies", json={"name": "Missing"})
    topology_id = create.json()["id"]
    canvas = {
        "nodes": [
            {"id": "nA", "data": {}},
            {"id": "nB", "data": {"device": {"id": str(uuid.uuid4())}}},
        ],
        "edges": [
            {
                "id": "broken",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L2"},
            }
        ],
    }
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["invalid_edges"]) == 1
    assert body["invalid_edges"][0]["reason"] == "missing_device"


@pytest.mark.asyncio
async def test_validate_topology_not_found(user_client):
    fake_id = str(uuid.uuid4())
    resp = await user_client.post(f"/topologies/{fake_id}/validate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validate_topology_unauthenticated():
    fake_id = str(uuid.uuid4())
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{fake_id}/validate")
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_validate_topology_forbidden_for_non_owner(user_client):
    """Non-owner non-admin caller is rejected; validate leaks cabling-fabric info."""
    create = await user_client.post("/topologies", json={"name": "Owned"})
    topology_id = create.json()["id"]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_other_user
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_validate_topology_internal_endpoint_with_token(user_client):
    """Service-to-service callers hit /validate/internal with X-Internal-Token (no JWT needed)."""
    create = await user_client.post("/topologies", json={"name": "Owned"})
    topology_id = create.json()["id"]

    # No auth override at all, no Bearer token; only the internal token.
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.routes.topologies.settings") as mock_settings:
        mock_settings.internal_api_token = "internal-test-token"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                f"/topologies/{topology_id}/validate/internal",
                headers={"X-Internal-Token": "internal-test-token"},
            )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_validate_topology_internal_endpoint_rejects_wrong_token(user_client):
    """Wrong X-Internal-Token to /validate/internal is 403."""
    create = await user_client.post("/topologies", json={"name": "Owned"})
    topology_id = create.json()["id"]

    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.routes.topologies.settings") as mock_settings:
        mock_settings.internal_api_token = "right-token"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                f"/topologies/{topology_id}/validate/internal",
                headers={"X-Internal-Token": "wrong-token"},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_validate_topology_admin_can_validate_any(user_client, admin_client):
    """Admins bypass the owner check without needing X-Internal-Token."""
    create = await user_client.post("/topologies", json={"name": "Owned"})
    topology_id = create.json()["id"]

    resp = await admin_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200


# --- Live-edit: reservation lock on canvas edits (deeper coverage) ---


@pytest.mark.asyncio
async def test_update_topology_canvas_superadmin_bypasses_reservation_lock(user_client):
    """A superadmin (not just an admin) may edit a reserved topology's wiring;
    the guard is not consulted, mirroring the admin bypass."""
    create_resp = await user_client.post("/topologies", json={"name": "Superadmin Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user_payload] = _override_superadmin
    app.dependency_overrides[require_admin] = _override_superadmin
    app.dependency_overrides[get_db] = _override_get_db
    guard = patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=[{"id": "r1", "user_id": OTHER_USER_ID, "status": "ACTIVE"}],
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        with guard as guard_mock:
            resp = await ac.put(
                f"/topologies/{topology_id}",
                json={"canvas_data": new_canvas},
                headers={"Authorization": "Bearer faketoken"},
            )
    assert resp.status_code == 200
    guard_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_topology_canvas_fails_open_when_reservations_down(user_client):
    """The guard fails open: when the reservations service is unreachable,
    find_blocking_reservations returns [] and the canvas edit proceeds rather
    than blocking the owner out of their own topology."""
    create_resp = await user_client.post("/topologies", json={"name": "Degraded Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    # An empty list is exactly what the guard returns on any reservations-service
    # failure (see reservation_guard.find_blocking_reservations).
    with patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=[],
    ) as guard_mock:
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": new_canvas},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 200
    assert resp.json()["canvas_data"] == new_canvas
    guard_mock.assert_called_once()


@pytest.mark.asyncio
async def test_update_topology_canvas_no_token_skips_reservation_lock(user_client):
    """With no Authorization header the guard cannot call the reservations
    service, so it is skipped and the edit proceeds. The token is required to
    consult the lock."""
    create_resp = await user_client.post("/topologies", json={"name": "No Token Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    with patch(
        "app.routes.topologies.find_blocking_reservations",
    ) as guard_mock:
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": new_canvas},
        )
    assert resp.status_code == 200
    guard_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_topology_canvas_blocked_when_any_other_owner_in_mix(user_client):
    """A blocking list mixing the editor's own reservation with another user's
    still blocks: the guard filters to others and blocks if any remain."""
    create_resp = await user_client.post("/topologies", json={"name": "Mixed Lab"})
    topology_id = create_resp.json()["id"]
    new_canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    blocking = [
        {
            "id": "mine",
            "user_id": USER_ID,
            "status": "ACTIVE",
            "end_time": "2026-12-01T00:00:00Z",
        },
        {
            "id": "theirs",
            "user_id": OTHER_USER_ID,
            "status": "ACTIVE",
            "end_time": "2026-12-02T00:00:00Z",
        },
    ]
    with patch(
        "app.routes.topologies.find_blocking_reservations",
        return_value=blocking,
    ):
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": new_canvas},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 409
    # Only the other user's reservation is surfaced as a blocker; the editor's
    # own reservation is filtered out.
    blockers = resp.json()["detail"]["reservations"]
    assert [b["id"] for b in blockers] == ["theirs"]


@pytest.mark.asyncio
async def test_update_topology_canvas_unchanged_skips_reservation_lock(user_client):
    """Re-submitting the identical canvas is not a wiring change, so the guard
    is not consulted even with a token and a live reservation present."""
    create_resp = await user_client.post("/topologies", json={"name": "Idempotent Lab"})
    topology_id = create_resp.json()["id"]
    canvas = {"nodes": [{"id": "x"}], "edges": [{"id": "e1"}]}
    # Seed the canvas first (as the owner, no guard interaction needed here).
    await user_client.put(
        f"/topologies/{topology_id}",
        json={"canvas_data": canvas},
    )
    # Now PUT the exact same canvas back; canvas_changed is False.
    with patch(
        "app.routes.topologies.find_blocking_reservations",
    ) as guard_mock:
        resp = await user_client.put(
            f"/topologies/{topology_id}",
            json={"canvas_data": canvas},
            headers={"Authorization": "Bearer faketoken"},
        )
    assert resp.status_code == 200
    guard_mock.assert_not_called()
