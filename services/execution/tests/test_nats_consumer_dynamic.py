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
- A row becomes DESTROYED only after the driver destroyed the instance or
  confirmed none exists (issue #937): a row with no instance_ref gets a keyed
  destroy (instance_ref=None, HERD_request_id in the context), and a failed or
  raising keyed destroy leaves the row live with a pinned log action.
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

import httpx
import pytest
from app.database import Base
from app.models.dynamic_instance import DynamicInstance
from app.models.execution_run import ExecutionRun
from app.services import dynamic_instance_service as dynamic_instance_service_module
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
from app.services.execution_service import driver_result_failed
from app.services.nats_consumer import (
    NATS_DLQ_SUBJECT,
    PermanentEventError,
    TransientUpstreamError,
    _build_recipe_context,
    _create_dynamic_device,
    _delete_dynamic_device,
    _execute_dynamic_teardown,
    _handle_provision_requested,
    _maybe_post_provision_failure,
    _post_provision_result_best_effort,
    _recipe_reported_success,
    handle_reservation_event,
    process_reservation_message,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


class _ReservationsStatusClient:
    """Stands in for reservations' GET /internal/{id}, reporting state["status"]."""

    def __init__(self, state):
        self._state = state

    async def get(self, url, **kwargs):
        code = self._state.get("code", 200)
        return httpx.Response(code, json={"status": self._state["status"]})


@pytest.fixture(autouse=True)
def reservation_status():
    """Run the REAL corroboration gate against a stubbed reservations answer.

    reservation.provision_requested is gated since issue #937, so every test
    that drives process_reservation_message would otherwise make a real HTTP
    call and NAK on its transport error. Defaults to PENDING_PROVISION, the
    status a legitimate provision_requested is staged under; a test sets
    reservation_status["status"] to model a reservation that has since ended.
    """
    state = {"status": "PENDING_PROVISION"}
    real_verify = nats_consumer._verify_reservation_event

    async def _verify(event_data, client):
        return await real_verify(event_data, _ReservationsStatusClient(state))

    with patch("app.services.nats_consumer._verify_reservation_event", new=_verify):
        yield state


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


async def test_insert_or_get_creating_concurrent_race_returns_winner_row():
    """A genuine two-session duplicate insert trips the IntegrityError recovery
    branch (lines 67-74): the loser rolls back and re-reads the winner's row
    instead of raising or double-inserting.

    The interleaving is real, not a mocked exception: `get_by_request_id` is
    wrapped so that, on the loser's FIRST read (a miss, matching the real race
    window), a second session inserts and commits the same request_id before
    the loser's own commit runs. SQLite's unique constraint on request_id then
    raises IntegrityError on the loser's real `db.commit()` call.
    """
    real_get_by_request_id = dynamic_instance_service_module.get_by_request_id
    call_count = {"n": 0}

    async def _interleave_winner_before_first_read(db, request_id):
        call_count["n"] += 1
        result = await real_get_by_request_id(db, request_id)
        if call_count["n"] == 1:
            # Simulate the concurrent redelivery: a second session wins the
            # race and commits its row between the loser's read and insert.
            # Built directly (not via insert_or_get_creating) so the winner's
            # own read does not also go through the patched function below.
            async with TestSessionLocal() as db_winner:
                winner_row = DynamicInstance(
                    request_id=uuid.UUID(REQUEST_ID),
                    reservation_id=uuid.UUID(RES_ID),
                    template_id=uuid.UUID(TEMPLATE_ID),
                    hypervisor_id=uuid.UUID(HYPERVISOR_ID),
                    status="CREATING",
                )
                db_winner.add(winner_row)
                await db_winner.commit()
                await db_winner.refresh(winner_row)
                call_count["winner_id"] = winner_row.id
        return result

    async with TestSessionLocal() as db_loser:
        with patch.object(
            dynamic_instance_service_module,
            "get_by_request_id",
            side_effect=_interleave_winner_before_first_read,
        ):
            loser_row = await insert_or_get_creating(
                db_loser, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID
            )

    # The loser recovered the winner's row rather than raising or duplicating.
    assert loser_row.id == call_count["winner_id"]
    assert loser_row.status == "CREATING"
    # The wrapper was called twice: the initial miss, then the post-rollback
    # re-read inside the except IntegrityError branch.
    assert call_count["n"] == 2
    assert len(await _rows()) == 1


async def test_insert_or_get_creating_reraises_when_no_row_found_after_integrity_error():
    """The documented real-anomaly path: IntegrityError fires, but the
    post-rollback re-read finds nothing. That combination should never happen
    from a legitimate unique-constraint race, so the function re-raises
    instead of silently retrying forever.
    """
    real_get_by_request_id = dynamic_instance_service_module.get_by_request_id
    call_count = {"n": 0}

    async def _integrity_error_then_missing_row(db, request_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First read: a real miss, plus a concurrent winner commits so the
            # loser's own commit below genuinely raises IntegrityError. Built
            # directly (not via insert_or_get_creating) so the winner's own
            # read does not also go through the patched function below.
            result = await real_get_by_request_id(db, request_id)
            async with TestSessionLocal() as db_winner:
                winner_row = DynamicInstance(
                    request_id=uuid.UUID(REQUEST_ID),
                    reservation_id=uuid.UUID(RES_ID),
                    template_id=uuid.UUID(TEMPLATE_ID),
                    hypervisor_id=uuid.UUID(HYPERVISOR_ID),
                    status="CREATING",
                )
                db_winner.add(winner_row)
                await db_winner.commit()
            return result
        # Second read (the post-rollback recovery re-read): report the real
        # anomaly by returning None even though a row now exists.
        return None

    async with TestSessionLocal() as db_loser:
        with patch.object(
            dynamic_instance_service_module,
            "get_by_request_id",
            side_effect=_integrity_error_then_missing_row,
        ):
            with pytest.raises(IntegrityError):
                await insert_or_get_creating(
                    db_loser, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID
                )

    assert call_count["n"] == 2
    # The winner's row still exists; only the loser's redundant insert failed.
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
        assert await mark_destroyed(db, REQUEST_ID, instance_ref="vm-100", device_id=DEVICE_ID)
        row = await get_by_request_id(db, REQUEST_ID)
        assert row.status == "DESTROYED"


async def test_list_teardown_candidates_excludes_destroyed():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        other = str(uuid.uuid4())
        await insert_or_get_creating(db, other, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_destroyed(db, other, instance_ref=None, device_id=None)
    async with TestSessionLocal() as db:
        candidates = await list_teardown_candidates(db, RES_ID)
    assert {str(c.request_id) for c in candidates} == {REQUEST_ID}


# --- row-absent no-op guards -------------------------------------------------
#
# set_instance_ref / mark_active / mark_destroyed each re-read the row by
# request_id and no-op if it is missing (a redelivery racing a row that was
# never inserted, or was inserted under a different request_id). None of the
# existing tests call these against an unknown request_id, so the `if row is
# None: return` guards were never exercised.


async def test_set_instance_ref_is_noop_for_unknown_request_id():
    unknown = str(uuid.uuid4())
    async with TestSessionLocal() as db:
        await set_instance_ref(db, unknown, "vm-999")
    assert await _rows() == []


async def test_mark_active_is_noop_for_unknown_request_id():
    unknown = str(uuid.uuid4())
    async with TestSessionLocal() as db:
        await mark_active(db, unknown, DEVICE_ID, "vm-999")
    assert await _rows() == []


async def test_mark_destroyed_is_noop_for_unknown_request_id():
    unknown = str(uuid.uuid4())
    async with TestSessionLocal() as db:
        assert not await mark_destroyed(db, unknown, instance_ref=None, device_id=None)
    assert await _rows() == []


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

    # Device create call shape: (client, template_id, reservation_id, field_data,
    # request_id). The ledger request_id is threaded through so a redelivered
    # create is idempotent inventory-side (issue #275).
    args = create_dev.await_args.args
    assert args[1] == TEMPLATE_ID
    assert args[2] == RES_ID
    assert args[3] == {"mgmt_ip": "10.0.0.9"}
    assert args[4] == REQUEST_ID

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
    # The reason posted to reservations is class-name-only (issue #870): the
    # diagnosable message (_VALIDATION_MSG) never leaves this service, since
    # it can carry driver-package internals. It IS still fully readable in
    # this process's log via exc_info, per process_reservation_message's
    # "Permanent error processing NATS message" record.
    assert posted.await_args.kwargs["error"] == "provisioning failed: PermanentEventError"
    assert _VALIDATION_MSG not in posted.await_args.kwargs["error"]


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
    got = await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {"k": "v"}, REQUEST_ID)
    assert got == {"id": DEVICE_ID}
    body = client.post.await_args.kwargs["json"]
    # request_id is carried so inventory can dedupe a redelivered create (issue #275).
    assert body == {
        "template_id": TEMPLATE_ID,
        "reservation_id": RES_ID,
        "field_data": {"k": "v"},
        "request_id": REQUEST_ID,
    }

    client.post = AsyncMock(return_value=_Resp(500))
    with pytest.raises(TransientUpstreamError):
        await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {}, REQUEST_ID)

    client.post = AsyncMock(return_value=_Resp(422))
    assert await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {}, REQUEST_ID) is None


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


async def test_post_provision_result_posts_body_and_raises_for_status():
    """The real (unmocked) implementation: POSTs the expected body and calls
    raise_for_status so a non-2xx surfaces as an httpx error, not silently."""
    from app.services.nats_consumer import _post_provision_result

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)

    await _post_provision_result(client, RES_ID, succeeded=True, device_ids=[DEVICE_ID], error=None)

    client.post.assert_awaited_once()
    _url, kwargs = client.post.await_args.args, client.post.await_args.kwargs
    assert kwargs["json"] == {"succeeded": True, "device_ids": [DEVICE_ID], "error": None}
    resp.raise_for_status.assert_called_once()


