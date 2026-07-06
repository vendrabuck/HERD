"""Tests for the dynamic-resource create/teardown paths (ADR 0004, issue #32).

Invariants under test:
- A recipe is a Hypervisor-type driver package; REQUIRED_METHODS validation
  rejects one missing a required method.
- The dynamic_instances ledger drives idempotency: a redelivery that finds an
  ACTIVE row with a device skips the create; teardown drives only from the
  ledger and is a no-op for DESTROYED rows.
- Create failures NAK with the row left CREATING; a missing config resource is a
  PermanentEventError that dead-letters and fires the failure callback.
- Teardown mirrors the L3 discipline: a driver-result failure ACKs and leaves
  the row ACTIVE (may-still-exist); a transient upstream error NAKs.
- Secret values reach the recipe only through the context file, never the child
  environment (the password_keys plumbing).
"""

import io
import json
import uuid
import zipfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.dynamic_instance import DynamicInstance
from app.models.execution_run import ExecutionRun
from app.services import nats_consumer
from app.services.driver_loader import (
    DriverPackageError,
    extract_driver_package,
    validate_driver,
)
from app.services.dynamic_instance_service import (
    get_by_request_id,
    insert_or_get_creating,
    list_teardown_candidates,
    mark_active,
    mark_destroyed,
    set_instance_ref,
)
from app.services.nats_consumer import (
    NATS_DLQ_SUBJECT,
    PermanentEventError,
    TransientUpstreamError,
    _build_recipe_context,
    _create_dynamic_device,
    _delete_dynamic_device,
    _execute_dynamic_teardown,
    _handle_provision_requested,
    _post_provision_result_best_effort,
    handle_reservation_event,
    process_reservation_message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _db_session_factory():
    class _Ctx:
        async def __aenter__(self):
            self._session = TestSessionLocal()
            return self._session

        async def __aexit__(self, *args):
            await self._session.close()

    def _get():
        return _Ctx()

    return _get


REQUEST_ID = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
HYPERVISOR_ID = str(uuid.uuid4())
DRIVER_ID = str(uuid.uuid4())
SECRET_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())
RES_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())

TEMPLATE_DATA = {
    "id": TEMPLATE_ID,
    "name": "Linux VM",
    "template_type": "dynamic",
    "driver_id": DRIVER_ID,
    "driver_sha256": "sha256abc",
    "driver_filename": "recipe.zip",
    "connection_type": "Hypervisor",
    "hypervisor_id": HYPERVISOR_ID,
    "sections": [
        {"name": "Instance", "fields": [{"key": "image", "type": "string", "default": "debian12"}]}
    ],
}

HYPERVISOR_DATA = {
    "id": HYPERVISOR_ID,
    "name": "pve-1",
    "endpoint": "https://pve.example:8006",
    "hypervisor_type": "proxmox",
    "secret_id": SECRET_ID,
    "enabled": True,
}

SECRET_DATA = {"username": "root", "password": "hunter2"}

LOGIN_OK = {"success": True, "output": {"ok": True}, "error": None, "duration_ms": 5}
CREATE_OK = {
    "success": True,
    "output": {"success": True, "instance_ref": "vm-100", "field_data": {"mgmt_ip": "10.0.0.9"}},
    "error": None,
    "duration_ms": 5,
}
CREATE_DRIVER_FAIL = {
    "success": True,
    "output": {"success": False},
    "error": "hypervisor rejected",
    "duration_ms": 5,
}
DESTROY_OK = {"success": True, "output": {"success": True}, "error": None, "duration_ms": 5}
DESTROY_DRIVER_FAIL = {
    "success": True,
    "output": {"success": False},
    "error": "still running",
    "duration_ms": 5,
}


def _recipe_execute(results_by_action):
    """execute_driver_method stub recording (action, method_kwargs, context)."""
    calls: list[tuple[str, dict, dict]] = []

    def _execute(driver_path, action, context, **kwargs):
        calls.append((action, kwargs.get("method_kwargs") or {}, dict(context)))
        return results_by_action.get(action, LOGIN_OK)

    return calls, _execute


