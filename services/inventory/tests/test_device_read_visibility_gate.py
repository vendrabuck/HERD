"""Group-visibility gate on the three reads issue #718 skipped (issue #909).

`GET /device-groups/device/{id}`, `GET /devices/{id}/apply-jobs`, and
`GET /apply-jobs/{id}` now apply the same non-admin group-visibility gate as
`GET /devices/{id}` and the device_configs.py config-version reads (issue
#718): a device outside the caller's groups 404s with the identical detail
the caller's own unknown-id 404 uses, so none of the three can be used to
tell "hidden" from "absent". Admins stay unfiltered.

Follows the harness pattern from test_device_configs.py's group-visibility
tests (issue #718): one shared AsyncClient per phase, built directly rather
than via a fixture, with the identity override switched between phases
against the same app instance.
"""

import io
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


def override_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "viewer", "role": "user"}


_mock_storage: dict[str, bytes] = {}


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
        patch(
            "app.services.driver_service.upload_object",
            side_effect=lambda key, data, **_: _mock_storage.__setitem__(key, data),
        ),
        patch(
            "app.services.driver_service.delete_object",
            side_effect=lambda key: _mock_storage.pop(key, None),
        ),
    ):
        yield


_driver_counter = 0


async def _create_driver(ac: AsyncClient) -> str:
    global _driver_counter
    _driver_counter += 1
    resp = await ac.post(
        "/drivers",
        data={"name": f"Drv{_driver_counter}", "connection_type": "Management"},
        files={"file": ("d.zip", io.BytesIO(b"PK\x03\x04"), "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_template(ac: AsyncClient) -> str:
    driver_id = await _create_driver(ac)
    resp = await ac.post(
        "/templates",
        json={
            "name": f"Tpl{uuid.uuid4().hex[:6]}",
            "vendor": "V",
            "model": "M",
            "driver_id": driver_id,
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_device(ac: AsyncClient) -> str:
    template_id = await _create_template(ac)
    resp = await ac.post(
        "/devices",
        json={
            "name": f"dev-{uuid.uuid4().hex[:6]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "X"},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _future(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _seed_device_with_job(ac: AsyncClient) -> tuple[str, str]:
    """As admin: create a device, a config version, and a scheduled apply
    job. Returns (device_id, job_id)."""
    device_id = await _create_device(ac)
    version = await ac.post(
        f"/devices/{device_id}/config-versions",
        json={"config": {"vlan": 100}},
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]
    job = await ac.post(
        f"/devices/{device_id}/config-versions/{version_id}/schedule",
        json={"scheduled_for": _future()},
    )
    assert job.status_code == 201, job.text
    return device_id, job.json()["id"]


async def _seed_grouped_device(ac: AsyncClient) -> tuple[str, str, str]:
    """As admin: create a device, a device group containing it, and a
    permission entry on the group. Returns (device_id, group_id, user_group_id)."""
    device_id = await _create_device(ac)
    group_resp = await ac.post(
        "/device-groups", json={"name": f"Grp{uuid.uuid4().hex[:6]}", "description": "d"}
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]
    add_resp = await ac.post(
        f"/device-groups/{group_id}/devices/bulk", json={"device_ids": [device_id]}
    )
    assert add_resp.status_code == 200, add_resp.text
    user_group_id = str(uuid.uuid4())
    perm_resp = await ac.post(
        f"/device-groups/{group_id}/permissions/bulk",
        json={"user_group_ids": [user_group_id]},
    )
    assert perm_resp.status_code == 200, perm_resp.text
    return device_id, group_id, user_group_id


# --- Route table: the three previously-ungated reads (issue #909) ----------
#
# Each case supplies the request path (given a real device_id/job_id) and
# the exact unknown-id 404 detail that route already used before this fix,
# so the hidden-device case is checked against each route's OWN existing
# phrasing rather than a new one.

ROUTE_CASES = ["device_groups_device", "list_apply_jobs", "get_apply_job"]


def _case_for(route: str, device_id: str, job_id: str) -> tuple[str, str]:
    """Return (path, expected_404_detail_for_this_real_id) for a route case."""
    if route == "device_groups_device":
        return f"/device-groups/device/{device_id}", f"Device {device_id} not found"
    if route == "list_apply_jobs":
        return f"/devices/{device_id}/apply-jobs", "Device not found"
    if route == "get_apply_job":
        return f"/apply-jobs/{job_id}", "Apply job not found"
    raise AssertionError(route)


def _unknown_case_for(route: str) -> tuple[str, str]:
    """Return (path, expected_404_detail) for an id that does not exist at all."""
    unknown_id = str(uuid.uuid4())
    if route == "device_groups_device":
        return f"/device-groups/device/{unknown_id}", f"Device {unknown_id} not found"
    if route == "list_apply_jobs":
        return f"/devices/{unknown_id}/apply-jobs", "Device not found"
    if route == "get_apply_job":
        return f"/apply-jobs/{unknown_id}", "Apply job not found"
    raise AssertionError(route)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTE_CASES)
async def test_hidden_device_404_matches_unknown_id_404(route):
    """A non-admin outside the device's groups gets 404 with a detail that
    reuses the route's OWN unknown-id phrasing verbatim: no second phrasing
    is introduced for the hidden case."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, job_id = await _seed_device_with_job(ac)

        hidden_path, hidden_expected_detail = _case_for(route, device_id, job_id)
        unknown_path, unknown_expected_detail = _unknown_case_for(route)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value=set()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                hidden_resp = await ac.get(hidden_path)
                unknown_resp = await ac.get(unknown_path)

        assert hidden_resp.status_code == 404
        assert unknown_resp.status_code == 404
        assert hidden_resp.json()["detail"] == hidden_expected_detail
        assert unknown_resp.json()["detail"] == unknown_expected_detail
        # For the plain-string routes the phrasing carries no id, so the two
        # details are not just same-template but literally byte-identical.
        if route != "device_groups_device":
            assert hidden_resp.json()["detail"] == unknown_resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTE_CASES)
async def test_non_admin_inside_groups_sees_same_body_as_admin(route):
    """A non-admin whose groups (per the mocked visibility resolver) cover
    the device sees exactly what an admin sees: the gate only changes the
    hidden case, never the visible one."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, job_id = await _seed_device_with_job(ac)
            path, _ = _case_for(route, device_id, job_id)
            admin_resp = await ac.get(path)
        assert admin_resp.status_code == 200

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(return_value={uuid.UUID(device_id)}),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                user_resp = await ac.get(path)
        assert user_resp.status_code == 200
        assert user_resp.json() == admin_resp.json()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTE_CASES)
async def test_visibility_unavailable_fails_closed_503(route):
    """An auth-service outage while resolving the caller's visible devices
    must 503, never fall through to showing (or hiding) the device."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, job_id = await _seed_device_with_job(ac)
            path, _ = _case_for(route, device_id, job_id)

        app.dependency_overrides[get_current_user_payload] = override_user
        with patch(
            "app.services.device_visibility._resolve_visible_device_ids",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=503, detail="auth service unreachable while fetching user groups"
                )
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get(path)
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTE_CASES)
async def test_admin_never_consults_visibility(route):
    """Admin reads never call the visibility resolver at all: patch it to
    blow up and prove the admin path short-circuits before it."""
    app.dependency_overrides[get_db] = override_get_db
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, job_id = await _seed_device_with_job(ac)
            path, _ = _case_for(route, device_id, job_id)

            with patch(
                "app.services.device_visibility._resolve_visible_device_ids",
                new=AsyncMock(side_effect=AssertionError("admin must not check visibility")),
            ):
                resp = await ac.get(path)
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_device_groups_for_device_non_admin_visible_returns_group_data():
    """A non-admin covered by a real device-group/permission assignment (not
    just the mocked visibility resolver) sees the actual group and
    user-group data, matching what #392's admin-only tests already prove for
    admins."""
    app.dependency_overrides[get_db] = override_get_db
    ug_names = AsyncMock(return_value={})
    try:
        app.dependency_overrides[get_current_user_payload] = override_admin
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_id, group_id, user_group_id = await _seed_grouped_device(ac)
            ug_names.return_value = {uuid.UUID(user_group_id): "Network Engineers"}
            with patch("app.routers.device_groups._fetch_user_group_names", new=ug_names):
                admin_resp = await ac.get(f"/device-groups/device/{device_id}")
        assert admin_resp.status_code == 200
        assert len(admin_resp.json()) == 1
        assert admin_resp.json()[0]["id"] == group_id
        assert admin_resp.json()[0]["user_groups"][0]["user_group_name"] == "Network Engineers"

        app.dependency_overrides[get_current_user_payload] = override_user
        with (
            patch(
                "app.services.device_visibility._resolve_visible_device_ids",
                new=AsyncMock(return_value={uuid.UUID(device_id)}),
            ),
            patch("app.routers.device_groups._fetch_user_group_names", new=ug_names),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                user_resp = await ac.get(f"/device-groups/device/{device_id}")
        assert user_resp.status_code == 200
        assert user_resp.json() == admin_resp.json()
    finally:
        app.dependency_overrides.clear()
