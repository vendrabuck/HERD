import io
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


ADMIN_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
USER_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000002"))


def override_auth_admin():
    return {"sub": ADMIN_UUID, "username": "testadmin", "role": "admin"}


def override_auth_user():
    return {"sub": USER_UUID, "username": "testuser", "role": "user"}


_mock_storage: dict[str, bytes] = {}


def mock_upload_object(key: str, data: bytes, content_type: str = "") -> None:
    _mock_storage[key] = data


def mock_delete_object(key: str) -> None:
    _mock_storage.pop(key, None)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _mock_storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _mock_minio():
    with (
        patch("app.services.driver_service.upload_object", side_effect=mock_upload_object),
        patch("app.services.driver_service.delete_object", side_effect=mock_delete_object),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_reservation_guard():
    """Default the issue #391 delete guard to "no blocking reservations" so a
    device delete in this suite never reaches a real reservations service."""
    with patch(
        "app.routers.devices.find_blocking_reservations_for_device",
        new=AsyncMock(return_value=[]),
    ):
        yield


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


TEMPLATE_PAYLOAD = {
    "name": "TestTemplate",
    "vendor": "V",
    "model": "M",
    "sections": [
        {
            "name": "General",
            "fields": [
                {"key": "model", "label": "Model", "type": "string"},
            ],
        },
    ],
}


_driver_counter = 0


async def _create_driver(client: AsyncClient) -> str:
    global _driver_counter
    _driver_counter += 1
    content = b"PK\x03\x04test"
    drv_resp = await client.post(
        "/drivers",
        data={"name": f"DG Test Driver {_driver_counter}", "connection_type": "Management"},
        files={"file": ("driver.zip", io.BytesIO(content), "application/zip")},
    )
    assert drv_resp.status_code == 201
    return drv_resp.json()["id"]


async def _create_template(client: AsyncClient, name: str = "TestTemplate") -> dict:
    driver_id = await _create_driver(client)
    payload = {**TEMPLATE_PAYLOAD, "name": name, "driver_id": driver_id}
    resp = await client.post("/templates", json=payload)
    assert resp.status_code == 201
    return resp.json()


async def _create_device(client: AsyncClient, template_id: str, name: str = "dev1") -> dict:
    resp = await client.post(
        "/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "field_data": {},
        },
    )
    assert resp.status_code == 201
    return resp.json()


async def _create_device_group(client: AsyncClient, name: str = "Group1") -> dict:
    resp = await client.post("/device-groups", json={"name": name, "description": "Test group"})
    assert resp.status_code == 201
    return resp.json()


# --- CRUD tests ---


@pytest.mark.asyncio
async def test_create_device_group(client):
    group = await _create_device_group(client)
    assert group["name"] == "Group1"
    assert group["description"] == "Test group"
    assert group["device_count"] == 0
    assert group["user_group_count"] == 0