def _create_patches(
    execute_fn,
    *,
    template=TEMPLATE_DATA,
    hypervisor=HYPERVISOR_DATA,
    secret=SECRET_DATA,
    device_return={"id": DEVICE_ID},
):
    """Patch the create/teardown flow's external seams (HTTP + sandbox)."""
    return [
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=template)),
        patch(
            "app.services.nats_consumer._fetch_hypervisor",
            new=AsyncMock(return_value=hypervisor),
        ),
        patch(
            "app.services.nats_consumer._fetch_secret_value",
            new=AsyncMock(return_value=secret),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/recipe")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute_fn),
        patch(
            "app.services.nats_consumer._create_dynamic_device",
            new=AsyncMock(return_value=device_return),
        ),
    ]


async def _rows():
    async with TestSessionLocal() as db:
        result = await db.execute(select(DynamicInstance))
        return list(result.scalars().all())


async def _runs():
    async with TestSessionLocal() as db:
        result = await db.execute(select(ExecutionRun).order_by(ExecutionRun.created_at))
        return list(result.scalars().all())


def _event(requests=None):
    return {
        "event": "reservation.provision_requested",
        "reservation_id": RES_ID,
        "user_id": USER_ID,
        "device_ids": [],
        "dynamic_requests": requests
        if requests is not None
        else [{"id": REQUEST_ID, "template_id": TEMPLATE_ID}],
        "event_id": str(uuid.uuid4()),
    }


# --- REQUIRED_METHODS Hypervisor validation ---------------------------------


def _make_zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", code)
    return buf.getvalue()


_VALID_RECIPE = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def create_instance(self):
        return {"success": True, "instance_ref": "vm-1", "field_data": {}}

    def destroy_instance(self, instance_ref):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""

_RECIPE_MISSING_DESTROY = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def create_instance(self):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""


def test_required_methods_registers_hypervisor():
    assert nats_consumer  # module import sanity
    from app.services.driver_loader import REQUIRED_METHODS

    assert REQUIRED_METHODS["Hypervisor"] == [
        "login",
        "logout",
        "create_instance",
        "destroy_instance",
        "status",
    ]


def test_valid_hypervisor_recipe_passes_validation(tmp_path):
    dest = tmp_path / "recipe"
    extract_driver_package(_make_zip(_VALID_RECIPE), "recipe.zip", dest)
    assert validate_driver(dest, "Hypervisor") == []


def test_recipe_missing_destroy_instance_fails_validation(tmp_path):
    dest = tmp_path / "recipe"
    extract_driver_package(_make_zip(_RECIPE_MISSING_DESTROY), "recipe.zip", dest)
    errors = validate_driver(dest, "Hypervisor")
    assert any("destroy_instance" in e for e in errors)


# --- ledger transitions + request_id uniqueness -----------------------------


async def test_ledger_insert_is_idempotent_on_request_id():
    async with TestSessionLocal() as db:
        row = await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        assert row.status == "CREATING"
        row2 = await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    assert str(row2.id) == str(row.id)
    assert len(await _rows()) == 1


async def test_ledger_active_then_destroyed_transition():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await set_instance_ref(db, REQUEST_ID, "vm-100")
        await mark_active(db, REQUEST_ID, DEVICE_ID, "vm-100")
    async with TestSessionLocal() as db:
        row = await get_by_request_id(db, REQUEST_ID)
        assert row.status == "ACTIVE"
        assert str(row.device_id) == DEVICE_ID
        assert row.instance_ref == "vm-100"
    async with TestSessionLocal() as db:
        await mark_destroyed(db, REQUEST_ID)
        row = await get_by_request_id(db, REQUEST_ID)
        assert row.status == "DESTROYED"


