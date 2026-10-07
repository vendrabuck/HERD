"""purpose_classify_requested_at is stamped at every terminal-transition site
(issue #646 phase 2, ADR 0013 point 8; test_fork_archive_reconcile.py is the
template this file mirrors). Idempotency (an already-set marker is never
overwritten) is also pinned here, and an AST enumeration fails when a terminal
status compare-and-swap in app/ has no matching stamp (issue #996).
"""

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationCreate
from app.services.reservation_service import (
    apply_provision_result,
    cancel_reservation,
    create_reservation,
    release_reservation,
)
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

# The expiration cycle opens its own AsyncSessionLocal against the app engine,
# so this suite must share that engine (mirrors test_expiration.py and
# test_fork_archive_reconcile.py).
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _insert(
    status: ReservationStatus,
    *,
    user_id: uuid.UUID = USER_ID,
    end_offset_h: float = 2,
    updated_at: datetime | None = None,
    dynamic: bool = False,
    purpose_classify_requested_at: datetime | None = None,
) -> uuid.UUID:
    res = Reservation(
        user_id=user_id,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_type=TopologyType.PHYSICAL,
        purpose="t",
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=end_offset_h),
        status=status,
        purpose_classify_requested_at=purpose_classify_requested_at,
    )
    if dynamic:
        res.dynamic_requests = [ReservationDynamicRequest(template_id=uuid.uuid4())]
    if updated_at is not None:
        res.updated_at = updated_at
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _get(rid: uuid.UUID) -> Reservation:
    async with TestSessionLocal() as db:
        return await db.get(Reservation, rid)


# --- Site 1: cancel_reservation -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_reservation_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await cancel_reservation(db, rid, USER_ID)
    res = await _get(rid)
    assert res.status == ReservationStatus.CANCELLED
    assert res.purpose_classify_requested_at is not None


# --- Site 2: release_reservation -------------------------------------------------------


@pytest.mark.asyncio
async def test_release_reservation_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await release_reservation(db, rid, USER_ID)
    res = await _get(rid)
    assert res.status == ReservationStatus.COMPLETED
    assert res.purpose_classify_requested_at is not None


# --- Site 3: apply_provision_result failure branch -------------------------------------


@pytest.mark.asyncio
async def test_provision_result_failed_stamps_marker():
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    with patch(
        "app.services.reservation_service._release_exclusive_devices_best_effort",
        AsyncMock(),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=False, device_ids=[], error="boom"
            )
    assert applied is True
    res = await _get(rid)
    assert res.status == ReservationStatus.FAILED
    assert res.purpose_classify_requested_at is not None