async def test_post_provision_result_propagates_raise_for_status():
    from app.services.nats_consumer import _post_provision_result

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=RuntimeError("500 Server Error"))
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)

    with pytest.raises(RuntimeError, match="500 Server Error"):
        await _post_provision_result(client, RES_ID, succeeded=False, device_ids=[], error="boom")


async def test_create_dynamic_device_raises_on_transport_error():
    import httpx

    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(TransientUpstreamError):
        await _create_dynamic_device(client, TEMPLATE_ID, RES_ID, {}, REQUEST_ID)


async def test_delete_dynamic_device_raises_on_transport_error():
    import httpx

    client = AsyncMock()
    client.delete = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(TransientUpstreamError):
        await _delete_dynamic_device(client, DEVICE_ID)


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


async def test_teardown_creating_without_instance_ref_runs_keyed_destroy():
    """Issue #937 (replaces the old "no instance_ref means nothing to destroy"
    test, which retired the row with no driver call): a CREATING row with no
    instance_ref may have a real instance behind it, so teardown drives
    destroy_instance with instance_ref=None and HERD_request_id in the
    context, and the row becomes DESTROYED only because that destroy reported
    success."""
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    delete = AsyncMock(return_value=True)
    client = AsyncMock()
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), client)

    assert [c[0] for c in calls] == ["login", "destroy_instance", "logout"]
    destroy_call = next(c for c in calls if c[0] == "destroy_instance")
    assert destroy_call[1] == {"instance_ref": None}
    assert destroy_call[2]["HERD_request_id"] == REQUEST_ID
    # No device was ever materialized, so there is nothing to delete.
    delete.assert_not_awaited()
    rows = await _rows()
    assert rows[0].status == "DESTROYED"
    # The run row records the keyed call shape, which is what an operator (and
    # the live integration test) reads to tell a keyed destroy apart.
    destroy_runs = [r for r in await _runs() if r.action == "destroy_instance"]
    assert destroy_runs[0].input_params["method_kwargs"] == {"instance_ref": None}


async def test_teardown_destroyed_row_is_noop():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_destroyed(db, REQUEST_ID, instance_ref=None, device_id=None)
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


def test_build_recipe_context_skips_field_with_no_key():
    """A malformed section field with no `key` contributes no HERD_ context entry
    (defensively skipped) rather than raising or producing a HERD_None key."""
    template = {
        **TEMPLATE_DATA,
        "sections": [
            {
                "name": "Instance",
                "fields": [
                    {"key": "image", "type": "string", "default": "debian12"},
                    {"type": "string", "default": "orphan-value"},
                ],
            }
        ],
    }
    context, _ = _build_recipe_context(
        template, HYPERVISOR_DATA, SECRET_DATA, REQUEST_ID, RES_ID, USER_ID
    )
    assert context["HERD_image"] == "debian12"
    assert "HERD_None" not in context
    assert "orphan-value" not in context.values()


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


# --- _maybe_post_provision_failure: best-effort DLQ failure callback --------


async def test_maybe_post_provision_failure_ignores_other_events():
    """Only reservation.provision_requested gets the failure callback; any other
    event type is a silent no-op (no callback attempt at all)."""
    called = {"n": 0}

    async def _spy(client, reservation_id, *, succeeded, device_ids, error):
        called["n"] += 1

    with patch("app.services.nats_consumer._post_provision_result", new=_spy):
        await _maybe_post_provision_failure(
            {"event": "reservation.wiring_changed", "reservation_id": RES_ID}, "boom"
        )
    assert called["n"] == 0


async def test_maybe_post_provision_failure_ignores_missing_reservation_id():
    called = {"n": 0}

    async def _spy(client, reservation_id, *, succeeded, device_ids, error):
        called["n"] += 1

    with patch("app.services.nats_consumer._post_provision_result", new=_spy):
        await _maybe_post_provision_failure({"event": "reservation.provision_requested"}, "boom")
    assert called["n"] == 0