async def test_list_teardown_candidates_excludes_destroyed():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        other = str(uuid.uuid4())
        await insert_or_get_creating(db, other, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_destroyed(db, other)
    async with TestSessionLocal() as db:
        candidates = await list_teardown_candidates(db, RES_ID)
    assert {str(c.request_id) for c in candidates} == {REQUEST_ID}


# --- create flow happy path -------------------------------------------------


async def test_provision_happy_path_records_runs_ledger_device_and_callback():
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    create_dev = AsyncMock(return_value={"id": DEVICE_ID})
    callback = AsyncMock()
    patches = _create_patches(execute)
    # Swap in the spy variants we assert on.
    patches[-1] = patch("app.services.nats_consumer._create_dynamic_device", new=create_dev)
    patches.append(
        patch(
            "app.services.nats_consumer._post_provision_result_best_effort",
            new=callback,
        )
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    # Recipe ran login -> create_instance -> logout, in order.
    assert [c[0] for c in calls] == ["login", "create_instance", "logout"]

    # ExecutionRun rows recorded for all three, keyed on the hypervisor id. The
    # login->create->logout order is asserted via `calls`; here we assert the
    # recorded set (SQLite created_at is second-precision and can tie).
    runs = await _runs()
    assert sorted(r.action for r in runs) == ["create_instance", "login", "logout"]
    assert all(str(r.device_id) == HYPERVISOR_ID for r in runs)
    assert all(r.status == "SUCCESS" for r in runs)

    # Ledger row ACTIVE with the materialized device + instance_ref.
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].status == "ACTIVE"
    assert str(rows[0].device_id) == DEVICE_ID
    assert rows[0].instance_ref == "vm-100"

    # Device create call shape: (client, template_id, reservation_id, field_data).
    args = create_dev.await_args.args
    assert args[1] == TEMPLATE_ID
    assert args[2] == RES_ID
    assert args[3] == {"mgmt_ip": "10.0.0.9"}

    # Success callback body.
    callback.assert_awaited_once()
    kwargs = callback.await_args.kwargs
    assert kwargs["succeeded"] is True
    assert kwargs["device_ids"] == [DEVICE_ID]
    assert kwargs["error"] is None


async def test_dispatch_routes_provision_requested_to_create_flow():
    """handle_reservation_event dispatches the new event to the create flow."""
    with patch("app.services.nats_consumer._handle_provision_requested", new=AsyncMock()) as spy:
        await handle_reservation_event(_event(), _db_session_factory(), dedupe_key="s:1")
    spy.assert_awaited_once()


# --- redelivery idempotency -------------------------------------------------


async def test_redelivery_skips_active_row_and_still_reports_success():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_active(db, REQUEST_ID, DEVICE_ID, "vm-100")

    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    callback = AsyncMock()
    patches = _create_patches(execute)
    patches.append(
        patch(
            "app.services.nats_consumer._post_provision_result_best_effort",
            new=callback,
        )
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:2")

    # No recipe method ran; the create was skipped entirely.
    assert calls == []
    # Still reports success with the already-materialized device id.
    callback.assert_awaited_once()
    assert callback.await_args.kwargs["device_ids"] == [DEVICE_ID]


# --- permanent + transient classification -----------------------------------


async def test_missing_template_is_permanent_dlq_with_failure_callback():
    calls, execute = _recipe_execute({})
    posted = AsyncMock()
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    patches = _create_patches(execute, template=None)
    patches.append(patch("app.services.nats_consumer._post_provision_result", new=posted))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, msg.data)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    # Best-effort failure callback fired with an empty device set.
    posted.assert_awaited_once()
    assert posted.await_args.kwargs["succeeded"] is False
    assert posted.await_args.kwargs["device_ids"] == []


async def test_missing_template_raises_permanent_directly():
    calls, execute = _recipe_execute({})
    patches = _create_patches(execute, template=None)
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(PermanentEventError):
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")


async def test_transient_5xx_naks():
    calls, execute = _recipe_execute({})
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    patches = _create_patches(execute)
    # A 5xx on the template fetch surfaces as TransientUpstreamError.
    patches[0] = patch(
        "app.services.nats_consumer._fetch_template",
        new=AsyncMock(side_effect=TransientUpstreamError("upstream 503")),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


# --- broken recipe package classification (issue #279) ----------------------
#
# A structurally broken package (missing Driver class, missing a required
# Hypervisor method, invalid archive, unparseable driver.py) can never load, so
# load_driver raises DriverPackageError. The create path maps that to a
# PermanentEventError, dead-lettering on FIRST delivery instead of NAK'ing
# through the full max_deliver ladder. A download failure (inventory
# unreachable) stays a transient RuntimeError and still NAKs for retry.

# The message load_driver pins for a missing Driver class (see
# driver_loader.validate_driver + load_driver); the recipe path wraps it.
_VALIDATION_MSG = "Driver validation failed: driver.py must define a class named Driver"


def _broken_package_patches(execute, exc):
    """create-flow patches with load_driver raising `exc` (a package failure)."""
    patches = _create_patches(execute)
    patches[3] = patch(
        "app.services.driver_loader.load_driver",
        new=AsyncMock(side_effect=exc),
    )
    return patches


async def test_broken_package_raises_permanent_with_diagnosable_message():
    """A DriverPackageError from the recipe load becomes a PermanentEventError
    whose message names the request and carries the underlying reason."""
    calls, execute = _recipe_execute({})
    patches = _broken_package_patches(execute, DriverPackageError(_VALIDATION_MSG))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(PermanentEventError) as excinfo:
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    message = str(excinfo.value)
    assert "recipe package cannot load" in message
    assert REQUEST_ID in message
    # The diagnosable underlying reason is preserved for the DLQ log/callback.
    assert "must define a class named Driver" in message
    # No recipe method ran: the package never loaded, so login/create never fired.
    assert calls == []


async def test_broken_package_dlqs_on_first_delivery_with_failure_callback():
    """The whole event dead-letters on the FIRST delivery (no retry ladder) and
    the best-effort failure callback fires so reservations fails fast."""
    calls, execute = _recipe_execute({})
    posted = AsyncMock()
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    # First delivery: a permanent failure must DLQ here, not ride the ladder.
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    patches = _broken_package_patches(execute, DriverPackageError(_VALIDATION_MSG))
    patches.append(patch("app.services.nats_consumer._post_provision_result", new=posted))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "dlq"
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, msg.data)
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    # The recipe never executed: no NAK ladder, no sandbox steps.
    assert calls == []
    # Failure callback fired with an empty device set: reservations transitions
    # to FAILED without waiting for the 900s timeout backstop.
    posted.assert_awaited_once()
    assert posted.await_args.kwargs["succeeded"] is False
    assert posted.await_args.kwargs["device_ids"] == []
    assert _VALIDATION_MSG in posted.await_args.kwargs["error"]


async def test_broken_package_leaves_row_creating_for_teardown():
    """The permanent create failure leaves the CREATING ledger row (inserted
    before the load) for the reservation.failed teardown handler to retire."""
    calls, execute = _recipe_execute({})
    patches = _broken_package_patches(execute, DriverPackageError(_VALIDATION_MSG))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(PermanentEventError):
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].status == "CREATING"
    assert rows[0].instance_ref is None
    assert rows[0].device_id is None


