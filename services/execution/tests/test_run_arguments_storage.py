"""Driver arguments: off the child's command line, stored masked, recovered on retry.

- CFG-SBX-1: the keyword arguments reach the sandbox child through a temp file;
  the child's argv carries only the file's path.
- CFG-EXEC-8: a run stores its keyword arguments masked
  (herd_common.config_redaction) and records whether anything was masked, plus
  the config version the configuration came from when the caller names one.
- CFG-RUN-7: a retry of a run whose stored copy is masked reads the configuration
  back from that config version, accepts it only when masking it gives the stored
  copy, and is refused with a pinned 409 or 503 otherwise.
"""

import json
import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.database import Base, get_db
from app.main import app
from app.models.execution_run import ExecutionRun
from app.routers import executions as ex_router
from app.routers.executions import (
    RETRY_CONFIG_NOT_RECOVERED_DETAIL,
    RETRY_CONFIG_UNAVAILABLE_DETAIL,
    RETRY_NO_CONFIG_SOURCE_DETAIL,
    _require_internal_token,
    get_current_user_payload,
    require_admin,
)
from app.services import execution_service as ex_service
from app.services.driver_sandbox import execute_driver_method
from app.services.execution_service import create_execution_run
from herd_common.config_redaction import redact_config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_RealAsyncClient = httpx.AsyncClient

ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}

FRR_CONFIG = {
    "commands": [
        "router bgp 65000",
        " neighbor 192.0.2.1 remote-as 65001",
        " neighbor 192.0.2.1 password BgpS3cret",
        "snmp-server community Comm7nity RO",
    ]
}
SECRET_VALUES = ("BgpS3cret", "Comm7nity")

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db():
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


def _mock_pipeline(monkeypatch) -> MagicMock:
    device = {
        "id": DEVICE_ID,
        "driver_id": DRIVER_ID,
        "driver_sha256": "sha",
        "driver_filename": "driver.zip",
        "connection_type": "Management",
        "template_id": TEMPLATE_ID,
    }
    monkeypatch.setattr(ex_router, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        ex_router, "fetch_template", AsyncMock(return_value={"id": TEMPLATE_ID, "sections": []})
    )
    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_config_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(ex_service, "validate_device_config", lambda *a, **k: None)
    monkeypatch.setattr(ex_service, "validate_device_config_with_schema", lambda *a, **k: None)
    sandbox = MagicMock(return_value={"success": True, "output": None, "duration_ms": 1})
    monkeypatch.setattr(ex_service, "execute_driver_method", sandbox)
    return sandbox


async def _stored_run(run_id: str) -> ExecutionRun:
    async with TestSessionLocal() as session:
        return await session.get(ExecutionRun, uuid.UUID(run_id))


# --- CFG-SBX-1: arguments never on the child's command line ---

ARGV_DRIVER = """
import sys

class Driver:
    def __init__(self, context):
        self.context = context

    def configure(self, commands):
        return {"success": True, "argv": sys.argv, "commands": commands}
"""


def test_driver_arguments_never_on_the_child_command_line():
    driver_dir = tempfile.mkdtemp(prefix="herd_test_driver_")
    with open(os.path.join(driver_dir, "driver.py"), "w") as f:
        f.write(ARGV_DRIVER)

    result = execute_driver_method(
        driver_dir, "configure", {"HERD_device_name": "r1"}, method_kwargs=FRR_CONFIG
    )

    assert result["success"] is True, result
    output = result["output"]
    # The driver received every argument, credentials included...
    assert output["commands"] == FRR_CONFIG["commands"]
    # ...and none of the argument text was on the child's argv.
    argv_text = " ".join(output["argv"])
    for value in (*SECRET_VALUES, "router bgp", "commands"):
        assert value not in argv_text
    # argv ends in a file path, and that file is gone once the call returns.
    kwargs_path = output["argv"][-1]
    assert os.path.basename(kwargs_path).startswith("herd_kw_")
    assert not os.path.exists(kwargs_path)