@pytest.mark.asyncio
async def test_provision_result_success_does_not_stamp_marker():
    """ACTIVE is not terminal: the success branch must not stamp the marker."""
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    with patch(
        "app.services.reservation_service._create_reservation_fork_best_effort",
        AsyncMock(),
    ):
        async with TestSessionLocal() as db:
            _, applied = await apply_provision_result(
                db, rid, succeeded=True, device_ids=[], error=None
            )
    assert applied is True
    res = await _get(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert res.purpose_classify_requested_at is None


# --- Site 4: expiration cycle auto-complete ---------------------------------------------


@pytest.mark.asyncio
async def test_expiry_autocomplete_stamps_marker():
    rid = await _insert(ReservationStatus.ACTIVE, end_offset_h=-1)  # already expired
    with (
        patch("app.tasks.expiration._fetch_devices_best_effort", AsyncMock(return_value=[])),
        patch("app.tasks.expiration._update_device_statuses", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_expiration_cycle()
    res = await _get(rid)
    assert res.status == ReservationStatus.COMPLETED
    assert res.purpose_classify_requested_at is not None


# --- Site 5: expiration cycle dynamic-timeout failure backstop --------------------------


@pytest.mark.asyncio
async def test_timeout_backstop_failed_stamps_marker():
    rid = await _insert(
        ReservationStatus.PENDING_PROVISION,
        dynamic=True,
        updated_at=NOW - timedelta(hours=1),  # older than provision_timeout deadline
    )
    with (
        patch("app.tasks.expiration._release_exclusive_devices_best_effort", AsyncMock()),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_expiration_cycle()
    res = await _get(rid)
    assert res.status == ReservationStatus.FAILED
    assert res.purpose_classify_requested_at is not None


# --- Idempotency: an already-set marker is never overwritten ---------------------------


@pytest.mark.asyncio
async def test_stamp_is_idempotent_on_cancel():
    already_requested = NOW - timedelta(days=1)
    rid = await _insert(ReservationStatus.ACTIVE, purpose_classify_requested_at=already_requested)
    with patch(
        "app.services.reservation_service._fetch_devices_best_effort",
        AsyncMock(return_value=[]),
    ):
        async with TestSessionLocal() as db:
            await cancel_reservation(db, rid, USER_ID)
    res = await _get(rid)
    # SQLite (the test backend) drops tzinfo on round-trip; compare naive.
    assert res.purpose_classify_requested_at.replace(tzinfo=None) == already_requested.replace(
        tzinfo=None
    )


# --- Site: create_reservation inventory-flip failure (issue #996) ---------------------

SVC = "app.services.reservation_service"


def _flip_failure_seams(dev, *, update):
    async def fetch(ids, token):
        return [
            {
                "id": str(i),
                "name": f"d-{str(i)[:4]}",
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "exclusive": True,
            }
            for i in ids
        ]

    async def once(fn, **_):
        return await fn()

    return (
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._update_device_statuses", new=update),
        patch(f"{SVC}.retry_with_backoff", new=once),
    )


class _PartialFlip:
    """RESERVED succeeds for the first device, then raises; records every write."""

    def __init__(self):
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        self.calls.append(([str(i) for i in ids], status))
        if status == "RESERVED":
            if succeeded is not None:
                succeeded.add(ids[0])
            raise RuntimeError("inventory down")
        return list(ids)


@pytest.mark.asyncio
async def test_create_flip_failure_stamps_marker():
    dev = uuid.uuid4()
    inv = _PartialFlip()
    p1, p2, p3 = _flip_failure_seams(dev, update=inv)
    body = ReservationCreate(
        device_ids=[dev],
        purpose="t",
        start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    with p1, p2, p3, pytest.raises(RuntimeError, match="Failed to reserve devices"):
        async with TestSessionLocal() as db:
            await create_reservation(db, body, USER_ID, "tok")
    async with TestSessionLocal() as db:
        res = (await db.execute(select(Reservation))).scalar_one()
    assert res.status == ReservationStatus.FAILED
    assert res.purpose_classify_requested_at is not None
    # The partially reserved device is reverted.
    assert inv.calls[-1] == ([str(dev)], "AVAILABLE")


@pytest.mark.asyncio
async def test_create_flip_failure_revert_skips_a_device_another_live_row_holds():
    """The create-path revert is holder-aware like every other release (issue #996)."""
    dev = uuid.uuid4()
    holder = await _insert(ReservationStatus.ACTIVE, end_offset_h=6)
    async with TestSessionLocal() as db:
        row = await db.get(Reservation, holder)
        row.device_ids = [dev]
        await db.commit()
    inv = _PartialFlip()
    p1, p2, p3 = _flip_failure_seams(dev, update=inv)
    body = ReservationCreate(
        device_ids=[dev],
        purpose="t",
        start_time=datetime.now(timezone.utc) + timedelta(hours=7),
        end_time=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    # The windows are disjoint, so the conflict check passes; a start grace wider
    # than the gap sends the booking down the immediate path, whose flip fails
    # while the ACTIVE holder still holds the device.
    from app.config import settings

    original = settings.reservation_start_grace_seconds
    settings.reservation_start_grace_seconds = 10 * 3600
    try:
        with p1, p2, p3, pytest.raises(RuntimeError):
            async with TestSessionLocal() as db:
                await create_reservation(db, body, USER_ID, "tok")
    finally:
        settings.reservation_start_grace_seconds = original
    assert ([str(dev)], "AVAILABLE") not in inv.calls


# --- Enumeration: every terminal status compare-and-swap stamps the marker -------------

APP_DIR = Path(__file__).resolve().parents[1] / "app"
_TERMINAL = {"COMPLETED", "CANCELLED", "FAILED"}
_CAS_HELPERS = {"_claim_status_transition", "_claim_provision_transition"}


def _cas_target(call: ast.Call):
    """The new_status argument of a CAS helper call."""
    for kw in call.keywords:
        if kw.arg == "new_status":
            return kw.value
    index = 3 if call.func.id == "_claim_status_transition" else 2
    return call.args[index] if len(call.args) > index else None


def _terminal_cas_and_stamps(fn):
    """(terminal CAS calls, stamp calls) inside one function.

    A literal ReservationStatus.<terminal> target counts; so does a bare name
    target that the function binds to an expression naming a terminal status
    (apply_provision_result's `target = ACTIVE if succeeded else FAILED`).
    """
    terminal_names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            names = {
                n.attr
                for n in ast.walk(node.value)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "ReservationStatus"
            }
            if names & _TERMINAL:
                terminal_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    cas = stamps = 0
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id == "stamp_purpose_classify_requested":
            stamps += 1
        elif node.func.id in _CAS_HELPERS:
            target = _cas_target(node)
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "ReservationStatus"
                and target.attr in _TERMINAL
            ) or (isinstance(target, ast.Name) and target.id in terminal_names):
                cas += 1
    return cas, stamps


def _sites():
    sites = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name not in (
                _CAS_HELPERS
            ):
                cas, stamps = _terminal_cas_and_stamps(fn)
                if cas or stamps:
                    sites[(path.relative_to(APP_DIR).as_posix(), fn.name)] = (cas, stamps)
    return sites


def test_every_terminal_status_write_stamps_the_marker():
    """Seven terminal-transition sites, each stamping exactly as often as it CASes
    into a terminal status. A new terminal write without a stamp fails here, and
    so does a new site nobody added to this list."""
    assert _sites() == {
        ("services/reservation_service.py", "create_reservation"): (1, 1),
        ("services/reservation_service.py", "apply_provision_result"): (1, 1),
        ("services/reservation_service.py", "cancel_reservation"): (1, 1),
        ("services/reservation_service.py", "release_reservation"): (1, 1),
        ("tasks/expiration.py", "_complete_expired_rows"): (1, 1),
        # The elapsed-window failure and the dynamic timeout failure.
        ("tasks/expiration.py", "_run_expiration_cycle"): (2, 2),
    }


def test_no_terminal_status_is_written_outside_the_cas_helpers():
    """An ORM `.status = <terminal>` or `.values(status=<terminal>)` would bypass
    both the compare-and-swap and the enumeration above."""

    def terminal(node):
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ReservationStatus"
            and node.attr in _TERMINAL
        )

    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Assign) and terminal(node.value):
                if any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets):
                    offenders.append((path.name, node.lineno))
            elif isinstance(node, ast.Call):
                if any(kw.arg == "status" and terminal(kw.value) for kw in node.keywords):
                    offenders.append((path.name, node.lineno))
    assert offenders == []