async def test_recipe_download_failure_still_naks():
    """A transient load failure (inventory unreachable: RuntimeError from the
    download step) is NOT permanent; it NAKs so JetStream retries with backoff."""
    calls, execute = _recipe_execute({})
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    patches = _broken_package_patches(
        execute, RuntimeError("Failed to download driver abc: connect error")
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()


async def test_create_instance_driver_failure_naks_row_stays_creating():
    calls, execute = _recipe_execute({"create_instance": CREATE_DRIVER_FAIL})
    patches = _create_patches(execute)
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(RuntimeError):
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].status == "CREATING"
    # create_instance failed before the instance_ref could be persisted.
    assert rows[0].instance_ref is None
    assert rows[0].device_id is None


# --- device create/delete HTTP shape ----------------------------------------


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


async def test_create_dynamic_device_maps_status_codes():
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Resp(201, {"id": DEVICE_ID}))
    got = await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {"k": "v"})
    assert got == {"id": DEVICE_ID}
    body = client.post.await_args.kwargs["json"]
    assert body == {"template_id": TEMPLATE_ID, "reservation_id": RES_ID, "field_data": {"k": "v"}}

    client.post = AsyncMock(return_value=_Resp(500))
    with pytest.raises(TransientUpstreamError):
        await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {})

    client.post = AsyncMock(return_value=_Resp(422))
    assert await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {}) is None