def test_driver_call_without_arguments_passes_no_arguments_file():
    driver_dir = tempfile.mkdtemp(prefix="herd_test_driver_")
    with open(os.path.join(driver_dir, "driver.py"), "w") as f:
        f.write(
            "import sys\n"
            "class Driver:\n"
            "    def __init__(self, context):\n"
            "        pass\n"
            "    def status(self):\n"
            "        return {'argv': sys.argv}\n"
        )
    result = execute_driver_method(driver_dir, "status", {})
    assert result["success"] is True, result
    assert len(result["output"]["argv"]) == 4


# --- CFG-EXEC-8: stored masked ---


@pytest.mark.asyncio
async def test_configure_run_stores_no_credential_value_from_an_frr_config(
    admin_client, monkeypatch
):
    sandbox = _mock_pipeline(monkeypatch)
    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": FRR_CONFIG,
            "config_version_id": VERSION_ID,
        },
    )
    assert resp.status_code == 201
    # The driver got the configuration as sent.
    assert sandbox.call_args.kwargs["method_kwargs"] == FRR_CONFIG
    # Neither the answer nor the stored row carries a credential value.
    stored = (await _stored_run(resp.json()["id"])).input_params
    for text in (json.dumps(resp.json()), json.dumps(stored)):
        for value in SECRET_VALUES:
            assert value not in text
    assert stored["method_kwargs"] == redact_config(FRR_CONFIG)[0]
    assert stored["method_kwargs_redacted"] is True
    assert stored["config_version_id"] == VERSION_ID


@pytest.mark.asyncio
async def test_run_without_credentials_stores_its_arguments_as_sent(admin_client, monkeypatch):
    _mock_pipeline(monkeypatch)
    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": {"hostname": "r1"},
        },
    )
    assert resp.status_code == 201
    stored = (await _stored_run(resp.json()["id"])).input_params
    assert stored["method_kwargs"] == {"hostname": "r1"}
    assert stored["method_kwargs_redacted"] is False
    assert "config_version_id" not in stored


@pytest.mark.asyncio
async def test_internal_execute_records_the_config_version(admin_client, monkeypatch):
    _mock_pipeline(monkeypatch)
    resp = await admin_client.post(
        "/execute/internal",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": FRR_CONFIG,
            "config_version_id": VERSION_ID,
        },
    )
    assert resp.status_code == 201
    stored = (await _stored_run(resp.json()["id"])).input_params
    assert stored["config_version_id"] == VERSION_ID
    assert stored["method_kwargs_redacted"] is True


@pytest.mark.asyncio
async def test_create_execution_run_masks_any_action_arguments():
    async with TestSessionLocal() as session:
        run = await create_execution_run(
            session,
            device_id=uuid.uuid4(),
            driver_id=uuid.uuid4(),
            driver_sha256="sha",
            action="create_instance",
            user_id=uuid.uuid4(),
            input_params={},
            method_kwargs={"name": "vm1", "admin_password": "Pw1"},
        )
    assert run.input_params["method_kwargs"] == {"name": "vm1", "admin_password": "[redacted]"}
    assert run.input_params["method_kwargs_redacted"] is True


# --- CFG-RUN-7: retry recovers the configuration ---


async def _seed_failed_configure_run(input_params: dict) -> uuid.UUID:
    async with TestSessionLocal() as session:
        run = ExecutionRun(
            device_id=uuid.UUID(DEVICE_ID),
            driver_id=uuid.UUID(DRIVER_ID),
            driver_sha256="sha",
            action="configure",
            user_id=uuid.UUID(USER_ID),
            status="FAILED",
            input_params=input_params,
        )
        session.add(run)
        await session.commit()
        return run.id


def _masked_params(**extra) -> dict:
    params = {
        "method_kwargs": redact_config(FRR_CONFIG)[0],
        "method_kwargs_redacted": True,
        "dry_run": False,
    }
    params.update(extra)
    return params


def _inventory(handler, seen: list | None = None):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(wrapped), **kwargs)

    return factory