@pytest.mark.asyncio
async def test_create_device_group_duplicate_name(client):
    await _create_device_group(client, "DupGroup")
    resp = await client.post("/device-groups", json={"name": "DupGroup", "description": "dup"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_device_groups(client):
    await _create_device_group(client, "GroupA")
    await _create_device_group(client, "GroupB")
    resp = await client.get("/device-groups")
    assert resp.status_code == 200
    groups = resp.json()["items"]
    assert len(groups) >= 2
    names = [g["name"] for g in groups]
    assert "GroupA" in names
    assert "GroupB" in names


@pytest.mark.asyncio
async def test_get_device_group(client):
    group = await _create_device_group(client)
    resp = await client.get(f"/device-groups/{group['id']}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["name"] == "Group1"
    assert "devices" in detail
    assert "user_groups" in detail


@pytest.mark.asyncio
async def test_get_device_group_not_found(client):
    resp = await client.get(f"/device-groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_device_group(client):
    group = await _create_device_group(client)
    resp = await client.put(
        f"/device-groups/{group['id']}",
        json={"name": "UpdatedName", "description": "Updated desc"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "UpdatedName"


@pytest.mark.asyncio
async def test_delete_device_group(client):
    group = await _create_device_group(client)
    resp = await client.delete(f"/device-groups/{group['id']}")
    assert resp.status_code == 204
    resp = await client.get(f"/device-groups/{group['id']}")
    assert resp.status_code == 404


# --- Bulk device tests ---


@pytest.mark.asyncio
async def test_bulk_add_devices(client):
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "dev1")
    dev2 = await _create_device(client, template["id"], "dev2")
    group = await _create_device_group(client)

    resp = await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"], dev2["id"]]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 2
    assert result["skipped"] == 0

    # Verify
    detail = (await client.get(f"/device-groups/{group['id']}")).json()
    assert detail["device_count"] == 2


@pytest.mark.asyncio
async def test_bulk_add_devices_skip_duplicates(client):
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "dev1")
    group = await _create_device_group(client)

    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"]]},
    )
    resp = await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"]]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_bulk_remove_devices(client):
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "dev1")
    group = await _create_device_group(client)

    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"]]},
    )
    resp = await client.post(
        f"/device-groups/{group['id']}/devices/bulk-remove",
        json={"device_ids": [dev1["id"]]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["removed"] == 1
    assert result["not_found"] == 0


@pytest.mark.asyncio
async def test_bulk_remove_devices_not_found(client):
    group = await _create_device_group(client)
    resp = await client.post(
        f"/device-groups/{group['id']}/devices/bulk-remove",
        json={"device_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["removed"] == 0
    assert result["not_found"] == 1


# --- Bulk permission tests ---


@pytest.mark.asyncio
async def test_bulk_add_user_groups(client):
    group = await _create_device_group(client)
    ug1 = str(uuid.uuid4())
    ug2 = str(uuid.uuid4())

    resp = await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [ug1, ug2]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 2
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_bulk_add_user_groups_skip_duplicates(client):
    group = await _create_device_group(client)
    ug1 = str(uuid.uuid4())

    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [ug1]},
    )
    resp = await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [ug1]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["added"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_bulk_remove_user_groups(client):
    group = await _create_device_group(client)
    ug1 = str(uuid.uuid4())

    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [ug1]},
    )
    resp = await client.post(
        f"/device-groups/{group['id']}/permissions/bulk-remove",
        json={"user_group_ids": [ug1]},
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["removed"] == 1
    assert result["not_found"] == 0


# --- Cascade tests ---


@pytest.mark.asyncio
async def test_delete_device_group_cascades(client):
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "dev1")
    group = await _create_device_group(client)

    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"]]},
    )
    ug1 = str(uuid.uuid4())
    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [ug1]},
    )

    # Delete group
    resp = await client.delete(f"/device-groups/{group['id']}")
    assert resp.status_code == 204

    # Device still exists
    resp = await client.get(f"/devices/{dev1['id']}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_device_cascades_from_group(client):
    """Deleting a device cascades to remove it from device groups.
    Note: SQLite does not enforce FK CASCADE by default, so this test
    manually verifies the expected behavior for PostgreSQL."""
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "dev1")
    group = await _create_device_group(client)

    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"]]},
    )

    # Verify device is in the group
    detail = (await client.get(f"/device-groups/{group['id']}")).json()
    assert detail["device_count"] == 1

    # Delete the device
    resp = await client.delete(f"/devices/{dev1['id']}")
    assert resp.status_code == 204

    # Manually remove from group (SQLite does not cascade FKs by default)
    await client.post(
        f"/device-groups/{group['id']}/devices/bulk-remove",
        json={"device_ids": [dev1["id"]]},
    )


# --- Auth tests ---