async def test_maybe_post_provision_failure_posts_failed_callback():
    seen = {}

    async def _spy(client, reservation_id, *, succeeded, device_ids, error):
        seen["reservation_id"] = reservation_id
        seen["succeeded"] = succeeded
        seen["device_ids"] = device_ids
        seen["error"] = error

    with patch("app.services.nats_consumer._post_provision_result", new=_spy):
        await _maybe_post_provision_failure(
            {"event": "reservation.provision_requested", "reservation_id": RES_ID},
            "DLQ exhausted",
        )

    assert seen == {
        "reservation_id": RES_ID,
        "succeeded": False,
        "device_ids": [],
        "error": "DLQ exhausted",
    }


async def test_maybe_post_provision_failure_swallows_callback_error():
    async def _always_fail(client, reservation_id, *, succeeded, device_ids, error):
        raise RuntimeError("reservations unreachable")

    with patch("app.services.nats_consumer._post_provision_result", new=_always_fail):
        # Must not raise: this is a best-effort callback on an already-DLQ'd event.
        await _maybe_post_provision_failure(
            {"event": "reservation.provision_requested", "reservation_id": RES_ID}, "boom"
        )


# --- driver_result_failed vs _recipe_reported_success missing-key divergence ---
#
# The two helpers deliberately disagree on a missing output "success" key:
# driver_result_failed treats it as a bare-data return and stays SUCCESS, while
# _recipe_reported_success layers a stricter missing-key-is-failure rule on top
# (nats_consumer.py, _recipe_reported_success docstring). Each pair below drives
# both helpers on the SAME input so the opposite defaults are pinned together.


def test_bare_data_output_diverges_on_missing_success_key():
    result = {
        "success": True,
        "output": {"instance_ref": "vm-9"},
        "error": None,
        "duration_ms": 1,
    }
    assert driver_result_failed(result) == (False, None)
    assert _recipe_reported_success(result) is False


def test_output_none_diverges_on_missing_success_key():
    result = {"success": True, "output": None, "error": None, "duration_ms": 1}
    assert driver_result_failed(result) == (False, None)
    assert _recipe_reported_success(result) is False


def test_explicit_output_failure_agrees_on_both_helpers():
    result = {
        "success": True,
        "output": {"success": False, "error": "hypervisor rejected"},
        "error": None,
        "duration_ms": 1,
    }
    assert driver_result_failed(result) == (True, "hypervisor rejected")
    assert _recipe_reported_success(result) is False


def test_explicit_output_success_agrees_on_both_helpers():
    result = {
        "success": True,
        "output": {"success": True, "instance_ref": "vm-9"},
        "error": None,
        "duration_ms": 1,
    }
    assert driver_result_failed(result) == (False, None)
    assert _recipe_reported_success(result) is True


def test_transport_failure_agrees_on_both_helpers():
    """A sandbox-level failure (transport flag False) fails both helpers alike."""
    result = {"success": False, "output": None, "error": "driver crashed", "duration_ms": 1}
    assert driver_result_failed(result) == (True, "driver crashed")
    assert _recipe_reported_success(result) is False


# --- _fetch_hypervisor / _fetch_secret_value: the fetch helpers themselves ----
#
# Both are mocked out wholesale everywhere else in this file; these exercise their
# own status-code handling directly (mirrors the _StubClient pattern used for
# _fetch_fork_intended_wires in test_nats_consumer_wiring_changed.py).


class _StubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _StubClient:
    def __init__(self, status_code, payload=None):
        self._response = _StubResponse(status_code, payload)

    async def get(self, url, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_fetch_hypervisor_200_returns_record():
    from app.services.nats_consumer import _fetch_hypervisor

    payload = {"id": "hv-1", "secret_id": "sec-1"}
    result = await _fetch_hypervisor("hv-1", _StubClient(200, payload))
    assert result == payload


@pytest.mark.asyncio
async def test_fetch_hypervisor_404_returns_none():
    """A genuine 404 is a permanent config error to the caller, not a retryable one."""
    from app.services.nats_consumer import _fetch_hypervisor

    assert await _fetch_hypervisor("hv-gone", _StubClient(404)) is None


@pytest.mark.asyncio
async def test_fetch_hypervisor_5xx_raises_transient():
    from app.services.nats_consumer import TransientUpstreamError, _fetch_hypervisor

    with pytest.raises(TransientUpstreamError):
        await _fetch_hypervisor("hv-1", _StubClient(503))


@pytest.mark.asyncio
async def test_fetch_secret_value_200_returns_data_mapping():
    from app.services.nats_consumer import _fetch_secret_value

    payload = {"data": {"username": "admin", "password": "hunter2"}}
    result = await _fetch_secret_value("sec-1", _StubClient(200, payload))
    assert result == {"username": "admin", "password": "hunter2"}


@pytest.mark.asyncio
async def test_fetch_secret_value_200_non_dict_body_returns_empty_dict():
    """A 200 whose body is not a dict (malformed upstream response) degrades to an
    empty data mapping rather than raising on .get()."""
    from app.services.nats_consumer import _fetch_secret_value

    result = await _fetch_secret_value("sec-1", _StubClient(200, ["not", "a", "dict"]))
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_secret_value_404_returns_none():
    from app.services.nats_consumer import _fetch_secret_value

    assert await _fetch_secret_value("sec-gone", _StubClient(404)) is None


@pytest.mark.asyncio
async def test_fetch_secret_value_5xx_raises_transient():
    from app.services.nats_consumer import TransientUpstreamError, _fetch_secret_value

    with pytest.raises(TransientUpstreamError):
        await _fetch_secret_value("sec-1", _StubClient(500))


# --- forward-only ledger (issue #896) ----------------------------------------
#
# Invariant: a dynamic-instance ledger row moves only CREATING to ACTIVE to
# DESTROYED and never leaves DESTROYED; a hypervisor instance exists only while
# its row is CREATING or ACTIVE.


def _actions(caplog):
    return [
        rec.action
        for rec in caplog.records
        if getattr(rec, "action", None) and str(rec.action).startswith("dynamic_")
    ]


async def _seed_status(status):
    """Seed the ledger row for REQUEST_ID in the named scenario state."""
    if status == "absent":
        return
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    if status == "CREATING":
        return
    async with TestSessionLocal() as db:
        if status == "ACTIVE with device":
            await set_instance_ref(db, REQUEST_ID, "vm-100")
            await mark_active(db, REQUEST_ID, DEVICE_ID, "vm-100")
        elif status == "ACTIVE without device":
            row = await get_by_request_id(db, REQUEST_ID)
            row.status = "ACTIVE"
            await db.commit()
        elif status == "DESTROYED":
            await mark_destroyed(db, REQUEST_ID, instance_ref=None, device_id=None)


@pytest.mark.parametrize(
    "seed, expected_calls, expected_status, expected_device, expected_actions, expect_callback",
    [
        ("absent", ["login", "create_instance", "logout"], "ACTIVE", DEVICE_ID, [], True),
        ("CREATING", ["login", "create_instance", "logout"], "ACTIVE", DEVICE_ID, [], True),
        ("ACTIVE with device", [], "ACTIVE", DEVICE_ID, [], True),
        (
            "ACTIVE without device",
            ["login", "create_instance", "logout"],
            "ACTIVE",
            DEVICE_ID,
            [],
            True,
        ),
        (
            "DESTROYED",
            [],
            "DESTROYED",
            None,
            ["dynamic_instance_resurrection_refused"],
            False,
        ),
    ],
)
async def test_provision_over_every_ledger_state(
    seed,
    expected_calls,
    expected_status,
    expected_device,
    expected_actions,
    expect_callback,
    caplog,
):
    await _seed_status(seed)
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    callback = AsyncMock()
    patches = _create_patches(execute)
    patches.append(
        patch("app.services.nats_consumer._post_provision_result_best_effort", new=callback)
    )
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    assert [c[0] for c in calls] == expected_calls
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].status == expected_status
    assert (str(rows[0].device_id) if rows[0].device_id else None) == expected_device
    actions = _actions(caplog)
    if expected_actions:
        assert actions == expected_actions + ["dynamic_provision_abandoned"]
    else:
        assert actions == []
    assert (callback.await_count == 1) is expect_callback


