"""A refused `configure` never stores or answers a value from the config (issue #1457).

Execution validates a `configure` call's keyword arguments against the driver's
published schema (else the registry), and a refusal ends the run FAILED with the
validator's text and answers 422 with the same text (CFG-EXEC-10). That text
used to be jsonschema's own message, which quotes the failing part of the
config, so a credential in an FRR vtysh line reached the 422, the run's stored
`error` (beside the masked `input_params` CFG-EXEC-8 promises), the run list,
and through the assistant's `list_executions_for_reservation` tool the model
provider.

These tests go through the real `POST /execute` and `POST /execute/internal`
routes with the published schema read from `drivers/frr_mgmt/driver.py` as
shipped, and one registry schema, and check the 422 body, the stored row, the
run list (what the assistant tool forwards), and every log line for the canary.
Two canaries: one after a credential keyword (`password <canary>`), which the
second layer (the CFG-EXEC-8 masking of the stored error) would also catch, and
one under no keyword, which only the message builder keeps out.
"""

import importlib.util
import json
import logging
import uuid
from pathlib import Path
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
from fastapi import HTTPException
from herd_common.config_redaction import REDACTED_VALUE, redact_config
from herd_common.device_config import (
    LEGACY_REFUSAL_WITHHELD,
    ConfigValidationError,
    validate_device_config_with_schema,
)
from herd_common.logging import JSONFormatter
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

CANARY = "S3cr3t-Canary-9d1e"
ADMIN_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
ADMIN_PAYLOAD = {"sub": ADMIN_ID, "username": "admin", "role": "admin"}


def _frr_published_schema() -> dict:
    path = Path(__file__).resolve().parents[3] / "drivers" / "frr_mgmt" / "driver.py"
    spec = importlib.util.spec_from_file_location("frr_mgmt_schema_for_refusals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver.config_schema()


FRR_SCHEMA = _frr_published_schema()
# A driver that publishes a credential-named field with a pattern: the refused
# value sits under no keyword, so only the message builder keeps it out.
PASSWORD_FIELD_SCHEMA = {
    "type": "object",
    "properties": {"password": {"type": "string", "pattern": "^[a-z]+$"}},
    "additionalProperties": False,
}

# (case id, published schema or None for the registry, method_kwargs, the text after the prefix)
CASES = [
    (
        "frr-maxLength",
        FRR_SCHEMA,
        {"commands": [f"neighbor 192.0.2.1 password {CANARY} " + "x" * 500]},
        "at commands[0]: must be at most 512 characters long",
    ),
    (
        "frr-maxItems",
        FRR_SCHEMA,
        {"commands": [f"neighbor 192.0.2.1 password {CANARY}"] + ["!"] * 100},
        "at commands: must have at most 100 items",
    ),
    (
        "frr-type",
        FRR_SCHEMA,
        {"command": [f"enable password {CANARY}"]},
        "at command: must be of type string",
    ),
    (
        "published-pattern-no-keyword",
        PASSWORD_FIELD_SCHEMA,
        {"password": CANARY},
        "at ['password']: must match the schema's pattern",
    ),
    (
        "registry-maxLength-no-keyword",
        None,
        {"hostname": CANARY * 10},
        "at hostname: must be at most 128 characters long",
    ),
]

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


def _mock_pipeline(monkeypatch, schema: dict | None, name: str = "edge-1") -> MagicMock:
    """Mock everything around the validation; the validators stay real."""
    device = {
        "id": DEVICE_ID,
        "name": name,
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
    monkeypatch.setattr(ex_service, "get_driver_config_schema", AsyncMock(return_value=schema))
    sandbox = MagicMock(return_value={"success": True, "output": None, "duration_ms": 1})
    monkeypatch.setattr(ex_service, "execute_driver_method", sandbox)
    return sandbox


async def _only_run() -> ExecutionRun:
    async with TestSessionLocal() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(ExecutionRun))).scalars().all()
    assert len(rows) == 1
    return rows[0]


