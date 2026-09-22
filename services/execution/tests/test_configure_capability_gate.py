"""Enumerated coverage for execution's own configure-capability gate (issue #870).

Issue #839 put the "can this driver configure" check in inventory's two apply
routes (services/inventory/app/services/manage_guard.py). Execution itself
checked nothing: `POST /execute` (admin, or non-admin with an ACL manage
grant), `POST /execute/internal` (service-to-service), and the AI commit path
(which POSTs straight to `/execute`) all accepted action="configure" against
ANY driver connection type and only failed once the sandbox's
`getattr(driver, "configure")` raised.

`_assert_action_permitted` (execution_service.py) closes this at the single
choke point every execute path runs through, `run_driver_action`, before any
ExecutionRun row is created and before any driver load: this file drives it
by connection type (every entry in driver_loader.REQUIRED_METHODS, the
contract source of truth that CONFIGURE_CONNECTION_TYPES is pinned against by
test_configure_capability_parity.py), by route (/execute and
/execute/internal), and by caller (admin and non-admin), plus the
device-has-no-driver case the same helper refuses for every action.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.database import Base, get_db
from app.main import app
from app.models.execution_run import ExecutionRun
from app.routers import executions as ex_router
from app.routers.executions import (
    _require_internal_token,
    get_current_user_payload,
    require_admin,
)
from app.services import execution_service as ex_service
from app.services.driver_loader import REQUIRED_METHODS
from app.services.execution_service import _assert_action_permitted, run_driver_action
from fastapi import HTTPException
from herd_common.device_config import CONFIGURE_CONNECTION_TYPES
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}
USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)

# The full connection-type space from the contract itself, not a hand-copied
# guess: a future connection type added to REQUIRED_METHODS is automatically
# exercised here too.
CONNECTION_TYPES = sorted(REQUIRED_METHODS)


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


async def _run_count() -> int:
    async with TestSessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(ExecutionRun))
        return result.scalar()


def _device_data(connection_type: str | None, *, driver_id: str | None = DRIVER_ID) -> dict:
    return {
        "id": DEVICE_ID,
        "name": "dev-under-test",
        "driver_id": driver_id,
        "driver_name": "some-driver",
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": connection_type,
        "template_id": TEMPLATE_ID,
    }


def _template_data() -> dict:
    return {"id": TEMPLATE_ID, "password_keys": []}


def _expected_driver_cannot_configure_detail(connection_type: str) -> dict:
    return {
        "error": "driver_cannot_configure",
        "connection_type": connection_type,
        "driver": "some-driver",
        "message": (
            f"This device's driver implements the {connection_type} "
            "contract, which has no configure method, so a config apply "
            "cannot run. Config versions on this device store intent only."
        ),
    }


def _expected_no_driver_detail() -> dict:
    return {
        "error": "device_has_no_driver",
        "message": "Device dev-under-test has no driver assigned, so no action can run against it.",
    }


def _stub_driver_success(monkeypatch) -> None:
    """Stub the driver pipeline to a clean SUCCESS, for connection types the
    gate is expected to let through."""
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )


@pytest.fixture
async def admin_client():
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client(monkeypatch):
    # A manage grant is stubbed True so the 403 authorization guard never
    # masks the 409 capability gate this file is testing.
    monkeypatch.setattr(ex_router, "_user_has_acl_manage", AsyncMock(return_value=True))
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- /execute, admin caller: every connection type in the contract ---------


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
async def test_execute_admin_configure_by_connection_type(
    admin_client, monkeypatch, connection_type
):
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(connection_type))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": ADMIN_ID,
            "method_kwargs": {},
        },
    )

    if connection_type in CONFIGURE_CONNECTION_TYPES:
        assert resp.status_code == 201, resp.text
        assert await _run_count() == 1
    else:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == _expected_driver_cannot_configure_detail(connection_type)
        assert await _run_count() == 0


# --- /execute/internal, service-to-service caller: same matrix -------------


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
async def test_internal_execute_configure_by_connection_type(
    admin_client, monkeypatch, connection_type
):
    """admin_client also no-ops _require_internal_token, so it doubles as the
    internal-caller fixture for this route (matches test_router_endpoints.py's
    test_internal_execute_uses_internal_token precedent)."""
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(connection_type))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await admin_client.post(
        "/execute/internal",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": ADMIN_ID,
            "method_kwargs": {},
        },
    )

    if connection_type in CONFIGURE_CONNECTION_TYPES:
        assert resp.status_code == 201, resp.text
        assert await _run_count() == 1
    else:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == _expected_driver_cannot_configure_detail(connection_type)
        assert await _run_count() == 0


# --- /execute, non-admin caller with a manage grant: same matrix -----------


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
async def test_execute_non_admin_configure_by_connection_type(
    user_client, monkeypatch, connection_type
):
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(connection_type))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await user_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": {},
        },
        headers={"Authorization": "Bearer t"},
    )

    if connection_type in CONFIGURE_CONNECTION_TYPES:
        assert resp.status_code == 201, resp.text
        assert await _run_count() == 1
    else:
        # Non-admin, ACL-manage-holding caller gets the SAME structured 409 as
        # an admin: the gate is a capability check, not an authorization one.
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == _expected_driver_cannot_configure_detail(connection_type)
        assert await _run_count() == 0


# --- device_has_no_driver: refused for every action, both routes, both roles


@pytest.mark.asyncio
async def test_execute_admin_no_driver_refused_for_configure(admin_client, monkeypatch):
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(None, driver_id=None))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await admin_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "configure", "user_id": ADMIN_ID},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == _expected_no_driver_detail()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_execute_admin_no_driver_refused_for_non_configure_action(admin_client, monkeypatch):
    """The no-driver refusal is unconditional, not gated on action=='configure'."""
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(None, driver_id=None))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await admin_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "status", "user_id": ADMIN_ID},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == _expected_no_driver_detail()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_internal_execute_no_driver_refused(admin_client, monkeypatch):
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(None, driver_id=None))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await admin_client.post(
        "/execute/internal",
        json={"device_id": DEVICE_ID, "action": "configure", "user_id": ADMIN_ID},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == _expected_no_driver_detail()
    assert await _run_count() == 0


@pytest.mark.asyncio
async def test_execute_non_admin_no_driver_refused(user_client, monkeypatch):
    monkeypatch.setattr(
        ex_router, "fetch_device", AsyncMock(return_value=_device_data(None, driver_id=None))
    )
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    _stub_driver_success(monkeypatch)

    resp = await user_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "configure", "user_id": USER_ID},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == _expected_no_driver_detail()
    assert await _run_count() == 0


# --- Direct service-layer coverage of _assert_action_permitted / run_driver_action


def test_assert_action_permitted_allows_management_configure():
    """No exception for the one connection type whose contract has configure."""
    _assert_action_permitted(_device_data("Management"), "configure")


@pytest.mark.parametrize(
    "connection_type", [ct for ct in CONNECTION_TYPES if ct not in CONFIGURE_CONNECTION_TYPES]
)
def test_assert_action_permitted_raises_409_for_every_non_configurable_type(connection_type):
    with pytest.raises(HTTPException) as exc:
        _assert_action_permitted(_device_data(connection_type), "configure")
    assert exc.value.status_code == 409
    assert exc.value.detail == _expected_driver_cannot_configure_detail(connection_type)


@pytest.mark.parametrize("connection_type", CONNECTION_TYPES)
def test_assert_action_permitted_ignores_non_configure_actions(connection_type):
    """The capability check only applies to action=='configure'; every other
    action is left to the driver contract's REQUIRED_METHODS enforcement at
    load time, not this gate."""
    for action in ("login", "logout", "status"):
        _assert_action_permitted(_device_data(connection_type), action)


def test_assert_action_permitted_raises_409_for_no_driver_regardless_of_action():
    for action in ("configure", "status", "login", "connect_ports"):
        with pytest.raises(HTTPException) as exc:
            _assert_action_permitted(_device_data(None, driver_id=None), action)
        assert exc.value.status_code == 409
        assert exc.value.detail == _expected_no_driver_detail()


@pytest.mark.asyncio
async def test_run_driver_action_refuses_before_create_execution_run(monkeypatch):
    """run_driver_action's very first line is the gate: create_execution_run
    (and therefore load_driver) must never even be called on a refusal."""
    create_run_spy = AsyncMock(side_effect=AssertionError("create_execution_run must not run"))
    load_driver_spy = AsyncMock(side_effect=AssertionError("load_driver must not run"))
    monkeypatch.setattr(ex_service, "create_execution_run", create_run_spy)
    monkeypatch.setattr(ex_service, "load_driver", load_driver_spy)

    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await run_driver_action(
                db, _device_data("Layer 3 Switch"), _template_data(), "configure", uuid.uuid4()
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "driver_cannot_configure"
    create_run_spy.assert_not_awaited()
    load_driver_spy.assert_not_awaited()
    assert await _run_count() == 0