async def test_ledger_cas_refuses_destroyed_row():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_destroyed(db, REQUEST_ID, instance_ref=None, device_id=None)
    async with TestSessionLocal() as db:
        assert await set_instance_ref(db, REQUEST_ID, "vm-1") is False
        assert await mark_active(db, REQUEST_ID, DEVICE_ID, "vm-1") is False
    row = (await _rows())[0]
    assert row.status == "DESTROYED"
    assert row.instance_ref is None
    assert row.device_id is None


async def test_ledger_cas_wins_on_creating_row():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        assert await set_instance_ref(db, REQUEST_ID, "vm-1") is True
        assert await mark_active(db, REQUEST_ID, DEVICE_ID, "vm-1") is True
    row = (await _rows())[0]
    assert (row.status, row.instance_ref, str(row.device_id)) == ("ACTIVE", "vm-1", DEVICE_ID)


async def test_teardown_between_create_and_instance_ref_destroys_created_instance(caplog):
    """Teardown retires the CREATING row (no instance_ref yet) while
    create_instance is in flight: the set_instance_ref CAS loses, the instance
    just created is destroyed again, and no device is ever created."""
    calls, execute = _recipe_execute({"create_instance": CREATE_OK, "destroy_instance": DESTROY_OK})
    create_dev = AsyncMock(return_value={"id": DEVICE_ID})
    callback = AsyncMock()
    real_set_ref = dynamic_instance_service_module.set_instance_ref

    async def _teardown_first(db, request_id, instance_ref):
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())
        return await real_set_ref(db, request_id, instance_ref)

    patches = _create_patches(execute)
    patches[-1] = patch("app.services.nats_consumer._create_dynamic_device", new=create_dev)
    patches.append(
        patch("app.services.nats_consumer._post_provision_result_best_effort", new=callback)
    )
    patches.append(
        patch.object(dynamic_instance_service_module, "set_instance_ref", new=_teardown_first)
    )
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    # Teardown's keyed destroy (no instance_ref yet, issue #937), then the
    # compensating destroy with the instance_ref create just returned.
    destroys = [c for c in calls if c[0] == "destroy_instance"]
    assert [c[1] for c in destroys] == [{"instance_ref": None}, {"instance_ref": "vm-100"}]
    create_dev.assert_not_awaited()
    callback.assert_not_awaited()
    assert (await _rows())[0].status == "DESTROYED"
    assert _actions(caplog) == [
        "dynamic_instance_compensated",
        "dynamic_instance_create_lost_to_teardown",
        "dynamic_provision_abandoned",
    ]


async def test_teardown_between_instance_ref_and_active_flip_leaves_no_orphan(caplog):
    """Teardown destroys the instance after set_instance_ref but before
    mark_active: the ACTIVE flip loses, the device just created is deleted, the
    instance is destroyed through the driver with its instance_ref, and the row
    stays DESTROYED."""
    calls, execute = _recipe_execute({"create_instance": CREATE_OK, "destroy_instance": DESTROY_OK})
    delete = AsyncMock(return_value=True)
    callback = AsyncMock()

    async def _create_device_then_teardown(client, *args, **kwargs):
        # Stands in for the window in which the other replica's teardown runs.
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())
        return {"id": DEVICE_ID}

    patches = _create_patches(execute)
    patches[-1] = patch(
        "app.services.nats_consumer._create_dynamic_device",
        new=_create_device_then_teardown,
    )
    patches.append(patch("app.services.nats_consumer._delete_dynamic_device", new=delete))
    patches.append(
        patch("app.services.nats_consumer._post_provision_result_best_effort", new=callback)
    )
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    # One destroy from teardown, one compensating destroy; both with vm-100.
    destroys = [c[1] for c in calls if c[0] == "destroy_instance"]
    assert destroys == [{"instance_ref": "vm-100"}, {"instance_ref": "vm-100"}]
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == DEVICE_ID
    callback.assert_not_awaited()
    row = (await _rows())[0]
    assert row.status == "DESTROYED"
    assert row.device_id is None
    assert _actions(caplog) == [
        "dynamic_instance_compensated",
        "dynamic_instance_create_lost_to_teardown",
        "dynamic_provision_abandoned",
    ]


async def test_failed_compensating_destroy_is_logged_not_raised(caplog):
    calls, execute = _recipe_execute(
        {"create_instance": CREATE_OK, "destroy_instance": DESTROY_DRIVER_FAIL}
    )
    real_set_ref = dynamic_instance_service_module.set_instance_ref

    async def _retire_row_first(db, request_id, instance_ref):
        await mark_destroyed(db, request_id, instance_ref=None, device_id=None)
        return await real_set_ref(db, request_id, instance_ref)

    patches = _create_patches(execute)
    patches.append(
        patch.object(dynamic_instance_service_module, "set_instance_ref", new=_retire_row_first)
    )
    patches.append(patch("app.services.nats_consumer._post_provision_result_best_effort"))
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    assert _actions(caplog) == [
        "dynamic_instance_compensation_failed",
        "dynamic_instance_create_lost_to_teardown",
        "dynamic_provision_abandoned",
    ]
    assert (await _rows())[0].status == "DESTROYED"


# --- keyed destroy (issue #937) -----------------------------------------------
#
# Invariant: a ledger row becomes DESTROYED only after the driver destroyed the
# instance or confirmed that none exists for its HERD_request_id. A row whose
# create outcome is unknown (no instance_ref, whatever its status) gets a keyed
# destroy, never a silent retirement.


def _formatted(caplog, action):
    """The JSONFormatter rendering of every record carrying `action`.

    Pinned through the real formatter (CLAUDE.md, LOG EXTRAS), not caplog text,
    so the test sees exactly the keys an operator's log search sees.
    """
    from herd_common.logging import JSONFormatter

    formatter = JSONFormatter("execution")
    return [
        json.loads(formatter.format(rec))
        for rec in caplog.records
        if getattr(rec, "action", None) == action
    ]