async def test_delete_dynamic_device_maps_status_codes():
    client = AsyncMock()
    client.delete = AsyncMock(return_value=_Resp(204))
    assert await _delete_dynamic_device(client, DEVICE_ID) is True

    client.delete = AsyncMock(return_value=_Resp(404))
    assert await _delete_dynamic_device(client, DEVICE_ID) is True

    client.delete = AsyncMock(return_value=_Resp(500))
    with pytest.raises(TransientUpstreamError):
        await _delete_dynamic_device(client, DEVICE_ID)

    client.delete = AsyncMock(return_value=_Resp(409))
    assert await _delete_dynamic_device(client, DEVICE_ID) is False


# --- teardown matrix --------------------------------------------------------


async def _seed_active(instance_ref="vm-100", device_id=DEVICE_ID):
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        if instance_ref is not None:
            await set_instance_ref(db, REQUEST_ID, instance_ref)
        if instance_ref is not None and device_id is not None:
            await mark_active(db, REQUEST_ID, device_id, instance_ref)


def _teardown_patches(execute, *, delete=None):
    patches = [
        patch(
            "app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=TEMPLATE_DATA)
        ),
        patch(
            "app.services.nats_consumer._fetch_hypervisor",
            new=AsyncMock(return_value=HYPERVISOR_DATA),
        ),
        patch(
            "app.services.nats_consumer._fetch_secret_value",
            new=AsyncMock(return_value=SECRET_DATA),
        ),
        patch("app.services.driver_loader.load_driver", new=AsyncMock(return_value="/tmp/recipe")),
        patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute),
    ]
    if delete is not None:
        patches.append(patch("app.services.nats_consumer._delete_dynamic_device", new=delete))
    return patches


async def test_teardown_happy_path_destroys_and_marks_destroyed():
    await _seed_active()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    delete = AsyncMock(return_value=True)
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)

    assert [c[0] for c in calls] == ["login", "destroy_instance", "logout"]
    # destroy_instance received the instance_ref via method_kwargs.
    destroy_call = next(c for c in calls if c[0] == "destroy_instance")
    assert destroy_call[1] == {"instance_ref": "vm-100"}
    delete.assert_awaited_once()
    rows = await _rows()
    assert rows[0].status == "DESTROYED"


async def test_teardown_driver_failure_leaves_active_and_acks():
    await _seed_active()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_DRIVER_FAIL})
    delete = AsyncMock(return_value=True)
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        # Must NOT raise (ACK); the row stays ACTIVE as may-still-exist.
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)

    delete.assert_not_awaited()
    rows = await _rows()
    assert rows[0].status == "ACTIVE"


async def test_teardown_transient_delete_raises():
    await _seed_active()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    delete = AsyncMock(side_effect=TransientUpstreamError("delete 503"))
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        with pytest.raises(TransientUpstreamError):
            await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)

    rows = await _rows()
    assert rows[0].status == "ACTIVE"


async def test_teardown_creating_without_instance_ref_marks_destroyed():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    calls, execute = _recipe_execute({})
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)

    # Nothing hypervisor-side to destroy: no recipe method ran.
    assert calls == []
    rows = await _rows()
    assert rows[0].status == "DESTROYED"