def _answers_config(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": VERSION_ID, "config": config})

    return handler


@pytest.mark.asyncio
async def test_retry_reads_a_masked_configuration_back_from_its_config_version(
    admin_client, monkeypatch
):
    run_id = await _seed_failed_configure_run(_masked_params(config_version_id=VERSION_ID))
    sandbox = _mock_pipeline(monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        ex_router.httpx, "AsyncClient", _inventory(_answers_config(FRR_CONFIG), seen)
    )

    resp = await admin_client.post(
        f"/runs/{run_id}/retry", headers={"Authorization": "Bearer admin-token"}
    )

    assert resp.status_code == 200
    assert sandbox.call_args.kwargs["method_kwargs"] == FRR_CONFIG
    # Read with the admin's own token, under the run's device.
    assert len(seen) == 1
    assert seen[0].url.path == f"/devices/{DEVICE_ID}/config-versions/{VERSION_ID}"
    assert seen[0].headers["Authorization"] == "Bearer admin-token"
    # The new run is stored masked and keeps the reference for a later retry.
    new_params = resp.json()["input_params"]
    assert new_params["config_version_id"] == VERSION_ID
    assert new_params["method_kwargs_redacted"] is True
    for value in SECRET_VALUES:
        assert value not in json.dumps(new_params)


@pytest.mark.asyncio
async def test_retry_of_a_masked_run_without_a_config_version_is_refused(admin_client, monkeypatch):
    run_id = await _seed_failed_configure_run(_masked_params())
    sandbox = _mock_pipeline(monkeypatch)
    resp = await admin_client.post(
        f"/runs/{run_id}/retry", headers={"Authorization": "Bearer admin-token"}
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": RETRY_NO_CONFIG_SOURCE_DETAIL}
    sandbox.assert_not_called()
    ex_router.fetch_device.assert_not_awaited()


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": "Config version not found"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        _not_found,
        _answers_config({"commands": ["router bgp 65000"]}),
        _answers_config(
            {"commands": [line.replace("BgpS3cret", "Other") for line in FRR_CONFIG["commands"]]}
            | {"extra": "x"}
        ),
    ],
    ids=["version_not_found", "different_config", "extra_key"],
)
async def test_retry_refuses_a_version_that_does_not_match_the_run(
    admin_client, monkeypatch, handler
):
    run_id = await _seed_failed_configure_run(_masked_params(config_version_id=VERSION_ID))
    sandbox = _mock_pipeline(monkeypatch)
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _inventory(handler))
    resp = await admin_client.post(
        f"/runs/{run_id}/retry", headers={"Authorization": "Bearer admin-token"}
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": RETRY_CONFIG_NOT_RECOVERED_DETAIL}
    sandbox.assert_not_called()


def _transport_failure(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("inventory down")


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"detail": "boom"})


def _not_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [_transport_failure, _server_error, _not_json, _answers_config(["not", "an", "object"])],
    ids=["transport", "non_200", "not_json", "config_not_an_object"],
)
async def test_retry_fails_closed_when_the_config_version_cannot_be_read(
    admin_client, monkeypatch, handler
):
    run_id = await _seed_failed_configure_run(_masked_params(config_version_id=VERSION_ID))
    sandbox = _mock_pipeline(monkeypatch)
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", _inventory(handler))
    resp = await admin_client.post(
        f"/runs/{run_id}/retry", headers={"Authorization": "Bearer admin-token"}
    )
    assert resp.status_code == 503
    assert resp.json() == {"detail": RETRY_CONFIG_UNAVAILABLE_DETAIL}
    sandbox.assert_not_called()


@pytest.mark.asyncio
async def test_retry_of_an_unmasked_run_reuses_its_stored_arguments(admin_client, monkeypatch):
    run_id = await _seed_failed_configure_run(
        {"method_kwargs": {"hostname": "r1"}, "method_kwargs_redacted": False, "dry_run": False}
    )
    sandbox = _mock_pipeline(monkeypatch)
    lookup = MagicMock(side_effect=AssertionError("no inventory read for an unmasked run"))
    monkeypatch.setattr(ex_router.httpx, "AsyncClient", lookup)
    resp = await admin_client.post(f"/runs/{run_id}/retry")
    assert resp.status_code == 200
    assert sandbox.call_args.kwargs["method_kwargs"] == {"hostname": "r1"}
    lookup.assert_not_called()
