"""Targeted tests for app.routers.executions helpers and endpoints.

The goal is to exercise the helper functions (fetch_device, fetch_template,
run_driver_action) and the /device-check, /execute, /runs/{id}/retry paths
that aren't covered by the existing CRUD tests.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
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
from app.services.execution_service import (
    fetch_device,
    fetch_template,
    run_driver_action,
)
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())

ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

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
    app.dependency_overrides[get_current_user_payload] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[require_admin] = lambda: ADMIN_PAYLOAD
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _fake_device_data() -> dict:
    return {
        "id": DEVICE_ID,
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Management",
        "template_id": TEMPLATE_ID,
    }


def _fake_template_data() -> dict:
    return {"id": TEMPLATE_ID, "password_keys": []}


# --- Internal token helper ---


def test_require_internal_token_rejects_missing(monkeypatch):
    monkeypatch.setattr(ex_router.settings, "internal_api_token", "secret")
    with pytest.raises(HTTPException) as exc:
        _require_internal_token(x_internal_token=None)
    assert exc.value.status_code == 403


def test_require_internal_token_accepts_match(monkeypatch):
    monkeypatch.setattr(ex_router.settings, "internal_api_token", "secret")
    assert _require_internal_token(x_internal_token="secret") is None


def test_require_internal_token_errors_when_not_configured(monkeypatch):
    monkeypatch.setattr(ex_router.settings, "internal_api_token", "")
    with pytest.raises(HTTPException) as exc:
        _require_internal_token(x_internal_token="anything")
    assert exc.value.status_code == 500


# --- fetch_device / fetch_template ---


class _FakeHttpxClient:
    """Context manager replacement for httpx.AsyncClient that returns a preset response."""

    def __init__(self, response=None, exc: Exception | None = None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._response


def _fake_response(status_code: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    if status_code >= 400 and status_code != 404:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=None, response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.mark.asyncio
async def test_fetch_device_returns_payload(monkeypatch):
    payload = _fake_device_data()

    def _client_factory():
        return _FakeHttpxClient(response=_fake_response(200, payload))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    result = await fetch_device(uuid.UUID(DEVICE_ID))
    assert result == payload


@pytest.mark.asyncio
async def test_fetch_device_404_raises_404(monkeypatch):
    def _client_factory():
        return _FakeHttpxClient(response=_fake_response(404))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    with pytest.raises(HTTPException) as exc:
        await fetch_device(uuid.UUID(DEVICE_ID))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fetch_device_other_error_raises_502(monkeypatch):
    def _client_factory():
        return _FakeHttpxClient(exc=RuntimeError("boom"))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    with pytest.raises(HTTPException) as exc:
        await fetch_device(uuid.UUID(DEVICE_ID))
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_fetch_template_404_raises_404(monkeypatch):
    def _client_factory():
        return _FakeHttpxClient(response=_fake_response(404))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    with pytest.raises(HTTPException) as exc:
        await fetch_template(TEMPLATE_ID)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fetch_template_returns_payload(monkeypatch):
    payload = _fake_template_data()

    def _client_factory():
        return _FakeHttpxClient(response=_fake_response(200, payload))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    assert await fetch_template(TEMPLATE_ID) == payload


@pytest.mark.asyncio
async def test_fetch_template_other_error_raises_502(monkeypatch):
    def _client_factory():
        return _FakeHttpxClient(exc=RuntimeError("network"))

    monkeypatch.setattr(httpx, "AsyncClient", lambda: _client_factory())
    with pytest.raises(HTTPException) as exc:
        await fetch_template(TEMPLATE_ID)
    assert exc.value.status_code == 502


# --- run_driver_action: success and failure paths ---


@pytest.mark.asyncio
async def test_run_driver_action_success(monkeypatch):
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": {"state": "ok"}, "duration_ms": 12}),
    )

    async with TestSessionLocal() as session:
        run = await run_driver_action(
            session,
            _fake_device_data(),
            _fake_template_data(),
            "status",
            uuid.UUID(USER_ID),
        )
    assert isinstance(run, ExecutionRun)
    assert run.status == "SUCCESS"
    assert run.duration_ms == 12


@pytest.mark.asyncio
async def test_run_driver_action_driver_load_failure(monkeypatch):
    async def _raise(*args, **kwargs):
        raise ValueError("missing driver")

    monkeypatch.setattr(ex_service, "load_driver", _raise)

    async with TestSessionLocal() as session:
        run = await run_driver_action(
            session,
            _fake_device_data(),
            _fake_template_data(),
            "status",
            uuid.UUID(USER_ID),
        )
    assert run.status == "FAILED"
    assert "missing driver" in run.error


@pytest.mark.asyncio
async def test_run_driver_action_execution_timeout(monkeypatch):
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(
            return_value={
                "success": False,
                "error": "driver method timed out",
                "duration_ms": 30000,
            }
        ),
    )

    async with TestSessionLocal() as session:
        run = await run_driver_action(
            session,
            _fake_device_data(),
            _fake_template_data(),
            "status",
            uuid.UUID(USER_ID),
        )
    assert run.status == "TIMEOUT"


@pytest.mark.asyncio
async def test_run_driver_action_execution_failure(monkeypatch):
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": False, "error": "boom", "duration_ms": 5}),
    )

    async with TestSessionLocal() as session:
        run = await run_driver_action(
            session,
            _fake_device_data(),
            _fake_template_data(),
            "status",
            uuid.UUID(USER_ID),
        )
    assert run.status == "FAILED"
    assert run.error == "boom"


# --- Endpoint tests: /device-check, /execute, /runs/{id}/retry ---


@pytest.mark.asyncio
async def test_device_check_success(admin_client, monkeypatch):
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": "up", "duration_ms": 5}),
    )

    resp = await admin_client.post(
        "/device-check", json={"device_id": DEVICE_ID, "user_id": USER_ID}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_device_check_login_failure_short_circuits(admin_client, monkeypatch):
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": False, "error": "login denied", "duration_ms": 2}),
    )

    resp = await admin_client.post(
        "/device-check", json={"device_id": DEVICE_ID, "user_id": USER_ID}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "FAILED"
    assert "login denied" in data["error"]


@pytest.mark.asyncio
async def test_manual_execute_success(admin_client, monkeypatch):
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 3}),
    )

    resp = await admin_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "status", "user_id": USER_ID},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_manual_execute_overrides_user_id_with_jwt_subject(admin_client, monkeypatch):
    """Audit attribution must use the JWT subject, not body.user_id.

    Regression: a caller could previously frame another user in the audit log
    by setting body.user_id to that user's UUID. With the fix, body.user_id is
    ignored for the JWT-protected /execute path and the JWT subject is used.
    """
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    framed_victim = str(uuid.uuid4())
    resp = await admin_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "status", "user_id": framed_victim},
    )
    assert resp.status_code == 201

    # Inspect the recorded run: user_id must be the admin's JWT sub, not the spoofed value.
    run_id = uuid.UUID(resp.json()["id"])
    async with TestSessionLocal() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert str(run.user_id) == ADMIN_ID
        assert str(run.user_id) != framed_victim


@pytest.mark.asyncio
async def test_manual_execute_validates_configure_kwargs(admin_client, monkeypatch):
    """For action='configure', method_kwargs must pass the inventory schema.

    Regression: arbitrary kwargs (including ones that could exfiltrate secrets
    via the driver) used to be accepted. Now the same allowlist that gates the
    inventory write path also gates execution.
    """
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": ADMIN_ID,
            "method_kwargs": {"arbitrary_secret_field": "leak-me"},
        },
    )
    assert resp.status_code == 422
    assert "arbitrary_secret_field" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_manual_execute_skips_validation_for_non_configure_actions(admin_client, monkeypatch):
    """Non-configure actions (status, etc.) are not config writes; no allowlist enforced."""
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "status",
            "user_id": ADMIN_ID,
            "method_kwargs": {"anything_goes": True},
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_internal_execute_validates_configure_kwargs(admin_client, monkeypatch):
    """The kwargs allowlist applies to /execute/internal too (defense-in-depth)."""
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    resp = await admin_client.post(
        "/execute/internal",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": {"definitely_not_in_schema": 1},
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_retry_rejects_successful_run(admin_client, monkeypatch):
    # Seed a SUCCESS run; retry should 400.
    async with TestSessionLocal() as session:
        run = ExecutionRun(
            device_id=uuid.UUID(DEVICE_ID),
            driver_id=uuid.UUID(DRIVER_ID),
            driver_sha256="sha",
            action="status",
            user_id=uuid.UUID(USER_ID),
            status="SUCCESS",
            input_params={},
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    resp = await admin_client.post(f"/runs/{run_id}/retry")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_retry_failed_run_rebuilds_and_runs(admin_client, monkeypatch):
    # Seed a FAILED run and mock the execution pipeline.
    async with TestSessionLocal() as session:
        run = ExecutionRun(
            device_id=uuid.UUID(DEVICE_ID),
            driver_id=uuid.UUID(DRIVER_ID),
            driver_sha256="sha",
            action="status",
            user_id=uuid.UUID(USER_ID),
            status="FAILED",
            input_params={"method_kwargs": {"foo": "bar"}},
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    resp = await admin_client.post(f"/runs/{run_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


# ---- ACL carve-out for /execute (roadmap #9 iter 2 piece A) ---------------

USER_PAYLOAD = {"sub": USER_ID, "username": "viewer", "role": "user"}


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_current_user_payload] = lambda: USER_PAYLOAD
    app.dependency_overrides[_require_internal_token] = lambda: None
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_execute_non_admin_status_action_forbidden(user_client):
    """Non-admins still cannot run non-configure actions, regardless of ACL."""
    resp = await user_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "status", "user_id": USER_ID},
    )
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_execute_non_admin_configure_without_grant_forbidden(user_client, monkeypatch):
    """Non-admin configure with no ACL manage grant is rejected."""
    monkeypatch.setattr(ex_router, "_user_has_acl_manage", AsyncMock(return_value=False))
    resp = await user_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "configure", "user_id": USER_ID},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert "manage grant" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_execute_non_admin_configure_with_grant_succeeds(user_client, monkeypatch):
    """Non-admin configure with ACL manage grant proceeds and returns a run."""
    monkeypatch.setattr(ex_router, "_user_has_acl_manage", AsyncMock(return_value=True))
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )

    resp = await user_client.post(
        "/execute",
        json={"device_id": DEVICE_ID, "action": "configure", "user_id": USER_ID},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_internal_execute_uses_internal_token(admin_client, monkeypatch):
    """The /execute/internal endpoint runs under _require_internal_token only.

    The admin_client fixture overrides _require_internal_token to a no-op, so
    this test verifies the endpoint at least invokes the driver pipeline. A
    separate integration test would exercise the real token gate.
    """
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )
    resp = await admin_client.post(
        "/execute/internal",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": {"vlan": 100},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden_action", ["status", "delete", "reboot", "drain", ""])
async def test_internal_execute_rejects_non_configure_action(
    admin_client, monkeypatch, forbidden_action
):
    """Carve-out: /execute/internal must only run action='configure'.

    Without this lock, any caller with INTERNAL_API_TOKEN can run arbitrary
    driver methods (the same surface the JWT-protected /execute path gates
    behind admin RBAC). The driver pipeline mocks here ensure that if the
    422 guard ever regressed, the test would surface a 201 instead.
    """
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=_fake_device_data()))
    monkeypatch.setattr(ex_router, "fetch_template", AsyncMock(return_value=_fake_template_data()))
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(
        ex_service,
        "execute_driver_method",
        MagicMock(return_value={"success": True, "output": None, "duration_ms": 1}),
    )
    resp = await admin_client.post(
        "/execute/internal",
        json={
            "device_id": DEVICE_ID,
            "action": forbidden_action,
            "user_id": USER_ID,
            "method_kwargs": {},
        },
    )
    assert resp.status_code == 422
    assert "configure" in resp.json()["detail"]


# --- GET /runs owner-scoped access (iter 2 of AI assistant prereq) ---


@pytest.mark.asyncio
async def test_list_runs_admin_unchanged(admin_client):
    """Admin can list /runs without a reservation_id filter."""
    resp = await admin_client.get("/runs")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_runs_non_admin_without_reservation_id_403(user_client):
    """Non-admin caller must supply a reservation_id; unscoped /runs stays admin-only."""
    resp = await user_client.get("/runs")
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_runs_non_owner_with_reservation_id_403(user_client, monkeypatch):
    """Non-admin caller who does not own the reservation gets 403."""
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=False))
    resp = await user_client.get(
        f"/runs?reservation_id={uuid.uuid4()}",
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 403
    assert "not owned" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_list_runs_owner_with_reservation_id_allowed(user_client, monkeypatch):
    """Owner of the reservation can list /runs filtered to that reservation."""
    monkeypatch.setattr(ex_router, "_user_owns_reservation", AsyncMock(return_value=True))
    resp = await user_client.get(
        f"/runs?reservation_id={uuid.uuid4()}",
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_user_owns_reservation_returns_false_without_authorization():
    """No Authorization header to forward = cannot prove ownership."""
    result = await ex_router._user_owns_reservation(uuid.uuid4(), None)
    assert result is False


@pytest.mark.asyncio
async def test_user_owns_reservation_returns_false_on_httpx_error(monkeypatch):
    """Network errors close-by-default."""

    class _FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lambda *a, **kw: _FailingClient())
    result = await ex_router._user_owns_reservation(uuid.uuid4(), "Bearer t")
    assert result is False
