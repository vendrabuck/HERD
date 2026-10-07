"""The provisioning backstops' clock is provision_started_at (issue #997).

Both PENDING_PROVISION backstops of the expiration sweep (the dynamic timeout and
the physical-only restart revert) measure provision_timeout_seconds from when the
row ENTERED PENDING_PROVISION, not from updated_at, which any write moves (a
purpose-category PATCH is allowed in every status). Every transition into
PENDING_PROVISION stamps the column in the same statement; rows that predate
migration 0017 have NULL and fall back to updated_at. Uses app.database's own
engine because the sweep opens its sessions from it.
"""

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.reservation import Reservation, ReservationDynamicRequest, ReservationStatus
from app.schemas.reservation import DynamicRequestSpec, ReservationCreate
from app.services.reservation_service import create_reservation, set_purpose_category
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

EXP = "app.tasks.expiration"
SVC = "app.services.reservation_service"
OWNER = uuid.uuid4()
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def inventory():
    """Stub every inventory, cabling, and retry seam the sweep and create touch."""
    update = AsyncMock(side_effect=lambda ids, status, **_: list(ids))

    async def exclusive(ids):
        return [{"id": str(i), "exclusive": True} for i in ids]

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

    with (
        patch(f"{EXP}._update_device_statuses", new=update),
        patch(f"{SVC}._update_device_statuses", new=update),
        patch(f"{EXP}._fetch_devices_best_effort", new=exclusive),
        patch(f"{SVC}._fetch_devices_best_effort", new=exclusive),
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._validate_dynamic_requests", new=AsyncMock()),
        patch(f"{SVC}.retry_with_backoff", new=once),
        patch(f"{EXP}._create_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._create_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{EXP}._archive_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}._archive_reservation_fork_best_effort", new=AsyncMock()),
    ):
        yield update


async def _insert(*, dynamic, provision_started_at, updated_at, status=None):
    rid = uuid.uuid4()
    res = Reservation(
        id=rid,
        user_id=OWNER,
        device_ids=[str(uuid.uuid4())],
        topology_type="PHYSICAL",
        purpose="t",
        start_time=_now() - timedelta(hours=3),
        end_time=_now() + timedelta(hours=3),
        status=status or ReservationStatus.PENDING_PROVISION,
        provision_started_at=provision_started_at,
    )
    res.updated_at = updated_at
    if dynamic:
        res.dynamic_requests = [ReservationDynamicRequest(template_id=uuid.uuid4())]
    async with TestSessionLocal() as session:
        session.add(res)
        await session.commit()
    return rid


async def _row(rid):
    async with TestSessionLocal() as session:
        return (
            await session.execute(select(Reservation).where(Reservation.id == rid))
        ).scalar_one()


def _timed_out(dynamic):
    return ReservationStatus.FAILED if dynamic else ReservationStatus.PENDING


@pytest.mark.parametrize("dynamic", [True, False], ids=["dynamic_timeout", "restart_backstop"])
async def test_purpose_category_patch_does_not_move_the_backstop_deadline(inventory, dynamic):
    stale = _now() - timedelta(hours=2)
    rid = await _insert(dynamic=dynamic, provision_started_at=stale, updated_at=stale)
    async with TestSessionLocal() as db:
        res, forbidden = await set_purpose_category(db, rid, OWNER, "user", "training")
    assert res is not None and not forbidden
    row = await _row(rid)
    # The PATCH moved updated_at past the deadline; the clock did not move.
    assert row.updated_at.replace(tzinfo=timezone.utc) > _now() - timedelta(minutes=5)
    await _run_expiration_cycle()
    assert (await _row(rid)).status == _timed_out(dynamic)


@pytest.mark.parametrize("dynamic", [True, False], ids=["dynamic_timeout", "restart_backstop"])
async def test_backstop_reads_provision_started_at_not_updated_at(inventory, dynamic):
    """A fresh entry stamp keeps the row even when updated_at is old."""
    rid = await _insert(
        dynamic=dynamic,
        provision_started_at=_now() - timedelta(seconds=30),
        updated_at=_now() - timedelta(hours=2),
    )
    await _run_expiration_cycle()
    assert (await _row(rid)).status == ReservationStatus.PENDING_PROVISION


@pytest.mark.parametrize("dynamic", [True, False], ids=["dynamic_timeout", "restart_backstop"])
async def test_backstop_falls_back_to_updated_at_when_unstamped(inventory, dynamic):
    """Rows that predate migration 0017 carry NULL and are judged by updated_at."""
    stale_rid = await _insert(
        dynamic=dynamic, provision_started_at=None, updated_at=_now() - timedelta(hours=2)
    )
    fresh_rid = await _insert(
        dynamic=dynamic, provision_started_at=None, updated_at=_now() - timedelta(seconds=30)
    )
    await _run_expiration_cycle()
    assert (await _row(stale_rid)).status == _timed_out(dynamic)
    assert (await _row(fresh_rid)).status == ReservationStatus.PENDING_PROVISION