def _log_text(records: list[logging.LogRecord]) -> str:
    """Every captured line at DEBUG and up, as the service's formatter writes it.

    aiosqlite's DEBUG lines are left out: they print every bound SQL parameter,
    including the stored arguments (masked by CFG-EXEC-8, but a non-credential
    value is kept as sent), and aiosqlite is the test database driver only; a
    deployment runs asyncpg.
    """
    formatter = JSONFormatter("execution")
    return "\n".join(
        formatter.format(record) for record in records if not record.name.startswith("aiosqlite")
    )


@pytest.mark.parametrize("route", ["/execute", "/execute/internal"])
@pytest.mark.parametrize(
    ("schema", "method_kwargs", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
@pytest.mark.asyncio
async def test_refused_configure_never_carries_a_config_value(
    admin_client, monkeypatch, caplog, route, schema, method_kwargs, expected
):
    sandbox = _mock_pipeline(monkeypatch, schema)
    with caplog.at_level(logging.DEBUG):
        resp = await admin_client.post(
            route,
            json={
                "device_id": DEVICE_ID,
                "action": "configure",
                "user_id": USER_ID,
                "method_kwargs": method_kwargs,
            },
        )
        runs = await admin_client.get("/runs")

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail == f"device 'edge-1': config failed schema validation {expected}"
    # The driver never ran.
    sandbox.assert_not_called()

    run = await _only_run()
    assert run.status == "FAILED"
    # CFG-EXEC-10: the row stores the text the 422 answered.
    assert run.error == detail

    # The run list is what the assistant's list_executions_for_reservation tool
    # hands the model provider.
    assert runs.status_code == 200
    listed = runs.json()["items"]
    assert [item["error"] for item in listed] == [detail]

    for text in (resp.text, run.error, json.dumps(listed[0]["error"]), _log_text(caplog.records)):
        assert CANARY not in text

    # The stored arguments are CFG-EXEC-8's masked copy, unchanged by this fix:
    # a credential-shaped value is masked there too, so for those cases nothing
    # in the row or the run list holds the canary. A value that is not
    # credential-shaped (the over-long hostname) is stored as sent, by design.
    masked, credential_shaped = redact_config(method_kwargs)
    assert run.input_params["method_kwargs"] == masked
    if credential_shaped:
        assert CANARY not in json.dumps(run.input_params)
        assert CANARY not in runs.text


@pytest.mark.asyncio
async def test_a_refusal_text_that_holds_a_credential_line_is_masked_before_it_is_stored(
    db_session_and_patches,
):
    """The second layer: whatever the validator's reason, the stored error and the
    422 detail carry its CFG-EXEC-8 masked copy, and they are equal. The prefix,
    which holds the device's name, is left as the validator wrote it: masking it
    would cut the refusal off after `key`."""
    db, raised = db_session_and_patches
    with pytest.raises(HTTPException) as exc:
        await ex_service.run_driver_action(
            db,
            {
                "id": DEVICE_ID,
                "driver_id": DRIVER_ID,
                "driver_sha256": "sha",
                "driver_filename": "driver.zip",
                "connection_type": "Management",
                "field_data": {},
                "name": "core key switch",
            },
            {"sections": []},
            "configure",
            uuid.UUID(USER_ID),
            method_kwargs={"commands": ["anything"]},
        )
    assert exc.value.status_code == 422
    assert exc.value.__cause__ is None
    expected = (
        f"device 'core key switch': refused line neighbor 192.0.2.1 password {REDACTED_VALUE}"
    )
    assert exc.value.detail == expected
    run = await _only_run()
    assert run.error == expected
    assert CANARY not in run.error
    assert raised  # the patched validator really ran


@pytest.fixture
async def db_session_and_patches(monkeypatch):
    raised: list[bool] = []

    def _raise(*_args, **_kwargs):
        raised.append(True)
        raise ConfigValidationError(
            "device 'core key switch'", f"refused line neighbor 192.0.2.1 password {CANARY}"
        )

    monkeypatch.setattr(ex_service, "load_driver", AsyncMock(return_value="/tmp/driver"))
    monkeypatch.setattr(ex_service, "get_driver_config_schema", AsyncMock(return_value=None))
    monkeypatch.setattr(ex_service, "validate_device_config_with_schema", _raise)
    monkeypatch.setattr(ex_service, "execute_driver_method", MagicMock())
    async with TestSessionLocal() as session:
        yield session, raised


# --- A device name never eats the refusal (CFG-EXEC-10) ----------------------

DEVICE_NAMES = [
    "core key switch",
    "Secret Lab SW1",
    "lab-sw password 7",
    "edge md5",
    "KEY",
    "community string rack 4",
    "r1",
]


@pytest.mark.parametrize("name", DEVICE_NAMES)
@pytest.mark.asyncio
async def test_a_device_name_with_a_credential_keyword_keeps_the_whole_refusal(
    admin_client, monkeypatch, name
):
    """The stored error and the 422 are exactly the validator's text, the same
    text inventory answers for the same refusal
    (services/inventory/tests/test_device_configs.py pins inventory's side
    against the same herd_common call)."""
    _mock_pipeline(monkeypatch, FRR_SCHEMA, name=name)
    with pytest.raises(ConfigValidationError) as direct:
        validate_device_config_with_schema(
            "Management", {"command": ""}, schema=FRR_SCHEMA, role=name
        )
    expected = str(direct.value)
    assert expected.endswith(
        "config failed schema validation at command: must be at least 1 character long"
    )

    resp = await admin_client.post(
        "/execute",
        json={
            "device_id": DEVICE_ID,
            "action": "configure",
            "user_id": USER_ID,
            "method_kwargs": {"command": ""},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == expected
    assert (await _only_run()).error == expected
    runs = await admin_client.get("/runs")
    assert [item["error"] for item in runs.json()["items"]] == [expected]


# --- Refusals stored before issue #1457 are withheld on read ------------------

OLD_REFUSALS = [
    f"device 'edge-1': config failed schema validation: 'neighbor 192.0.2.1 password {CANARY} x'"
    " is too long",
    f"device: config failed schema validation: ['enable password {CANARY}'] is not of type "
    "'string'",
    f"device 'core key switch': config failed schema validation: '{CANARY}' does not match "
    "'^[a-z]+$'",
]
KEPT_ERRORS = [
    "device 'edge-1': config failed schema validation at commands[0]: must be at most 512 "
    "characters long",
    "driver raised RuntimeError",
    None,
]


async def _seed_runs(errors: list[str | None]) -> list[str]:
    ids = []
    async with TestSessionLocal() as session:
        for error in errors:
            run = ExecutionRun(
                device_id=uuid.UUID(DEVICE_ID),
                driver_id=uuid.UUID(DRIVER_ID),
                driver_sha256="sha",
                action="configure",
                status="FAILED",
                user_id=uuid.UUID(USER_ID),
                input_params={},
                error=error,
            )
            session.add(run)
            await session.flush()
            ids.append(str(run.id))
        await session.commit()
    return ids


@pytest.mark.asyncio
async def test_a_refusal_stored_before_the_fix_is_withheld_by_every_run_read(admin_client):
    old_ids = await _seed_runs(OLD_REFUSALS)
    kept_ids = await _seed_runs(KEPT_ERRORS)

    listed = await admin_client.get("/runs")
    assert listed.status_code == 200
    by_id = {item["id"]: item["error"] for item in listed.json()["items"]}
    details = {}
    for run_id in old_ids + kept_ids:
        detail = await admin_client.get(f"/runs/{run_id}")
        assert detail.status_code == 200
        details[run_id] = detail.json()["error"]

    prefixes = ["device 'edge-1'", "device", "device 'core key switch'"]
    for run_id, prefix in zip(old_ids, prefixes, strict=True):
        withheld = f"{prefix}: {LEGACY_REFUSAL_WITHHELD}"
        assert by_id[run_id] == withheld
        assert details[run_id] == withheld
    for run_id, error in zip(kept_ids, KEPT_ERRORS, strict=True):
        assert by_id[run_id] == error
        assert details[run_id] == error
    # The list is what the assistant's list_executions_for_reservation tool
    # forwards to the model provider.
    assert CANARY not in listed.text
    assert all(CANARY not in text for text in details.values() if text)

    # The rows themselves are unchanged: the mask is on read only.
    async with TestSessionLocal() as session:
        stored = [(await session.get(ExecutionRun, uuid.UUID(i))).error for i in old_ids]
    assert stored == OLD_REFUSALS
