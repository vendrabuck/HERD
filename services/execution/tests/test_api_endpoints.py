"""Tests for execution API endpoints with mocked driver execution."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, get_db
from app.main import app
from app.routers.executions import _require_internal_token, get_current_user_payload, require_admin
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

MOCK_DEVICE = {
    "id": DEVICE_ID,
    "name": "L1-Switch-01",
    "template_id": TEMPLATE_ID,
    "driver_id": DRIVER_ID,
    "driver_sha256": "abc123",
    "driver_filename": "driver.zip",
    "connection_type": "Layer 1 Switch",
    "field_data": {"ip_address": "10.0.1.50", "password": "secret"},
}

MOCK_TEMPLATE = {
    "id": TEMPLATE_ID,
    "name": "Test Template",
    "sections": [
        {
            "name": "Credentials",
            "fields": [
                {"key": "ip_address", "type": "string"},
                {"key": "password", "type": "password"},
            ],
        }
    ],
}

MOCK_SUCCESS_RESULT = {
    "success": True,
    "output": {"result": True},
    "error": None,
    "duration_ms": 100,
}

MOCK_FAILURE_RESULT = {
    "success": False,
    "output": None,
    "error": "Connection refused",
    "duration_ms": 50,
}

MOCK_TIMEOUT_RESULT = {
    "success": False,
    "output": None,
    "error": "Execution timed out after 30s",
    "duration_ms": 30000,
}


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


def _override_admin():
    return ADMIN_PAYLOAD


def _override_internal_token():
    return None


def _mock_fetch_device(device_data):
    async def _fetch(device_id):
        return device_data

    return _fetch


def _mock_fetch_template(template_data):
    async def _fetch(template_id):
        return template_data

    return _fetch


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def internal_client():
    app.dependency_overrides[_require_internal_token] = _override_internal_token
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- POST /execute: successful execution ---


@pytest.mark.asyncio
async def test_execute_success(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "status",
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert data["action"] == "status"
    assert data["device_id"] == DEVICE_ID
    assert data["driver_sha256"] == "abc123"
    assert data["duration_ms"] == 100
    # Password should be redacted in input_params
    assert data["input_params"]["HERD_password"] == "***REDACTED***"
    assert data["input_params"]["HERD_ip_address"] == "10.0.1.50"


@pytest.mark.asyncio
async def test_execute_with_ports(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "connect_ports",
                "user_id": USER_ID,
                "port_a": "1/1/1",
                "port_b": "1/1/2",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["port_a"] == "1/1/1"
    assert data["port_b"] == "1/1/2"
    assert data["action"] == "connect_ports"


# --- POST /execute: failure cases ---


@pytest.mark.asyncio
async def test_execute_driver_load_failure(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            side_effect=ValueError("Driver validation failed: Missing driver.py"),
        ),
    ):
        resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 201  # run record created, status=FAILED
    data = resp.json()
    assert data["status"] == "FAILED"
    assert "Missing driver.py" in data["error"]


@pytest.mark.asyncio
async def test_execute_driver_failure(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_FAILURE_RESULT,
        ),
    ):
        resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "FAILED"
    assert "Connection refused" in data["error"]


@pytest.mark.asyncio
async def test_execute_timeout(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_TIMEOUT_RESULT,
        ),
    ):
        resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "status",
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "TIMEOUT"
    assert "timed out" in data["error"]


# --- POST /device-check ---


@pytest.mark.asyncio
async def test_device_check_success(internal_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        resp = await internal_client.post(
            "/device-check",
            json={
                "device_id": DEVICE_ID,
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["device_id"] == DEVICE_ID
    assert data["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_device_check_login_failure(internal_client):
    """If login fails, device check returns FAILED without running status."""
    call_count = 0

    def mock_execute(**kwargs):
        nonlocal call_count
        call_count += 1
        action = kwargs.get("action", "")
        if action == "login":
            return MOCK_FAILURE_RESULT
        return MOCK_SUCCESS_RESULT

    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            side_effect=lambda **kwargs: mock_execute(**kwargs),
        ),
    ):
        resp = await internal_client.post(
            "/device-check",
            json={
                "device_id": DEVICE_ID,
                "user_id": USER_ID,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "FAILED"
    assert data["error"] is not None


# --- POST /runs/{id}/retry ---


@pytest.mark.asyncio
async def test_retry_failed_run(admin_client):
    """Create a failed run, then retry it successfully."""
    # First create a failed run
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_FAILURE_RESULT,
        ),
    ):
        create_resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )
    assert create_resp.status_code == 201
    failed_run = create_resp.json()
    assert failed_run["status"] == "FAILED"

    # Now retry it
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        retry_resp = await admin_client.post(f"/runs/{failed_run['id']}/retry")
    assert retry_resp.status_code == 200
    retry_data = retry_resp.json()
    assert retry_data["status"] == "SUCCESS"
    assert retry_data["id"] != failed_run["id"]  # new run


@pytest.mark.asyncio
async def test_retry_success_run_rejected(admin_client):
    """Cannot retry a successful run."""
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        create_resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "status",
                "user_id": USER_ID,
            },
        )
    assert create_resp.status_code == 201
    success_run = create_resp.json()
    assert success_run["status"] == "SUCCESS"

    resp = await admin_client.post(f"/runs/{success_run['id']}/retry")
    assert resp.status_code == 400
    assert "Only failed or timed-out" in resp.json()["detail"]


# --- GET /runs with data ---


@pytest.mark.asyncio
async def test_list_runs_with_data(admin_client):
    """Create runs and verify list returns them."""
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )
        await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "status",
                "user_id": USER_ID,
            },
        )

    resp = await admin_client.get("/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_list_runs_filter_by_status(admin_client):
    """Filter runs by status."""
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_FAILURE_RESULT,
        ),
    ):
        await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "login",
                "user_id": USER_ID,
            },
        )

    resp = await admin_client.get("/runs?status=SUCCESS")
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["status"] == "SUCCESS"


# --- GET /runs/{id} with data ---


@pytest.mark.asyncio
async def test_get_run_detail(admin_client):
    with (
        patch(
            "app.routers.executions.fetch_device",
            side_effect=_mock_fetch_device(MOCK_DEVICE),
        ),
        patch(
            "app.routers.executions.fetch_template",
            side_effect=_mock_fetch_template(MOCK_TEMPLATE),
        ),
        patch(
            "app.services.execution_service.load_driver",
            new_callable=AsyncMock,
            return_value="/tmp/driver",
        ),
        patch(
            "app.services.execution_service.execute_driver_method",
            return_value=MOCK_SUCCESS_RESULT,
        ),
    ):
        create_resp = await admin_client.post(
            "/execute",
            json={
                "device_id": DEVICE_ID,
                "action": "status",
                "user_id": USER_ID,
            },
        )
    run_id = create_resp.json()["id"]

    resp = await admin_client.get(f"/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert data["action"] == "status"
    assert data["status"] == "SUCCESS"


# --- Internal token validation ---


@pytest.mark.asyncio
async def test_device_check_requires_internal_token():
    """Device check without internal token override should fail."""
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/device-check",
            json={
                "device_id": DEVICE_ID,
                "user_id": USER_ID,
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403