def _create_body(*, start, dynamic=False):
    return ReservationCreate(
        device_ids=[uuid.uuid4()],
        dynamic_requests=[DynamicRequestSpec(template_id=uuid.uuid4())] if dynamic else [],
        purpose="t",
        start_time=start,
        end_time=start + timedelta(hours=1),
    )


async def test_create_stamps_provision_started_at_when_it_enters_pending_provision(inventory):
    """A start-now dynamic booking stays PENDING_PROVISION: the insert stamped it."""
    before = _now()
    async with TestSessionLocal() as db:
        res = await create_reservation(db, _create_body(start=_now(), dynamic=True), OWNER, "tok")
    assert res.status == ReservationStatus.PENDING_PROVISION
    stamp = (await _row(res.id)).provision_started_at
    assert stamp is not None
    assert stamp.replace(tzinfo=timezone.utc) >= before - timedelta(seconds=1)


async def test_create_leaves_provision_started_at_null_for_pending_and_active(inventory):
    async with TestSessionLocal() as db:
        future = await create_reservation(
            db, _create_body(start=_now() + timedelta(days=1)), OWNER, "tok"
        )
    assert future.status == ReservationStatus.PENDING
    assert (await _row(future.id)).provision_started_at is None
    body = _create_body(start=_now())
    shared = [
        {
            "id": str(body.device_ids[0]),
            "name": "shared",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "exclusive": False,
        }
    ]
    with patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=shared)):
        async with TestSessionLocal() as db:
            active = await create_reservation(db, body, OWNER, "tok")
    assert active.status == ReservationStatus.ACTIVE
    assert (await _row(active.id)).provision_started_at is None


async def test_sweep_claim_stamps_provision_started_at(inventory):
    """A scheduled dynamic row is claimed and stays PENDING_PROVISION; the claim's
    UPDATE carried the stamp."""
    old = _now() - timedelta(hours=5)
    rid = await _insert(
        dynamic=True,
        provision_started_at=old,
        updated_at=old,
        status=ReservationStatus.PENDING,
    )
    before = _now()
    await _run_expiration_cycle()
    row = await _row(rid)
    assert row.status == ReservationStatus.PENDING_PROVISION
    assert row.provision_started_at.replace(tzinfo=timezone.utc) >= before - timedelta(seconds=1)


# --- Enumeration: every write that ENTERS PENDING_PROVISION stamps the clock ---


def _is_pending_provision(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "PENDING_PROVISION"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ReservationStatus"
    )


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _entry_sites():
    """(file, function, line) of every write that moves a row INTO PENDING_PROVISION.

    Recognized shapes: `<x>.status = ReservationStatus.PENDING_PROVISION`, an
    `initial_status = ReservationStatus.PENDING_PROVISION` choice, and
    `.values(status=ReservationStatus.PENDING_PROVISION)`. The compare-and-swap
    helpers called with PENDING_PROVISION as the target are checked separately.
    """
    sites = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in _functions(tree):
            for node in ast.walk(fn):
                hit = False
                if isinstance(node, ast.Assign) and _is_pending_provision(node.value):
                    for t in node.targets:
                        if (isinstance(t, ast.Attribute) and t.attr == "status") or (
                            isinstance(t, ast.Name) and t.id == "initial_status"
                        ):
                            hit = True
                elif isinstance(node, ast.Call) and any(
                    kw.arg == "status" and _is_pending_provision(kw.value) for kw in node.keywords
                ):
                    hit = True
                if hit:
                    stamps = any(
                        (isinstance(n, ast.Attribute) and n.attr == "provision_started_at")
                        or (isinstance(n, ast.keyword) and n.arg == "provision_started_at")
                        for n in ast.walk(fn)
                    )
                    sites.append((path.relative_to(APP_DIR).as_posix(), fn.name, stamps))
    return sites


def test_every_entry_into_pending_provision_stamps_provision_started_at():
    sites = _entry_sites()
    assert {(f, fn) for f, fn, _ in sites} == {
        ("services/reservation_service.py", "create_reservation"),
        ("tasks/expiration.py", "_run_expiration_cycle"),
    }, "a new write into PENDING_PROVISION must set provision_started_at (issue #997)"
    assert all(stamps for _, _, stamps in sites), sites


def test_cas_into_pending_provision_is_only_the_self_transition_guard():
    """A compare-and-swap whose target is PENDING_PROVISION must expect only
    PENDING_PROVISION (the issue #899 self-transition guard), so it never ENTERS
    the status and needs no stamp. A new entry CAS fails here."""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_claim_status_transition":
                continue
            target = node.args[3] if len(node.args) >= 4 else None
            for kw in node.keywords:
                if kw.arg == "new_status":
                    target = kw.value
            if target is not None and _is_pending_provision(target):
                offenders.append((path.name, node.lineno))
    assert offenders == []