async def test_teardown_destroyed_row_is_noop():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_destroyed(db, REQUEST_ID)
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)
    assert calls == []


async def test_teardown_updated_only_removed_devices():
    """reservation.updated teardown destroys only rows whose device was removed."""
    await _seed_active()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    delete = AsyncMock(return_value=True)
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        # Removed set does NOT include this instance's device: it is left alone.
        await _execute_dynamic_teardown(
            RES_ID, USER_ID, _db_session_factory(), client, removed_device_ids=[str(uuid.uuid4())]
        )
    assert calls == []
    rows = await _rows()
    assert rows[0].status == "ACTIVE"

    # Now with the instance's device in the removed set: it is torn down.
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        await _execute_dynamic_teardown(
            RES_ID, USER_ID, _db_session_factory(), client, removed_device_ids=[DEVICE_ID]
        )
    rows = await _rows()
    assert rows[0].status == "DESTROYED"


# --- secret isolation via password_keys -------------------------------------

_ENV_DUMP_RECIPE = """
import os

class Driver:
    def __init__(self, context):
        self.context = context

    def create_instance(self):
        return {
            "env_has_secret": "HERD_SECRET_PASSWORD" in os.environ,
            "context_secret": self.context.get("HERD_secret_password", "MISSING"),
        }
"""


def test_recipe_context_secret_keys_excluded_from_child_env(tmp_path):
    """Secret values reach the recipe via the context file, never the env."""
    from app.services.driver_sandbox import execute_driver_method

    dest = tmp_path / "recipe"
    for name, content in {"driver.py": _ENV_DUMP_RECIPE}.items():
        (dest).mkdir(parents=True, exist_ok=True)
        (dest / name).write_text(content)

    context, secret_keys = _build_recipe_context(
        TEMPLATE_DATA, HYPERVISOR_DATA, SECRET_DATA, REQUEST_ID, RES_ID, USER_ID
    )
    # Both secret values are present in context and flagged as password keys.
    assert context["HERD_secret_password"] == "hunter2"
    assert secret_keys == {"HERD_secret_username", "HERD_secret_password"}

    result = execute_driver_method(
        str(dest), "create_instance", context, timeout=10, password_keys=secret_keys
    )
    assert result["success"] is True
    assert result["output"]["env_has_secret"] is False
    assert result["output"]["context_secret"] == "hunter2"


def test_build_recipe_context_carries_hypervisor_and_ids():
    context, _ = _build_recipe_context(
        TEMPLATE_DATA, HYPERVISOR_DATA, SECRET_DATA, REQUEST_ID, RES_ID, USER_ID
    )
    assert context["HERD_hypervisor_endpoint"] == "https://pve.example:8006"
    assert context["HERD_hypervisor_type"] == "proxmox"
    assert context["HERD_request_id"] == REQUEST_ID
    assert context["HERD_reservation_id"] == RES_ID
    assert context["HERD_user_id"] == USER_ID
    # Template field default lands as a HERD_ key.
    assert context["HERD_image"] == "debian12"


# --- callback retry-then-log ------------------------------------------------


async def test_callback_retries_then_succeeds():
    attempts = {"n": 0}

    async def _flaky(client, reservation_id, *, succeeded, device_ids, error):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("callback 503")

    with patch("app.services.nats_consumer._post_provision_result", new=_flaky):
        await _post_provision_result_best_effort(
            RES_ID, succeeded=True, device_ids=[DEVICE_ID], error=None
        )
    assert attempts["n"] == 3


async def test_callback_persistent_failure_is_swallowed():
    async def _always_fail(client, reservation_id, *, succeeded, device_ids, error):
        raise RuntimeError("callback down")

    with patch("app.services.nats_consumer._post_provision_result", new=_always_fail):
        # Must not raise: the timeout backstop covers a lost callback.
        await _post_provision_result_best_effort(
            RES_ID, succeeded=True, device_ids=[DEVICE_ID], error=None
        )