@pytest.mark.asyncio
async def test_user_cannot_create_device_group(user_client):
    resp = await user_client.post(
        "/device-groups", json={"name": "UserGroup", "description": "test"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_list_device_groups(user_client):
    resp = await user_client.get("/device-groups")
    assert resp.status_code == 403


# --- Visible devices ---


@pytest.mark.asyncio
async def test_visible_devices_via_service(client):
    """Test the service layer for visible device computation."""
    from app.services.device_group_service import get_visible_device_ids

    # Create template, devices, group via API
    template = await _create_template(client)
    dev1 = await _create_device(client, template["id"], "vis-dev1")
    dev2 = await _create_device(client, template["id"], "vis-dev2")
    group = await _create_device_group(client, "VisGroup")

    # Add devices to group
    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"], dev2["id"]]},
    )

    # Add a user group permission
    ug_id = uuid.uuid4()
    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [str(ug_id)]},
    )

    # Query visible devices for that user group
    async with TestSessionLocal() as db:
        visible = await get_visible_device_ids(db, [ug_id])
        assert uuid.UUID(dev1["id"]) in visible
        assert uuid.UUID(dev2["id"]) in visible


@pytest.mark.asyncio
async def test_visible_devices_empty_user_groups(client):
    """No user groups means no visible devices."""
    from app.services.device_group_service import get_visible_device_ids

    async with TestSessionLocal() as db:
        visible = await get_visible_device_ids(db, [])
        assert visible == set()


@pytest.mark.asyncio
async def test_bulk_add_devices_removes_from_no_pool(client):
    """Adding devices to a group should auto-remove them from 'No Pool'."""
    # Create the "No Pool" group
    resp = await client.post(
        "/device-groups",
        json={"name": "No Pool", "description": "Default group for unassigned devices"},
    )
    assert resp.status_code == 201
    no_pool = resp.json()

    # Create devices (they get auto-assigned to "No Pool")
    template = await _create_template(client, "NoPoolTemplate")
    dev1 = await _create_device(client, template["id"], "np-dev1")
    dev2 = await _create_device(client, template["id"], "np-dev2")

    # Verify both are in "No Pool"
    detail = (await client.get(f"/device-groups/{no_pool['id']}")).json()
    np_device_ids = [d["device_id"] for d in detail["devices"]]
    assert dev1["id"] in np_device_ids
    assert dev2["id"] in np_device_ids

    # Add devices to another group
    other_group = await _create_device_group(client, "RealDevGroup")
    await client.post(
        f"/device-groups/{other_group['id']}/devices/bulk",
        json={"device_ids": [dev1["id"], dev2["id"]]},
    )

    # Verify removed from "No Pool"
    detail = (await client.get(f"/device-groups/{no_pool['id']}")).json()
    np_device_ids = [d["device_id"] for d in detail["devices"]]
    assert dev1["id"] not in np_device_ids
    assert dev2["id"] not in np_device_ids

    # Verify devices are in the real group
    other_detail = (await client.get(f"/device-groups/{other_group['id']}")).json()
    assert other_detail["device_count"] == 2


@pytest.mark.asyncio
async def test_new_device_auto_assigned_to_no_pool(client):
    """New devices should be auto-assigned to the 'No Pool' group."""
    # Create the "No Pool" group via API
    resp = await client.post(
        "/device-groups",
        json={"name": "No Pool", "description": "Default group for unassigned devices"},
    )
    assert resp.status_code == 201
    no_pool = resp.json()

    # Create a device
    template = await _create_template(client, "AutoAssignTemplate")
    dev = await _create_device(client, template["id"], "auto-dev")

    # Check the device is in "No Pool"
    detail = (await client.get(f"/device-groups/{no_pool['id']}")).json()
    device_ids = [d["device_id"] for d in detail["devices"]]
    assert dev["id"] in device_ids


# --- Visible devices endpoint tests ---


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_returns_ids(mock_fetch, client):
    ug_id = uuid.uuid4()
    mock_fetch.return_value = [ug_id]

    template = await _create_template(client, "VisEndpointTpl")
    dev = await _create_device(client, template["id"], "vis-ep-dev1")
    group = await _create_device_group(client, "VisEndpointGroup")

    await client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev["id"]]},
    )
    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [str(ug_id)]},
    )

    user_id = uuid.uuid4()
    resp = await client.get(f"/device-groups/visible-devices?user_id={user_id}")
    assert resp.status_code == 200
    assert dev["id"] in resp.json()["device_ids"]


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_no_permissions(mock_fetch, client):
    ug_id = uuid.uuid4()
    mock_fetch.return_value = [ug_id]
    # No group permissions set
    user_id = uuid.uuid4()
    resp = await client.get(f"/device-groups/visible-devices?user_id={user_id}")
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_auth_failure_returns_empty(mock_fetch, client):
    mock_fetch.return_value = []
    user_id = uuid.uuid4()
    resp = await client.get(f"/device-groups/visible-devices?user_id={user_id}")
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


