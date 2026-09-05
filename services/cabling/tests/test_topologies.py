import uuid
from unittest.mock import patch

import app.routes.topologies as topologies_module
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
async def test_validate_topology_device_ids_field_lists_canvas_devices(admin_client, user_client):
    """The additive device_ids field (#701) lists every canvas device node,
    deduplicated and sorted, regardless of validity."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_connection(admin_client, a, "eth1", b, "eth1")

    create = await user_client.post("/topologies", json={"name": "DeviceIds"})
    topology_id = create.json()["id"]
    canvas = _canvas_with_edge("nA", "nB", a, b)
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_ids"] == sorted(str(d) for d in (a, b))


@pytest.mark.asyncio
async def test_validate_topology_device_ids_empty_canvas(user_client):
    """A topology with no edges (and so no device nodes) reports an empty
    device_ids list rather than omitting the field."""
    create = await user_client.post("/topologies", json={"name": "EmptyDeviceIds"})
    topology_id = create.json()["id"]
    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


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
async def test_validate_topology_multi_edge_mix_preserves_order(admin_client, user_client):
    """Multiple edges (reachable, two no_path, missing_device, one proposal) in a
    single topology pin the batched pathfind refactor (issue #313): the
    validation loop now resolves all resolvable pairs in one
    find_all_shortest_paths_batch_async call instead of one call per edge, and
    this asserts the resulting InvalidEdge set (and its order) is unchanged
    from the old per-edge-call behavior.
    """
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()

    # a1-a2 cabled directly (reachable). b-pair and c-pair each cabled
    # internally but not to each other, so cross edges between them are
    # unreachable (no_path).
    await _seed_connection(admin_client, a1, "eth1", a2, "eth1")
    await _seed_connection(admin_client, b1, "eth1", b2, "eth1")
    await _seed_connection(admin_client, c1, "eth1", c2, "eth1")

    canvas = {
        "nodes": [
            {"id": "nA1", "data": {"device": {"id": str(a1)}}},
            {"id": "nA2", "data": {"device": {"id": str(a2)}}},
            {"id": "nB1", "data": {"device": {"id": str(b1)}}},
            {"id": "nB2", "data": {"device": {"id": str(b2)}}},
            {"id": "nC1", "data": {"device": {"id": str(c1)}}},
            {"id": "nC2", "data": {"device": {"id": str(c2)}}},
            {"id": "nMissing", "data": {}},
        ],
        "edges": [
            {
                "id": "missing-1",
                "source": "nA1",
                "target": "nMissing",
                "data": {"layer": "L2"},
            },
            {"id": "reachable-1", "source": "nA1", "target": "nA2", "data": {"layer": "L2"}},
            {"id": "no-path-1", "source": "nB1", "target": "nC1", "data": {"layer": "L2"}},
            {
                "id": "proposal-1",
                "source": "nB2",
                "target": "nC2",
                "data": {"layer": "L2", "isProposal": True},
            },
            {"id": "no-path-2", "source": "nB2", "target": "nC2", "data": {"layer": "L2"}},
        ],
    }

    create = await user_client.post("/topologies", json={"name": "Mixed"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False

    # Order must match edge order in the canvas: missing-1, then no-path-1,
    # then no-path-2 (reachable-1 is valid, proposal-1 is skipped).
    edge_ids = [e["edge_id"] for e in body["invalid_edges"]]
    assert edge_ids == ["missing-1", "no-path-1", "no-path-2"]

    reasons = {e["edge_id"]: e["reason"] for e in body["invalid_edges"]}
    assert reasons["missing-1"] == "missing_device"
    assert reasons["no-path-1"] == "no_path"
    assert reasons["no-path-2"] == "no_path"

    missing = body["invalid_edges"][0]
    assert missing["source_device_id"] == str(a1)
    assert missing["target_device_id"] is None


def _element_canvas(
    *,
    device_id: uuid.UUID,
    device_node_id: str = "nDev",
    element_node_id: str = "nElem",
    element_id: str | None = None,
    edge_id: str = "attach-1",
    device_is_source: bool = True,
    source_port_name: str | None = "eth0",
    target_port_name: str | None = None,
) -> dict:
    """Build a canvas with one device node, one networkElementNode, and one edge
    between them, mirroring ADR 0012's canvas shape.
    """
    element_data: dict = {"element_type": "vlan_segment", "label": "Segment"}
    if element_id is not None:
        element_data["id"] = element_id
    device_node = {"id": device_node_id, "data": {"device": {"id": str(device_id)}}}
    element_node = {
        "id": element_node_id,
        "type": "networkElementNode",
        "data": {"element": element_data},
    }
    source_id = device_node_id if device_is_source else element_node_id
    target_id = element_node_id if device_is_source else device_node_id
    return {
        "nodes": [device_node, element_node],
        "edges": [
            {
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "data": {
                    "layer": "L1",
                    "source_port_name": source_port_name,
                    "target_port_name": target_port_name,
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_validate_topology_element_attachment_valid_no_bfs(user_client):
    """A device-to-element edge with a non-empty device-side port is VALID and the
    batched pathfind call excludes it entirely (no BFS for a declarative attachment).
    """
    device_id = uuid.uuid4()
    canvas = _element_canvas(device_id=device_id, element_id=str(uuid.uuid4()))

    create = await user_client.post("/topologies", json={"name": "Element"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    seen_pairs: list[list] = []
    real = topologies_module.find_all_shortest_paths_batch_async

    async def _spy(graph, pairs):
        seen_pairs.append(list(pairs))
        return await real(graph, pairs)

    with patch("app.routes.topologies.find_all_shortest_paths_batch_async", _spy):
        resp = await user_client.post(f"/topologies/{topology_id}/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []
    # The batch call, if made at all, must not include the element edge's pair.
    for pairs in seen_pairs:
        assert pairs == []


@pytest.mark.asyncio
async def test_validate_topology_device_ids_excludes_element_nodes(user_client):
    """A network element node is never a device (#701): device_ids names only the
    real device endpoint of a device-to-element attachment edge."""
    device_id = uuid.uuid4()
    canvas = _element_canvas(device_id=device_id, element_id=str(uuid.uuid4()))

    create = await user_client.post("/topologies", json={"name": "ElementDeviceIds"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == [str(device_id)]


@pytest.mark.asyncio
async def test_validate_topology_element_attachment_valid_element_first(user_client):
    """Direction is accepted either way: element-as-source classifies identically to
    device-as-source.
    """
    device_id = uuid.uuid4()
    canvas = _element_canvas(
        device_id=device_id,
        element_id=str(uuid.uuid4()),
        device_is_source=False,
        source_port_name=None,
        target_port_name="eth0",
    )

    create = await user_client.post("/topologies", json={"name": "ElementFirst"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []


@pytest.mark.asyncio
async def test_validate_topology_element_to_element(user_client):
    """Two network element nodes joined by an edge report element_to_element."""
    create = await user_client.post("/topologies", json={"name": "ElementToElement"})
    topology_id = create.json()["id"]
    canvas = {
        "nodes": [
            {
                "id": "nE1",
                "type": "networkElementNode",
                "data": {"element": {"id": str(uuid.uuid4()), "element_type": "subnet"}},
            },
            {
                "id": "nE2",
                "type": "networkElementNode",
                "data": {"element": {"id": str(uuid.uuid4()), "element_type": "subnet"}},
            },
        ],
        "edges": [{"id": "e-e", "source": "nE1", "target": "nE2", "data": {"layer": "L1"}}],
    }
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["invalid_edges"]) == 1
    assert body["invalid_edges"][0]["reason"] == "element_to_element"


@pytest.mark.asyncio
async def test_validate_topology_element_edge_no_port(user_client):
    """An element edge whose device-side port name is missing reports
    element_edge_no_port."""
    device_id = uuid.uuid4()
    canvas = _element_canvas(
        device_id=device_id, element_id=str(uuid.uuid4()), source_port_name=None
    )

    create = await user_client.post("/topologies", json={"name": "NoPort"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["invalid_edges"]) == 1
    assert body["invalid_edges"][0]["reason"] == "element_edge_no_port"


@pytest.mark.asyncio
async def test_validate_topology_element_edge_empty_port(user_client):
    """An empty-string device-side port name is treated the same as missing."""
    device_id = uuid.uuid4()
    canvas = _element_canvas(device_id=device_id, element_id=str(uuid.uuid4()), source_port_name="")

    create = await user_client.post("/topologies", json={"name": "EmptyPort"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["invalid_edges"][0]["reason"] == "element_edge_no_port"


@pytest.mark.asyncio
async def test_validate_topology_element_edge_unknown_device_side(user_client):
    """An element edge whose OTHER endpoint's node id is in neither map still reports
    missing_device, unchanged (issue #22's fourth classification rule).
    """
    create = await user_client.post("/topologies", json={"name": "DanglingOtherSide"})
    topology_id = create.json()["id"]
    canvas = {
        "nodes": [
            {
                "id": "nElem",
                "type": "networkElementNode",
                "data": {"element": {"id": str(uuid.uuid4()), "element_type": "subnet"}},
            },
            # nDangling is intentionally absent from nodes: the edge references a node
            # id that resolves to neither a device nor an element.
        ],
        "edges": [
            {
                "id": "dangling",
                "source": "nDangling",
                "target": "nElem",
                "data": {"layer": "L1", "target_port_name": "eth0"},
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
async def test_validate_topology_element_node_missing_element_id_falls_back_to_node_id(
    user_client,
):
    """A malformed element node with no data.element.id still classifies as an
    element (falls back to the node id), per node_to_element_map's contract.
    """
    device_id = uuid.uuid4()
    canvas = _element_canvas(device_id=device_id, element_id=None)

    create = await user_client.post("/topologies", json={"name": "NoElementId"})
    topology_id = create.json()["id"]
    await user_client.put(f"/topologies/{topology_id}", json={"canvas_data": canvas})

    resp = await user_client.post(f"/topologies/{topology_id}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["invalid_edges"] == []


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