async def _seed_creating():
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)


async def test_keyed_destroy_driver_failure_leaves_row_creating_and_logs(caplog):
    """A keyed destroy that returns a driver-result failure: the row stays
    CREATING (not DESTROYED), teardown does not raise (the message ACKs), and
    the pinned operator action carries the request and reservation ids but no
    driver text."""
    await _seed_creating()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_DRIVER_FAIL})
    delete = AsyncMock(return_value=True)
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert [c[1] for c in calls if c[0] == "destroy_instance"] == [{"instance_ref": None}]
    delete.assert_not_awaited()
    rows = await _rows()
    assert rows[0].status == "CREATING"
    assert rows[0].instance_ref is None

    [record] = _formatted(caplog, "dynamic_instance_keyed_destroy_failed")
    assert record["level"] == "ERROR"
    assert record["request_id"] == REQUEST_ID
    assert record["reservation_id"] == RES_ID
    assert record["ledger_status"] == "CREATING"
    assert record["reason"] == "destroy_failed"
    # The driver's own error text never reaches the log extras.
    assert "still running" not in json.dumps(record)


async def test_keyed_destroy_on_active_row_without_ref_destroys_and_deletes_device():
    """Variant 2 as already-stored data: an ACTIVE row with a device and a NULL
    instance_ref (written before issue #937 made a ref-less create a failure).
    The no-ref branch keys on the missing ref, not on CREATING, so teardown
    runs the keyed destroy, then deletes the device, then retires the row."""
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        await mark_active(db, REQUEST_ID, DEVICE_ID, None)
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    delete = AsyncMock(return_value=True)
    with ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert [c[1] for c in calls if c[0] == "destroy_instance"] == [{"instance_ref": None}]
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == DEVICE_ID
    assert (await _rows())[0].status == "DESTROYED"


@pytest.mark.parametrize(
    "seed_patch, reason",
    [
        (
            patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=None)),
            "recipe_config_missing",
        ),
        (
            patch(
                "app.services.driver_loader.load_driver",
                new=AsyncMock(side_effect=DriverPackageError("broken")),
            ),
            "recipe_load_failed",
        ),
    ],
)
async def test_keyed_destroy_that_cannot_run_leaves_row_creating(seed_patch, reason, caplog):
    """A no-ref row whose recipe cannot even be driven (config gone, package
    will not load) used to be retired as "nothing to destroy"; it now stays
    CREATING with the pinned action and the fixed reason word."""
    await _seed_creating()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute):
            stack.enter_context(p)
        stack.enter_context(seed_patch)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert calls == []
    assert (await _rows())[0].status == "CREATING"
    [record] = _formatted(caplog, "dynamic_instance_keyed_destroy_failed")
    assert record["reason"] == reason


async def test_keyed_destroy_login_failure_leaves_row_creating(caplog):
    await _seed_creating()
    login_fail = {"success": False, "output": None, "error": "denied", "duration_ms": 1}
    calls, execute = _recipe_execute({"login": login_fail})
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert [c[0] for c in calls] == ["login"]
    assert (await _rows())[0].status == "CREATING"
    [record] = _formatted(caplog, "dynamic_instance_keyed_destroy_failed")
    assert record["reason"] == "login_failed"


async def test_by_ref_destroy_failure_does_not_emit_the_keyed_action(caplog):
    """The pinned action is specific to the keyed case; the existing by-ref
    failure path keeps its own log line."""
    await _seed_active()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_DRIVER_FAIL})
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute, delete=AsyncMock(return_value=True)):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert (await _rows())[0].status == "ACTIVE"
    assert _formatted(caplog, "dynamic_instance_keyed_destroy_failed") == []


# --- create that succeeds without an instance_ref (variant 2) -----------------


@pytest.mark.parametrize("bad_ref", [None, "", "   ", 4711])
async def test_create_success_without_instance_ref_is_a_failed_create(bad_ref, caplog):
    """A create_instance that reports success with a missing, empty, or
    non-string instance_ref never reaches set_instance_ref or mark_active: the
    create raises a PermanentEventError (a recipe defect, not a transient), the
    row stays CREATING with no ref, no device is created, and no success
    callback is posted."""
    output = {"success": True, "field_data": {"mgmt_ip": "10.0.0.9"}}
    if bad_ref is not None:
        output["instance_ref"] = bad_ref
    create_no_ref = {"success": True, "output": output, "error": None, "duration_ms": 5}
    calls, execute = _recipe_execute({"create_instance": create_no_ref})
    create_dev = AsyncMock(return_value={"id": DEVICE_ID})
    callback = AsyncMock()
    set_ref = AsyncMock(return_value=True)
    activate = AsyncMock(return_value=True)
    patches = _create_patches(execute)
    patches[-1] = patch("app.services.nats_consumer._create_dynamic_device", new=create_dev)
    patches += [
        patch("app.services.nats_consumer._post_provision_result_best_effort", new=callback),
        patch.object(dynamic_instance_service_module, "set_instance_ref", new=set_ref),
        patch.object(dynamic_instance_service_module, "mark_active", new=activate),
    ]
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(PermanentEventError, match="no instance_ref"):
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")

    set_ref.assert_not_awaited()
    activate.assert_not_awaited()
    create_dev.assert_not_awaited()
    callback.assert_not_awaited()
    row = (await _rows())[0]
    assert (row.status, row.instance_ref, row.device_id) == ("CREATING", None, None)
    [record] = _formatted(caplog, "dynamic_instance_create_missing_ref")
    assert record["request_id"] == REQUEST_ID
    assert record["reservation_id"] == RES_ID


# --- compensation with no instance_ref (_destroy_orphaned_instance) -----------


async def _compensate(execute, instance_ref):
    from app.services.nats_consumer import _destroy_orphaned_instance

    with patch("app.services.driver_sandbox.execute_driver_method", side_effect=execute):
        async with TestSessionLocal() as db:
            await _destroy_orphaned_instance(
                db,
                uuid.UUID(HYPERVISOR_ID),
                uuid.UUID(DRIVER_ID),
                "sha256abc",
                uuid.UUID(USER_ID),
                {},
                uuid.UUID(RES_ID),
                "/tmp/recipe",
                {"HERD_request_id": REQUEST_ID},
                set(),
                REQUEST_ID,
                instance_ref,
            )


@pytest.mark.parametrize("instance_ref", [None, ""])
async def test_compensation_without_instance_ref_runs_keyed_destroy(instance_ref, caplog):
    """The #896 compensation's no-ref branch used to log and give up; it now
    issues the keyed destroy like teardown does."""
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    with caplog.at_level("INFO"):
        await _compensate(execute, instance_ref)

    assert [c[0] for c in calls] == ["login", "destroy_instance", "logout"]
    destroy_call = next(c for c in calls if c[0] == "destroy_instance")
    assert destroy_call[1] == {"instance_ref": None}
    assert destroy_call[2]["HERD_request_id"] == REQUEST_ID
    assert _actions(caplog) == ["dynamic_instance_compensated"]


