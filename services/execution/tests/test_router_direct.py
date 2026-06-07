"""Direct-call coverage for app.routers.executions and app.routers.health.

The ASGI-driven tests in test_router_endpoints.py and test_health_endpoints.py
assert behavior through httpx, but coverage's tracer does not credit endpoint
bodies executed inside the ASGI event-loop task. These tests call the endpoint
and helper coroutines directly with a real in-memory SQLite session and stubbed
service helpers, so the real logic (and its branches) is exercised and counted.

Patterns mirror the existing suite: in-memory aiosqlite engine, AsyncMock for
the service-layer functions, and a _FakeClient context manager for httpx.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.database import Base
from app.models.device_health_status import DeviceHealthStatus
from app.models.execution_run import ExecutionRun
from app.routers import executions as ex_router
from app.routers import health as health_router
from app.schemas.execution import DeviceCheckRequest, ManualExecuteRequest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

DEVICE_ID = uuid.uuid4()
TEMPLATE_ID = uuid.uuid4()
DRIVER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


def _device_data() -> dict:
    return {
        "id": str(DEVICE_ID),
        "driver_id": str(DRIVER_ID),
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Management",
        "template_id": str(TEMPLATE_ID),
        "field_data": {},
        "name": "dev",
    }


def _template_data() -> dict:
    return {"id": str(TEMPLATE_ID), "sections": []}


def _make_run(status: str = "SUCCESS", **kw) -> ExecutionRun:
    return ExecutionRun(
        id=kw.get("id", uuid.uuid4()),
        device_id=DEVICE_ID,
        driver_id=DRIVER_ID,
        driver_sha256="sha",
        action=kw.get("action", "status"),
        status=status,
        user_id=USER_ID,
        input_params={},
        output=kw.get("output"),
        error=kw.get("error"),
        reservation_id=kw.get("reservation_id"),
    )


# --- _user_has_acl_manage (executions router, lines 58-78) ---


class _FakeClient:
    def __init__(self, resp=None, exc: Exception | None = None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _resp(status_code: int, payload=None, raise_json=False):
    r = MagicMock()
    r.status_code = status_code
    if raise_json:
        r.json.side_effect = ValueError("bad json")
    else:
        r.json.return_value = payload or {}
    return r


@pytest.mark.asyncio
async def test_acl_manage_false_without_authorization():
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, None) is False


@pytest.mark.asyncio
async def test_acl_manage_false_on_httpx_error(monkeypatch):
    monkeypatch.setattr(
        ex_router.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(exc=httpx.ConnectError("down")),
    )
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, "Bearer t") is False


@pytest.mark.asyncio
async def test_acl_manage_false_on_non_200(monkeypatch):
    monkeypatch.setattr(
        ex_router.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(403)),
    )
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, "Bearer t") is False


@pytest.mark.asyncio
async def test_acl_manage_false_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        ex_router.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(200, raise_json=True)),
    )
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, "Bearer t") is False


@pytest.mark.asyncio
async def test_acl_manage_true_when_allowed(monkeypatch):
    monkeypatch.setattr(
        ex_router.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(200, {"allowed": True})),
    )
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, "Bearer t") is True


@pytest.mark.asyncio
async def test_acl_manage_false_when_not_allowed(monkeypatch):
    monkeypatch.setattr(
        ex_router.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeClient(resp=_resp(200, {"allowed": False})),
    )
    assert await ex_router._user_has_acl_manage(str(USER_ID), DEVICE_ID, "Bearer t") is False


# --- get_run (lines 174-177) ---


@pytest.mark.asyncio
async def test_get_run_404_when_missing(db):
    with pytest.raises(HTTPException) as exc:
        await ex_router.get_run(uuid.uuid4(), {"role": "admin"}, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_run_returns_persisted(db):
    run = _make_run()
    db.add(run)
    await db.commit()
    got = await ex_router.get_run(run.id, {"role": "admin"}, db)
    assert got.id == run.id


# --- list_run_commands (lines 195-198) ---


@pytest.mark.asyncio
async def test_list_run_commands_404_when_run_missing(db):
    with pytest.raises(HTTPException) as exc:
        await ex_router.list_run_commands(uuid.uuid4(), {"role": "admin"}, None, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_run_commands_admin_returns_empty(db):
    run = _make_run()
    db.add(run)
    await db.commit()
    rows = await ex_router.list_run_commands(run.id, {"role": "admin"}, None, db)
    assert rows == []


# --- _authorize_run_read (lines 130-135) via list_run_commands non-admin ---


@pytest.mark.asyncio
async def test_list_run_commands_non_admin_no_reservation_403(db):
    run = _make_run()  # reservation_id is None
    db.add(run)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await ex_router.list_run_commands(run.id, {"role": "user"}, None, db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_run_commands_non_admin_non_owner_403(db, monkeypatch):
    reservation_id = uuid.uuid4()
    run = _make_run(reservation_id=reservation_id)
    db.add(run)
    await db.commit()
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await ex_router.list_run_commands(run.id, {"role": "user"}, "Bearer t", db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_run_commands_non_admin_owner_allowed(db, monkeypatch):
    reservation_id = uuid.uuid4()
    run = _make_run(reservation_id=reservation_id)
    db.add(run)
    await db.commit()
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=True))
    rows = await ex_router.list_run_commands(run.id, {"role": "user"}, "Bearer t", db)
    assert rows == []


# --- device_check status path (lines 213-225) ---


@pytest.mark.asyncio
async def test_device_check_status_success_path(db, monkeypatch):
    """login SUCCESS then status SUCCESS then logout: returns the status run."""
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))

    status_run = _make_run(status="SUCCESS", output="up", action="status")
    runs = [_make_run(status="SUCCESS", action="login"), status_run, _make_run(action="logout")]
    monkeypatch.setattr(ex_router, "run_driver_action", AsyncMock(side_effect=runs))

    body = DeviceCheckRequest(device_id=DEVICE_ID, user_id=USER_ID)
    resp = await ex_router.device_check(body, None, db)
    assert resp.status == "SUCCESS"
    assert resp.output == "up"
    assert resp.run_id == status_run.id


@pytest.mark.asyncio
async def test_device_check_login_failure_short_circuits(db, monkeypatch):
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))

    login_run = _make_run(status="FAILED", error="denied", action="login")
    monkeypatch.setattr(ex_router, "run_driver_action", AsyncMock(return_value=login_run))

    body = DeviceCheckRequest(device_id=DEVICE_ID, user_id=USER_ID)
    resp = await ex_router.device_check(body, None, db)
    assert resp.status == "FAILED"
    assert resp.error == "denied"
    assert resp.run_id == login_run.id


# --- retry_run (lines 334-356) ---


@pytest.mark.asyncio
async def test_retry_run_404_when_missing(db):
    with pytest.raises(HTTPException) as exc:
        await ex_router.retry_run(uuid.uuid4(), {"role": "admin"}, db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_retry_run_rejects_non_failed(db):
    run = _make_run(status="SUCCESS")
    db.add(run)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await ex_router.retry_run(run.id, {"role": "admin"}, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_retry_run_failed_rebuilds(db, monkeypatch):
    original = _make_run(status="FAILED")
    original.input_params = {"method_kwargs": {"foo": "bar"}}
    db.add(original)
    await db.commit()

    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    new_run = _make_run(status="SUCCESS")
    monkeypatch.setattr(ex_router, "run_driver_action", AsyncMock(return_value=new_run))

    result = await ex_router.retry_run(original.id, {"role": "admin"}, db)
    assert result.status == "SUCCESS"
    # method_kwargs recovered from the original run's input_params.
    _, kwargs = ex_router.run_driver_action.call_args
    assert kwargs["method_kwargs"] == {"foo": "bar"}


# --- list_runs return path (line 164) ---


@pytest.mark.asyncio
async def test_list_runs_returns_paginated(db):
    run = _make_run()
    db.add(run)
    await db.commit()
    resp = await ex_router.list_runs(
        device_id=None,
        reservation_id=None,
        status_filter=None,
        created_after=None,
        created_before=None,
        skip=0,
        limit=50,
        _={"role": "admin"},
        db=db,
    )
    assert resp.total == 1
    assert resp.skip == 0
    assert resp.limit == 50
    assert len(resp.items) == 1


# --- manual_execute admin return path (line 281) ---


@pytest.mark.asyncio
async def test_manual_execute_admin_returns_run(db, monkeypatch):
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_template_data()))
    new_run = _make_run(status="SUCCESS")
    monkeypatch.setattr(ex_router, "run_driver_action", AsyncMock(return_value=new_run))

    admin_id = uuid.uuid4()
    body = ManualExecuteRequest(device_id=DEVICE_ID, action="status", user_id=USER_ID)
    result = await ex_router.manual_execute(body, {"role": "admin", "sub": str(admin_id)}, None, db)
    assert result.status == "SUCCESS"
    # Audit attribution uses the JWT subject, not body.user_id.
    args, _ = ex_router.run_driver_action.call_args
    assert args[4] == admin_id


# --- _user_owns_reservation True path (line 96) ---


@pytest.mark.asyncio
async def test_user_owns_reservation_true_on_200(monkeypatch):
    class _OkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _resp(200)

    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lambda *a, **kw: _OkClient())
    assert await ex_router._user_owns_reservation(uuid.uuid4(), "Bearer t") is True


# --- health router endpoints (lines 46-55, 67-91) ---


@pytest.mark.asyncio
async def test_get_device_health_synthetic_unknown(db):
    device_id = uuid.uuid4()
    resp = await health_router.get_device_health(device_id, db, {"role": "user"})
    assert resp.last_status == "UNKNOWN"
    assert resp.device_id == device_id
    assert resp.last_polled_at is None
    assert resp.consecutive_failures == 0


@pytest.mark.asyncio
async def test_get_device_health_returns_persisted(db):
    device_id = uuid.uuid4()
    db.add(DeviceHealthStatus(device_id=device_id, last_status="DEGRADED", consecutive_failures=2))
    await db.commit()
    resp = await health_router.get_device_health(device_id, db, {"role": "user"})
    assert resp.last_status == "DEGRADED"
    assert resp.consecutive_failures == 2


@pytest.mark.asyncio
async def test_list_device_health_no_filter(db):
    for st in ("HEALTHY", "DEGRADED"):
        db.add(DeviceHealthStatus(device_id=uuid.uuid4(), last_status=st))
    await db.commit()
    resp = await health_router.list_device_health(0, 50, None, {"role": "admin"}, db)
    assert resp.total == 2


@pytest.mark.asyncio
async def test_list_device_health_status_filter(db):
    for st in ("HEALTHY", "HEALTHY", "DEGRADED"):
        db.add(DeviceHealthStatus(device_id=uuid.uuid4(), last_status=st))
    await db.commit()
    resp = await health_router.list_device_health(0, 50, "HEALTHY", {"role": "admin"}, db)
    assert resp.total == 2
    assert all(item.last_status == "HEALTHY" for item in resp.items)


@pytest.mark.asyncio
async def test_list_device_health_invalid_filter_422(db):
    db.add(DeviceHealthStatus(device_id=uuid.uuid4(), last_status="HEALTHY"))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await health_router.list_device_health(0, 50, "BOGUS", {"role": "admin"}, db)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_list_device_health_empty_known_filter_not_422(db):
    """An empty result for a VALID status (e.g. UNREACHABLE) returns total 0, not 422."""
    resp = await health_router.list_device_health(0, 50, "UNREACHABLE", {"role": "admin"}, db)
    assert resp.total == 0
    assert resp.items == []