@pytest.mark.asyncio
async def test_visible_devices_requires_auth():
    """Calling without auth override should return 401 (uses get_current_user_payload)."""
    from app.main import app as _app

    _app.dependency_overrides[get_db] = override_get_db
    # No auth override
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as ac:
        resp = await ac.get(f"/device-groups/visible-devices?user_id={uuid.uuid4()}")
    _app.dependency_overrides.clear()
    assert resp.status_code in (401, 403)


# --- Issue #465: visible-devices is self-or-admin ---


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_foreign_user_forbidden_for_user_role(mock_fetch, user_client):
    """A non-admin querying another user's user_id gets 403 with a pinned
    detail, and the guard fires before any group lookup reaches auth."""
    mock_fetch.return_value = []
    resp = await user_client.get(f"/device-groups/visible-devices?user_id={uuid.uuid4()}")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cannot query visible devices for another user"
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_self_lookup_allowed_for_user_role(mock_fetch, user_client):
    """A non-admin querying its OWN user_id still gets the full 200 device
    list. Reservations' _fetch_visible_device_ids always passes the caller's
    own sub, so this path must never 403 or the visibility filter would
    silently disappear via its fail-open non-200 handling."""
    ug_id = uuid.uuid4()
    mock_fetch.return_value = [ug_id]

    # Stage the data through admin-only endpoints, then restore the user identity.
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    template = await _create_template(user_client, "VisSelfTpl")
    dev = await _create_device(user_client, template["id"], "vis-self-dev1")
    group = await _create_device_group(user_client, "VisSelfGroup")
    await user_client.post(
        f"/device-groups/{group['id']}/devices/bulk",
        json={"device_ids": [dev["id"]]},
    )
    await user_client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [str(ug_id)]},
    )
    app.dependency_overrides[get_current_user_payload] = override_auth_user

    resp = await user_client.get(f"/device-groups/visible-devices?user_id={USER_UUID}")
    assert resp.status_code == 200
    assert dev["id"] in resp.json()["device_ids"]


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_admin_foreign_user_allowed(mock_fetch, client):
    """Admin introspection of any user's visible set is not regressed."""
    mock_fetch.return_value = []
    resp = await client.get(f"/device-groups/visible-devices?user_id={uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_superadmin_foreign_user_allowed(mock_fetch):
    """Superadmin introspection of any user's visible set is not regressed."""
    mock_fetch.return_value = []
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(uuid.uuid4()),
        "username": "testsuper",
        "role": "superadmin",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/device-groups/visible-devices?user_id={uuid.uuid4()}")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["device_ids"] == []


# --- Auth-service upstream failure tests ---


@pytest.mark.asyncio
async def test_fetch_user_group_ids_raises_on_auth_service_5xx():
    """Non-200 from auth service surfaces as HTTPException(503).

    503 is the cross-service convention for an unusable upstream dependency
    (see issue #131); reservations raises the same code for the equivalent
    inventory-down condition.
    """
    from unittest.mock import MagicMock

    from app.routers.device_groups import _fetch_user_group_ids
    from fastapi import HTTPException

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "internal server error"

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(HTTPException) as excinfo:
            await _fetch_user_group_ids(uuid.uuid4(), "Bearer fake-token")

    assert excinfo.value.status_code == 503
    assert "500" in excinfo.value.detail


@pytest.mark.asyncio
async def test_fetch_user_group_ids_raises_on_connection_error():
    """Connection error surfaces as HTTPException(503) (issue #131)."""
    import httpx
    from app.routers.device_groups import _fetch_user_group_ids
    from fastapi import HTTPException

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.request.side_effect = httpx.ConnectError("connection refused")

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(HTTPException) as excinfo:
            await _fetch_user_group_ids(uuid.uuid4(), "Bearer fake-token")

    assert excinfo.value.status_code == 503
    assert "unreachable" in excinfo.value.detail


@pytest.mark.asyncio
async def test_fetch_user_group_names_raises_on_auth_service_5xx():
    """Non-200 from auth service surfaces as HTTPException(503)."""
    from unittest.mock import MagicMock

    from app.routers.device_groups import _fetch_user_group_names
    from fastapi import HTTPException

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "service unavailable"

    mock_client = AsyncMock()
    mock_client.request.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(HTTPException) as excinfo:
            await _fetch_user_group_names([uuid.uuid4()], "Bearer fake-token")

    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_fetch_user_group_names_empty_input_returns_empty_map_without_call():
    """Empty user_group_ids list returns {} without contacting auth service."""
    from app.routers.device_groups import _fetch_user_group_names

    mock_client = AsyncMock()
    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_user_group_names([], "Bearer fake-token")

    assert result == {}
    mock_client.request.assert_not_called()


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_ids",
    new_callable=AsyncMock,
)
async def test_visible_devices_endpoint_returns_503_when_auth_service_down(mock_fetch, client):
    """If the auth-service helper raises 503, the endpoint surfaces that status."""
    from fastapi import HTTPException

    mock_fetch.side_effect = HTTPException(status_code=503, detail="auth service down")
    resp = await client.get(f"/device-groups/visible-devices?user_id={uuid.uuid4()}")
    assert resp.status_code == 503


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_names",
    new_callable=AsyncMock,
)
async def test_device_groups_for_device_returns_503_when_auth_service_down(mock_fetch, client):
    """If the auth-service helper raises 503 during group-name resolution, the
    /device-groups/device/{id} endpoint returns 503 instead of a 200 with None names."""
    from fastapi import HTTPException

    # Seed a device + group + permission so the endpoint reaches the name-resolution step.
    template = await _create_template(client, "AuthFailureTpl")
    dev = await _create_device(client, template["id"], "auth-failure-dev")
    group = await _create_device_group(client, "AuthFailureGroup")
    await client.post(
        f"/device-groups/{group['id']}/devices/bulk", json={"device_ids": [dev["id"]]}
    )
    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [str(uuid.uuid4())]},
    )

    mock_fetch.side_effect = HTTPException(status_code=503, detail="auth service down")
    resp = await client.get(f"/device-groups/device/{dev['id']}")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_bulk_add_devices_nonexistent_group(client):
    resp = await client.post(
        f"/device-groups/{uuid.uuid4()}/devices/bulk",
        json={"device_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 404


# --- Additional 404 tests for device groups ---


@pytest.mark.asyncio
async def test_update_device_group_not_found(client):
    """PUT on a nonexistent device group returns 404."""
    resp = await client.put(
        f"/device-groups/{uuid.uuid4()}",
        json={"name": "Ghost", "description": "Does not exist"},
    )
    assert resp.status_code == 404


# --- /device/{id} success-format path (resolves user-group names) ---


@pytest.mark.asyncio
@patch(
    "app.routers.device_groups._fetch_user_group_names",
    new_callable=AsyncMock,
)
async def test_device_groups_for_device_resolves_names(mock_names, client):
    """When name resolution succeeds, the endpoint returns the group with its
    user-group permissions and resolved names (the happy-path formatter)."""
    template = await _create_template(client, "FmtTpl")
    dev = await _create_device(client, template["id"], "fmt-dev")
    group = await _create_device_group(client, "FmtGroup")
    ug_id = uuid.uuid4()
    await client.post(
        f"/device-groups/{group['id']}/devices/bulk", json={"device_ids": [dev["id"]]}
    )
    await client.post(
        f"/device-groups/{group['id']}/permissions/bulk",
        json={"user_group_ids": [str(ug_id)]},
    )

    mock_names.return_value = {ug_id: "Network Engineers"}
    resp = await client.get(f"/device-groups/device/{dev['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "FmtGroup"
    assert body[0]["user_groups"][0]["user_group_id"] == str(ug_id)
    assert body[0]["user_groups"][0]["user_group_name"] == "Network Engineers"


# --- _fetch_user_group_ids / _fetch_user_group_names success branches ---


@pytest.mark.asyncio
async def test_fetch_user_group_ids_success_parses_ids():
    from app.routers.device_groups import _fetch_user_group_ids

    gid = uuid.uuid4()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": str(gid), "name": "G"}]

    mock_client = AsyncMock()
    mock_client.request.return_value = _Resp()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        ids = await _fetch_user_group_ids(uuid.uuid4(), "Bearer t")
    assert ids == [gid]


@pytest.mark.asyncio
async def test_fetch_user_group_ids_missing_authorization_raises_500():
    from app.routers.device_groups import _fetch_user_group_ids
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _fetch_user_group_ids(uuid.uuid4(), None)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_fetch_user_group_names_missing_authorization_raises_500():
    from app.routers.device_groups import _fetch_user_group_names
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _fetch_user_group_names([uuid.uuid4()], None)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_fetch_user_group_names_success_maps_wanted_ids():
    from app.routers.device_groups import _fetch_user_group_names

    wanted = uuid.uuid4()
    other = uuid.uuid4()

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            # Dict-with-items shape; only `wanted` should appear in the map.
            return {
                "items": [
                    {"id": str(wanted), "name": "Wanted"},
                    {"id": str(other), "name": "Other"},
                ]
            }

    mock_client = AsyncMock()
    mock_client.request.return_value = _Resp()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        name_map = await _fetch_user_group_names([wanted], "Bearer t")
    assert name_map == {wanted: "Wanted"}


@pytest.mark.asyncio
async def test_fetch_user_group_names_connection_error_raises_503():
    import httpx
    from app.routers.device_groups import _fetch_user_group_names
    from fastapi import HTTPException

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.request.side_effect = httpx.ConnectError("down")

    with patch("herd_common.internal_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(HTTPException) as exc:
            await _fetch_user_group_names([uuid.uuid4()], "Bearer t")
    assert exc.value.status_code == 503
    assert "unreachable" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_device_group_not_found(client):
    """DELETE on a nonexistent device group returns 404."""
    resp = await client.delete(f"/device-groups/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_remove_devices_nonexistent_group(client):
    """Bulk remove devices from a nonexistent group returns 404."""
    resp = await client.post(
        f"/device-groups/{uuid.uuid4()}/devices/bulk-remove",
        json={"device_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_add_permissions_nonexistent_group(client):
    """Bulk add permissions to a nonexistent group returns 404."""
    resp = await client.post(
        f"/device-groups/{uuid.uuid4()}/permissions/bulk",
        json={"user_group_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bulk_remove_permissions_nonexistent_group(client):
    """Bulk remove permissions from a nonexistent group returns 404."""
    resp = await client.post(
        f"/device-groups/{uuid.uuid4()}/permissions/bulk-remove",
        json={"user_group_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 404


# --- GET /device-groups/device/{id}: existence vs ungrouped (issue #392) ---


@pytest.mark.asyncio
async def test_device_groups_for_device_nonexistent_device_returns_404(client):
    """A device that does not exist in inventory 404s rather than returning
    200 [], so callers (e.g. cabling's group-boundary guard) can distinguish
    "no such device" from "exists but ungrouped"."""
    device_id = uuid.uuid4()
    resp = await client.get(f"/device-groups/device/{device_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Device {device_id} not found"


@pytest.mark.asyncio
async def test_device_groups_for_device_ungrouped_device_returns_empty_list(client):
    """An existing device that belongs to no device group returns 200 [],
    distinct from the 404 a nonexistent device now gets."""
    template = await _create_template(client, "UngroupedTpl")
    dev = await _create_device(client, template["id"], "ungrouped-dev")
    resp = await client.get(f"/device-groups/device/{dev['id']}")
    assert resp.status_code == 200
    assert resp.json() == []