async def test_compensation_keyed_destroy_failure_is_logged_not_raised(caplog):
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_DRIVER_FAIL})
    with caplog.at_level("INFO"):
        await _compensate(execute, None)

    assert [c[1] for c in calls if c[0] == "destroy_instance"] == [{"instance_ref": None}]
    assert _actions(caplog) == ["dynamic_instance_compensation_failed"]


# --- stateful driver double, real sandbox (variants 1 and 2) ------------------
#
# The checked-in mock hypervisor is stateless, so it acknowledges any destroy
# and cannot show that an instance was really found and removed. This double
# keeps its "hypervisor" in a JSON file named by a template field default, runs
# as a real subprocess through the sandbox, and derives the instance name from
# HERD_request_id in create and in the keyed destroy, as the contract requires.

_STATEFUL_RECIPE = """
import json
from pathlib import Path


class Driver:
    def __init__(self, context):
        self.context = context
        self.state = Path(context["HERD_state_file"])
        self.mode = context.get("HERD_double_mode") or ""

    def _load(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def _save(self, vms):
        self.state.write_text(json.dumps(vms))

    def _name(self):
        return "vm-" + self.context["HERD_request_id"]

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def status(self):
        return {"reachable": True}

    def create_instance(self, **_):
        vms = self._load()
        vms[self._name()] = "id-" + self._name()
        self._save(vms)
        if self.mode == "fail_after_create":
            return {"success": False, "error": "power-on failed after the VM was created"}
        if self.mode == "no_ref":
            return {"success": True, "field_data": {}}
        return {"success": True, "instance_ref": vms[self._name()], "field_data": {}}

    def destroy_instance(self, instance_ref=None, **_):
        vms = self._load()
        if self.mode == "legacy":
            # A recipe written to the pre-#937 contract: it assumes a ref.
            name = instance_ref.removeprefix("id-")
        elif instance_ref is None:
            name = self._name()
        else:
            name = instance_ref.removeprefix("id-")
        vms.pop(name, None)
        self._save(vms)
        return {"success": True}
"""


def _stateful_template(state_file, mode):
    return {
        **TEMPLATE_DATA,
        "sections": [
            {
                "name": "Double",
                "fields": [
                    {"key": "state_file", "type": "string", "default": str(state_file)},
                    {"key": "double_mode", "type": "string", "default": mode},
                ],
            }
        ],
    }


def _stateful_patches(recipe_dir, template, *, delete=None):
    """Every external seam except the sandbox, which runs the double for real."""
    return [
        patch("app.services.nats_consumer._fetch_template", new=AsyncMock(return_value=template)),
        patch(
            "app.services.nats_consumer._fetch_hypervisor",
            new=AsyncMock(return_value=HYPERVISOR_DATA),
        ),
        patch(
            "app.services.nats_consumer._fetch_secret_value",
            new=AsyncMock(return_value=SECRET_DATA),
        ),
        patch(
            "app.services.driver_loader.load_driver", new=AsyncMock(return_value=str(recipe_dir))
        ),
        patch(
            "app.services.nats_consumer._create_dynamic_device",
            new=AsyncMock(return_value={"id": DEVICE_ID}),
        ),
        patch(
            "app.services.nats_consumer._delete_dynamic_device",
            new=delete or AsyncMock(return_value=True),
        ),
        patch("app.services.nats_consumer._post_provision_result_best_effort", new=AsyncMock()),
    ]


def _write_double(tmp_path):
    recipe_dir = tmp_path / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "driver.py").write_text(_STATEFUL_RECIPE)
    return recipe_dir, tmp_path / "hypervisor.json"


def _hypervisor_vms(state_file):
    return json.loads(state_file.read_text()) if state_file.exists() else {}


@pytest.mark.parametrize("mode", ["fail_after_create", "no_ref"])
async def test_create_side_effect_then_failure_is_destroyed_by_teardown(tmp_path, mode):
    """Variant 1 (create fails after the VM exists) and variant 2 (create
    succeeds but returns no instance_ref), end to end through the real sandbox:
    the create NAKs with the row CREATING and no ref, the reservation goes
    terminal, and teardown's keyed destroy removes the VM the double really
    holds. Under the pre-#937 code the VM survived and the row was DESTROYED."""
    recipe_dir, state_file = _write_double(tmp_path)
    template = _stateful_template(state_file, mode)

    with ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, template):
            stack.enter_context(p)
        with pytest.raises(RuntimeError):
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="s:1")
        # The side effect is real: the hypervisor holds the instance.
        assert _hypervisor_vms(state_file) == {f"vm-{REQUEST_ID}": f"id-vm-{REQUEST_ID}"}
        row = (await _rows())[0]
        assert (row.status, row.instance_ref) == ("CREATING", None)

        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert _hypervisor_vms(state_file) == {}
    assert (await _rows())[0].status == "DESTROYED"


async def test_legacy_recipe_keyed_destroy_raises_row_stays_creating(tmp_path, caplog):
    """A recipe written to the old contract raises when instance_ref is None.
    Policy: the row stays CREATING as a may-still-exist record (the VM really
    does still exist), teardown does not raise (ACK), the run row stores only
    the exception class, and the pinned action names the request."""
    recipe_dir, state_file = _write_double(tmp_path)
    template = _stateful_template(state_file, "legacy")
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    state_file.write_text(json.dumps({f"vm-{REQUEST_ID}": f"id-vm-{REQUEST_ID}"}))

    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, template):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert _hypervisor_vms(state_file) == {f"vm-{REQUEST_ID}": f"id-vm-{REQUEST_ID}"}
    assert (await _rows())[0].status == "CREATING"
    destroy_run = next(r for r in await _runs() if r.action == "destroy_instance")
    assert destroy_run.error == "driver raised AttributeError"
    assert destroy_run.input_params["method_kwargs"] == {"instance_ref": None}
    [record] = _formatted(caplog, "dynamic_instance_keyed_destroy_failed")
    assert record["request_id"] == REQUEST_ID
    assert record["reason"] == "destroy_failed"
    assert "removeprefix" not in json.dumps(record)


async def test_teardown_rerun_retries_a_failed_keyed_destroy(tmp_path):
    """A row left CREATING by a failed keyed destroy is still a teardown
    candidate, so a later teardown run (a redelivered or replayed terminal
    event, after the recipe was fixed) retries the keyed destroy and only then
    retires the row."""
    recipe_dir, state_file = _write_double(tmp_path)
    async with TestSessionLocal() as db:
        await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
    state_file.write_text(json.dumps({f"vm-{REQUEST_ID}": f"id-vm-{REQUEST_ID}"}))

    with ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, _stateful_template(state_file, "legacy")):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())
    assert (await _rows())[0].status == "CREATING"

    with ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, _stateful_template(state_file, "")):
            stack.enter_context(p)
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())
    assert _hypervisor_vms(state_file) == {}
    assert (await _rows())[0].status == "DESTROYED"


# --- no resurrection after a failed keyed destroy -------------------------------


@pytest.mark.parametrize("status", ["CANCELLED", "COMPLETED", "FAILED", "ACTIVE", "PENDING"])
async def test_provision_redelivery_after_reservation_left_pending_provision_creates_nothing(
    tmp_path, reservation_status, status, caplog
):
    """A failed keyed destroy leaves the row CREATING, which the #896
    DESTROYED refusal does not cover. A provision_requested that arrives after
    the reservation left PENDING_PROVISION (a NAK-backoff redelivery behind a
    cancel) is refused by the corroboration gate: ACK, no recipe call, row
    unchanged."""
    await _seed_creating()
    reservation_status["status"] = status
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=2)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()

    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _create_patches(execute):
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "ack"
    msg.ack.assert_awaited_once()
    msg.nak.assert_not_awaited()
    assert calls == []
    row = (await _rows())[0]
    assert (row.status, row.instance_ref) == ("CREATING", None)
    [record] = _formatted(caplog, "nats_event_unverified")
    assert record["event"] == "reservation.provision_requested"
    assert record["reported_status"] == status


async def test_provision_requested_under_pending_provision_still_runs(reservation_status):
    """The gate's positive case: the status reservations stages the event under."""
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    patches = _create_patches(execute)
    patches.append(patch("app.services.nats_consumer._post_provision_result_best_effort"))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, MagicMock(), handle_reservation_event, _db_session_factory()
        )

    assert result == "ack"
    assert [c[0] for c in calls] == ["login", "create_instance", "logout"]
    assert (await _rows())[0].status == "ACTIVE"


async def test_create_success_without_instance_ref_dead_letters_on_first_delivery():
    """Decision on #937 review: a ref-less success is deterministic, so the
    message dead-letters on its FIRST delivery with the failed provision-result
    callback instead of NAKing through five more create_instance calls."""
    create_no_ref = {
        "success": True,
        "output": {"success": True, "field_data": {}},
        "error": None,
        "duration_ms": 5,
    }
    calls, execute = _recipe_execute({"create_instance": create_no_ref})
    posted = AsyncMock()
    js = MagicMock()
    js.publish = AsyncMock()
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=1)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    patches = _create_patches(execute)
    patches.append(patch("app.services.nats_consumer._post_provision_result", new=posted))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "dlq"
    msg.nak.assert_not_awaited()
    js.publish.assert_awaited_once_with(NATS_DLQ_SUBJECT, msg.data)
    assert [c[0] for c in calls].count("create_instance") == 1
    posted.assert_awaited_once()
    assert posted.await_args.kwargs["succeeded"] is False
    assert posted.await_args.kwargs["error"] == "provisioning failed: PermanentEventError"
    assert (await _rows())[0].status == "CREATING"


# --- provision_requested gate outage (issue #937 review) -------------------------


def _provision_msg(num_delivered):
    msg = MagicMock()
    msg.data = json.dumps(_event()).encode()
    msg.metadata = SimpleNamespace(num_delivered=num_delivered)
    msg.ack = AsyncMock()
    msg.nak = AsyncMock()
    return msg


async def test_reservations_5xx_on_the_provision_gate_naks(reservation_status):
    """The gate fails closed: a reservations 5xx is transient, so the message
    NAKs with a delay and no recipe step runs."""
    reservation_status["code"] = 503
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    js = MagicMock()
    js.publish = AsyncMock()
    msg = _provision_msg(1)
    with ExitStack() as stack:
        for p in _create_patches(execute):
            stack.enter_context(p)
        result = await process_reservation_message(
            msg, js, handle_reservation_event, _db_session_factory()
        )

    assert result == "nak"
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    js.publish.assert_not_awaited()
    assert calls == []
    assert await _rows() == []


async def test_reservations_outage_through_max_deliver_dead_letters_without_a_create(
    reservation_status,
):
    """An outage longer than the NAK schedule: the last delivery dead-letters
    and best-effort posts the failure callback (which, reservations being down,
    is lost; the provision timeout then fails the reservation). No create was
    ever attempted and no ledger row exists, so there is nothing to tear down."""
    reservation_status["code"] = 503
    calls, execute = _recipe_execute({"create_instance": CREATE_OK})
    posted = AsyncMock(side_effect=httpx.ConnectError("reservations down"))
    js = MagicMock()
    js.publish = AsyncMock()
    msg = _provision_msg(nats_consumer.NATS_MAX_DELIVER)
    patches = _create_patches(execute)
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
    posted.assert_awaited_once()
    assert posted.await_args.kwargs["error"] == "provisioning failed: TransientUpstreamError"
    assert calls == []
    assert await _rows() == []


# --- DESTROYED compare-and-swap (issue #937 review) ------------------------------
#
# Teardown holds its row snapshot across minutes of recipe calls; mark_destroyed
# must win only while the row is live AND still holds what teardown destroyed.

OTHER_DEVICE_ID = str(uuid.uuid4())


async def _seed_row(status, instance_ref, device_id):
    async with TestSessionLocal() as db:
        row = await insert_or_get_creating(db, REQUEST_ID, RES_ID, TEMPLATE_ID, HYPERVISOR_ID)
        row.status = status
        row.instance_ref = instance_ref
        row.device_id = uuid.UUID(device_id) if device_id else None
        await db.commit()


@pytest.mark.parametrize(
    "stored, snapshot, wins",
    [
        (("CREATING", None, None), (None, None), True),
        (("ACTIVE", "vm-1", DEVICE_ID), ("vm-1", DEVICE_ID), True),
        (("ACTIVE", None, DEVICE_ID), (None, DEVICE_ID), True),
        # A create recorded a ref after the snapshot.
        (("CREATING", "vm-1", None), (None, None), False),
        # A create recorded a ref and a device and flipped ACTIVE.
        (("ACTIVE", "vm-1", DEVICE_ID), (None, None), False),
        # The ref changed under the snapshot.
        (("ACTIVE", "vm-2", DEVICE_ID), ("vm-1", DEVICE_ID), False),
        # A device appeared, or changed.
        (("ACTIVE", "vm-1", DEVICE_ID), ("vm-1", None), False),
        (("ACTIVE", "vm-1", OTHER_DEVICE_ID), ("vm-1", DEVICE_ID), False),
        # The snapshot holds a ref the row no longer has.
        (("CREATING", None, None), ("vm-1", None), False),
        # Already DESTROYED: never re-written.
        (("DESTROYED", None, None), (None, None), False),
    ],
)
async def test_mark_destroyed_cas_matches_status_ref_and_device(stored, snapshot, wins):
    await _seed_row(*stored)
    async with TestSessionLocal() as db:
        won = await mark_destroyed(db, REQUEST_ID, instance_ref=snapshot[0], device_id=snapshot[1])
    assert won is wins
    row = (await _rows())[0]
    assert row.status == ("DESTROYED" if wins else stored[0])
    assert row.instance_ref == stored[1]


async def test_teardown_loses_cas_to_a_landing_create_then_destroys_what_it_holds(tmp_path):
    """The review's two-replica interleaving with the stateful double: B tears
    down R (CREATING, no ref, no device); its keyed destroy truthfully finds no
    VM; before B retires R, A's create lands in full (VM created, ref recorded,
    device materialized, ACTIVE). B's CAS must lose, B re-reads R and destroys
    the VM by ref and the device, and only then is R DESTROYED."""
    recipe_dir, state_file = _write_double(tmp_path)
    template = _stateful_template(state_file, "")
    await _seed_creating()
    delete = AsyncMock(return_value=True)
    real_mark = dynamic_instance_service_module.mark_destroyed
    seen = {"landed": False}

    async def _create_lands_first(db, request_id, **snapshot):
        if not seen["landed"]:
            seen["landed"] = True
            # Replica A's create completes inside B's window.
            await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="a:1")
            assert _hypervisor_vms(state_file) == {f"vm-{REQUEST_ID}": f"id-vm-{REQUEST_ID}"}
            assert (await _rows())[0].status == "ACTIVE"
        return await real_mark(db, request_id, **snapshot)

    with ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, template, delete=delete):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(dynamic_instance_service_module, "mark_destroyed", new=_create_lands_first)
        )
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert seen["landed"]
    assert _hypervisor_vms(state_file) == {}
    delete.assert_awaited_once()
    assert delete.await_args.args[1] == DEVICE_ID
    row = (await _rows())[0]
    assert row.status == "DESTROYED"
    # The first pass was keyed, the second destroyed by the ref the create
    # recorded (compared as a set: SQLite created_at can tie).
    destroys = {
        r.input_params["method_kwargs"]["instance_ref"]
        for r in await _runs()
        if r.action == "destroy_instance"
    }
    assert destroys == {None, f"id-vm-{REQUEST_ID}"}


async def test_teardown_that_keeps_losing_the_cas_leaves_the_row_live(caplog):
    """Bounded: if every pass loses the CAS the row is left live with the pinned
    contended action, never retired from a stale snapshot."""
    await _seed_creating()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    always_loses = AsyncMock(return_value=False)
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute, delete=AsyncMock(return_value=True)):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(dynamic_instance_service_module, "mark_destroyed", new=always_loses)
        )
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert always_loses.await_count == nats_consumer.DYNAMIC_TEARDOWN_MAX_ATTEMPTS
    assert [c[0] for c in calls].count("destroy_instance") == 3
    assert (await _rows())[0].status == "CREATING"
    [record] = _formatted(caplog, "dynamic_instance_teardown_contended")
    assert record["request_id"] == REQUEST_ID
    assert record["reservation_id"] == RES_ID
    assert record["attempts"] == 3


# --- failed create whose row teardown retired meanwhile (issue #937 review) -----


@pytest.mark.parametrize("mode", ["fail_after_create", "no_ref"])
async def test_failed_create_after_teardown_retired_the_row_is_compensated(tmp_path, mode, caplog):
    """B's keyed teardown runs while A is about to create: it finds no VM and
    retires R. A's create then makes the VM and fails (or returns no ref). A
    must not just raise into the resurrection refusal: it sees R DESTROYED and
    runs the keyed compensation, which removes the VM."""
    recipe_dir, state_file = _write_double(tmp_path)
    template = _stateful_template(state_file, mode)
    real_step = nats_consumer._run_recipe_step
    seen = {"teardown": False}

    async def _teardown_before_create(db, *args, **kwargs):
        if args[3] == "create_instance" and not seen["teardown"]:
            seen["teardown"] = True
            await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())
            assert (await _rows())[0].status == "DESTROYED"
            assert _hypervisor_vms(state_file) == {}
        return await real_step(db, *args, **kwargs)

    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _stateful_patches(recipe_dir, template):
            stack.enter_context(p)
        stack.enter_context(
            patch("app.services.nats_consumer._run_recipe_step", new=_teardown_before_create)
        )
        await _handle_provision_requested(_event(), _db_session_factory(), dedupe_key="a:1")

    assert seen["teardown"]
    assert _hypervisor_vms(state_file) == {}
    assert (await _rows())[0].status == "DESTROYED"
    assert _actions(caplog) == [
        "dynamic_instance_compensated",
        "dynamic_instance_create_lost_to_teardown",
        "dynamic_provision_abandoned",
    ]

# --- issue #1029: a transient recipe download failure during teardown NAKs ----


async def _seed_by_ref_active():
    await _seed_active()


@pytest.mark.parametrize(
    "seed, ledger_status",
    [(_seed_by_ref_active, "ACTIVE"), (_seed_creating, "CREATING")],
    ids=["by-ref", "keyed"],
)
async def test_teardown_recipe_download_failure_naks_then_redelivery_destroys(
    seed, ledger_status, caplog
):
    """load_driver's RuntimeError (the download step: a cache miss while
    inventory or package storage is unreachable) is transient. Teardown used
    to ACK it with the row left live and nothing ever retrying; it now raises
    TransientUpstreamError (the NAK path) with no recipe call and no row
    change, and the redelivered terminal event destroys the instance."""
    await seed()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    load = AsyncMock(
        side_effect=[RuntimeError("Failed to download driver x: ConnectError"), "/tmp/recipe"]
    )
    delete = AsyncMock(return_value=True)
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute, delete=delete):
            stack.enter_context(p)
        stack.enter_context(patch("app.services.driver_loader.load_driver", new=load))
        with pytest.raises(TransientUpstreamError) as excinfo:
            await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

        assert str(excinfo.value) == (
            f"recipe load failed for teardown of request {REQUEST_ID}: RuntimeError"
        )
        assert calls == []
        assert (await _rows())[0].status == ledger_status
        assert _formatted(caplog, "dynamic_instance_keyed_destroy_failed") == []

        # The redelivery: the package downloads this time and the destroy runs.
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert [c[0] for c in calls] == ["login", "destroy_instance", "logout"]
    assert (await _rows())[0].status == "DESTROYED"


async def test_teardown_broken_package_still_acks_with_row_live(caplog):
    """The permanent half of the split stays as it was: a DriverPackageError
    can never load, so teardown ACKs, leaves the row live, and logs the keyed
    action with reason recipe_load_failed."""
    await _seed_creating()
    calls, execute = _recipe_execute({"destroy_instance": DESTROY_OK})
    with caplog.at_level("INFO"), ExitStack() as stack:
        for p in _teardown_patches(execute):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.driver_loader.load_driver",
                new=AsyncMock(side_effect=DriverPackageError("broken")),
            )
        )
        await _execute_dynamic_teardown(RES_ID, USER_ID, _db_session_factory(), AsyncMock())

    assert calls == []
    assert (await _rows())[0].status == "CREATING"
    [record] = _formatted(caplog, "dynamic_instance_keyed_destroy_failed")
    assert record["reason"] == "recipe_load_failed"

