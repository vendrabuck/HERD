"""reservation.wiring_changed staging: the ledger anchor and the sweeper heal (#345 P3b).

ADR 0007 Decision 2 and 3. Covers, at the reservations boundary:

- The atomicity invariant: the outbox row exists if and only if the ledger advanced.
  A failure between the two writes commits neither.
- The save-path payload shape, including the per-wire fields relayed verbatim from
  cabling, and the ledger advancing to the saved version.
- Heal events carry released/built as null (the load-bearing full-reconcile marker);
  save events carry arrays.
- The sweeper stages a heal when cabling's latest fork_version exceeds the ledger
  (including a missing ledger row, treated as 0), stages nothing when in sync, and
  isolates a cabling fetch failure from the rest of the sweep.
- The save handler stages on a successful relay and stages nothing on the ARCHIVED 409.
- The issue #573 fault-injection seam gates: it fires only with BOTH
  HERD_FAULT_INJECTION set AND the reservation's purpose carrying the
  __herd_fault_stage_wiring__ sentinel; the closed-loop live proof that the sweep
  heal then converges the wiring lives in
  tests/integration/test_wiring_changed_reconcile.py.

The expiration sweep and stage_wiring_changed open their own AsyncSessionLocal against
the app engine, so this suite shares that engine (mirrors test_fork_archive_reconcile).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.fork_wiring_ledger import ForkWiringLedger
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from app.services import reservation_service
from app.services.reservation_service import (
    _FAULT_STAGE_WIRING_SENTINEL,
    _PRUNE_OUTCOME_CONVERGED,
    _PRUNE_OUTCOME_NO_FORK,
    WIRING_CHANGED_SUBJECT,
    _create_reservation_fork_best_effort,
    _prune_removed_devices_from_fork,
    stage_wiring_changed,
)
from app.tasks.expiration import _run_fork_archive_reconcile
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

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
    topology_id: uuid.UUID | None = None,
    purpose: str = "t",
) -> uuid.UUID:
    res = Reservation(
        user_id=user_id,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_id=topology_id,
        topology_type=TopologyType.PHYSICAL,
        purpose=purpose,
        start_time=NOW - timedelta(hours=1),
        end_time=NOW + timedelta(hours=2),
        status=status,
    )
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _wiring_rows() -> list[OutboxEvent]:
    async with TestSessionLocal() as db:
        return (
            (
                await db.execute(
                    select(OutboxEvent).where(OutboxEvent.subject == WIRING_CHANGED_SUBJECT)
                )
            )
            .scalars()
            .all()
        )


async def _ledger(reservation_id: uuid.UUID) -> ForkWiringLedger | None:
    async with TestSessionLocal() as db:
        return await db.get(ForkWiringLedger, reservation_id)


def _wire() -> dict:
    return {
        "device_a_id": str(uuid.uuid4()),
        "port_a": "a0",
        "device_b_id": str(uuid.uuid4()),
        "port_b": "b0",
        "layer": "L1",
        "physical_connection_id": str(uuid.uuid4()),
    }


# --- stage_wiring_changed: atomicity and payload shape -------------------------------


@pytest.mark.asyncio
async def test_atomicity_neither_persists_on_failure_between_writes():
    """The invariant, pinned: outbox row exists iff the ledger advanced.

    enqueue_event stages the outbox row first, then the ledger upsert raises before the
    single commit. Nothing is committed, so after a rollback neither row persists.
    """
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        with patch.object(
            reservation_service,
            "_upsert_wiring_ledger",
            new=AsyncMock(side_effect=RuntimeError("boom between writes")),
        ):
            with pytest.raises(RuntimeError):
                await stage_wiring_changed(db, rid, 2, released=[], built=[])
        await db.rollback()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


@pytest.mark.asyncio
async def test_save_staging_payload_and_ledger_advance():
    """A save stages one event with the exact payload and per-wire fields, ledger to N."""
    rid = uuid.uuid4()
    built = [_wire()]
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 2, released=[], built=built)

    rows = await _wiring_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.subject == "herd.reservations.wiring_changed"
    payload = row.payload
    assert payload["event"] == "reservation.wiring_changed"
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 2
    assert payload["released"] == []
    assert payload["built"] == built
    # enqueue_event stamps a stable event_id, and it matches the outbox row id.
    assert payload["event_id"] == str(row.id)
    # Every per-wire identity field survives verbatim.
    wire = payload["built"][0]
    assert set(wire) == {
        "device_a_id",
        "port_a",
        "device_b_id",
        "port_b",
        "layer",
        "physical_connection_id",
    }

    ledger = await _ledger(rid)
    assert ledger is not None
    assert ledger.last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_heal_staging_carries_null_delta():
    """A heal event carries released/built as null, not empty lists (Decision 2)."""
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 5, released=None, built=None)

    payload = (await _wiring_rows())[0].payload
    assert payload["released"] is None
    assert payload["built"] is None
    assert payload["fork_version"] == 5
    assert (await _ledger(rid)).last_staged_fork_version == 5


@pytest.mark.asyncio
async def test_upsert_advances_existing_ledger_row():
    """A second staging on an existing ledger advances it rather than inserting."""
    rid = uuid.uuid4()
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=1))
        await db.commit()
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 3, released=None, built=None)
    assert (await _ledger(rid)).last_staged_fork_version == 3


# --- Sweeper heal via _run_fork_archive_reconcile -----------------------------------


def _forks(*pairs: tuple[uuid.UUID, int]):
    return patch(
        "app.tasks.expiration._fetch_active_forks",
        AsyncMock(return_value=list(pairs)),
    )


@pytest.mark.asyncio
async def test_heal_stages_when_cabling_exceeds_missing_ledger():
    """A missing ledger row counts as 0, so an ACTIVE fork at v2 heals to v2."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with (
        _forks((rid, 2)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 2
    assert payload["released"] is None
    assert payload["built"] is None
    assert (await _ledger(rid)).last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_heal_stages_when_cabling_exceeds_existing_ledger():
    """An ACTIVE fork whose ledger sits behind cabling heals to cabling's latest."""
    rid = await _insert(ReservationStatus.ACTIVE)
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=1))
        await db.commit()

    with (
        _forks((rid, 3)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    rows = await _wiring_rows()
    assert len(rows) == 1
    assert rows[0].payload["fork_version"] == 3
    assert (await _ledger(rid)).last_staged_fork_version == 3


@pytest.mark.asyncio
async def test_no_heal_when_in_sync():
    """Ledger equal to cabling's latest stages nothing and leaves the ledger untouched."""
    rid = await _insert(ReservationStatus.ACTIVE)
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=2))
        await db.commit()

    with (
        _forks((rid, 2)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert (await _ledger(rid)).last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_terminal_reservation_is_not_healed():
    """A terminal (COMPLETED) reservation is archived, never wiring-healed."""
    rid = await _insert(ReservationStatus.COMPLETED)
    archive = AsyncMock()
    with (
        _forks((rid, 9)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", archive),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None
    archive.assert_awaited_once_with(rid)


@pytest.mark.asyncio
async def test_heal_isolated_from_cabling_fetch_failure():
    """A cabling fetch failure is non-fatal: the sweep returns cleanly, stages nothing."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with (
        patch(
            "app.tasks.expiration._fetch_active_forks",
            AsyncMock(side_effect=RuntimeError("cabling unreachable")),
        ),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
    ):
        await _run_fork_archive_reconcile()

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


# --- Initial-provision unification: activation stages the initial wiring (phase 7) ----


@pytest.mark.asyncio
async def test_activation_stages_initial_wiring_heal_after_fork_create():
    """After a successful fork create, activation stages a delta-less wiring_changed.

    ADR 0009 phase 7: initial provisioning unifies through the fork. The staged event
    for the fork's initial version (v1) carries released/built as null (the heal form),
    so the execution consumer full-reconciles against cabling's intended set, which for
    a fresh fork IS the initial wiring. The outbox row and the ledger advance together
    (the stage_wiring_changed atomicity invariant).
    """
    rid = uuid.uuid4()
    topology_id = uuid.uuid4()
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(return_value=1),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 1
    assert payload["released"] is None
    assert payload["built"] is None
    # Atomic with the outbox row: the ledger advanced to the staged version.
    assert (await _ledger(rid)).last_staged_fork_version == 1
    # The bool contract (issue #448 item 1): a real success returns True.
    assert result is True


@pytest.mark.asyncio
async def test_activation_stages_nothing_when_fork_create_fails():
    """A fork-create failure stages no wiring_changed and does not advance the ledger.

    The failure is swallowed (best-effort activation) and the sweep wiring-heal is the
    documented backstop: once the fork is lazily created, the heal stages the wiring.
    Here we pin only that activation itself leaves nothing staged.
    """
    rid = uuid.uuid4()
    topology_id = uuid.uuid4()
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(side_effect=RuntimeError("cabling down")),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None
    # The bool contract (issue #448 item 1): exhausted retries return False, the one
    # genuine failure signal the sweep's give-up counter relies on.
    assert result is False


@pytest.mark.asyncio
async def test_activation_stages_nothing_when_no_topology():
    """No parent topology means no fork and no initial wiring at activation (Case A)."""
    rid = uuid.uuid4()
    result = await _create_reservation_fork_best_effort(rid, None, created_by=str(USER_ID))
    assert await _wiring_rows() == []
    assert await _ledger(rid) is None
    # A null topology is a deliberate skip, not a failure (issue #448 item 1).
    assert result is True


@pytest.mark.asyncio
async def test_activation_staging_is_ledger_guarded():
    """A version already staged is not staged again by the activation helper.

    The idempotent fork re-create case (and the photo-finish with the sweep backstop):
    cabling returns the fork's current version, but the ledger already carries it, so
    the helper stages nothing and the outbox gains no duplicate row.
    """
    rid = uuid.uuid4()
    topology_id = uuid.uuid4()
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 1, released=None, built=None)
    assert len(await _wiring_rows()) == 1

    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(return_value=1),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    assert len(await _wiring_rows()) == 1
    assert (await _ledger(rid)).last_staged_fork_version == 1
    # The fork create itself succeeded (the ledger guard only skips the staging
    # sub-step), so the bool contract still reports True (issue #448 item 1).
    assert result is True


# --- Issue #573 fault-injection seam gates --------------------------------------------


@pytest.mark.asyncio
async def test_fault_seam_stages_nothing_when_both_gates_open(monkeypatch):
    """With HERD_FAULT_INJECTION set and the sentinel in the reservation's purpose,
    the activation staging call raises before it reaches stage_wiring_changed, so the
    existing except-and-log path swallows it exactly as a real staging failure would:
    the outbox gains no row, the ledger is left un-advanced, and the bool contract
    still reports True (fork creation itself succeeded; only the staging sub-step
    failed, mirroring the ledger-guard case above).
    """
    monkeypatch.setenv("HERD_FAULT_INJECTION", "1")
    rid = await _insert(
        ReservationStatus.ACTIVE,
        topology_id=uuid.uuid4(),
        purpose=f"x-{_FAULT_STAGE_WIRING_SENTINEL}-x",
    )
    topology_id = uuid.uuid4()
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(return_value=1),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    assert await _wiring_rows() == []
    assert await _ledger(rid) is None
    assert result is True


@pytest.mark.asyncio
async def test_fault_seam_inert_without_env_var(monkeypatch):
    """The sentinel alone, with HERD_FAULT_INJECTION unset, does not trip the seam:
    staging proceeds normally. This is the negative half of the double gate."""
    monkeypatch.delenv("HERD_FAULT_INJECTION", raising=False)
    rid = await _insert(
        ReservationStatus.ACTIVE,
        topology_id=uuid.uuid4(),
        purpose=f"x-{_FAULT_STAGE_WIRING_SENTINEL}-x",
    )
    topology_id = uuid.uuid4()
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(return_value=1),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    assert len(await _wiring_rows()) == 1
    assert (await _ledger(rid)).last_staged_fork_version == 1
    assert result is True


@pytest.mark.asyncio
async def test_fault_seam_inert_without_sentinel(monkeypatch):
    """HERD_FAULT_INJECTION set but no sentinel in the purpose: also inert, so the
    seam never affects an ordinary reservation on a stack where fault injection is
    enabled stack-wide (the dev/test compose override)."""
    monkeypatch.setenv("HERD_FAULT_INJECTION", "1")
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=uuid.uuid4())
    topology_id = uuid.uuid4()
    with patch(
        "app.services.reservation_service._create_reservation_fork",
        new=AsyncMock(return_value=1),
    ):
        result = await _create_reservation_fork_best_effort(
            rid, topology_id, created_by=str(USER_ID)
        )

    assert len(await _wiring_rows()) == 1
    assert (await _ledger(rid)).last_staged_fork_version == 1
    assert result is True


# --- Missing-fork sweep backstop (phase 7) -------------------------------------------


@pytest.mark.asyncio
async def test_sweep_creates_missing_fork_and_stages_initial_wiring():
    """An ACTIVE topology-carrying reservation with no cabling fork is backstopped.

    The fork-creation-failure case: activation's fork POST failed, so cabling lists no
    fork for the reservation. The sweep creates the fork through the same idempotent
    helper activation uses, and the initial delta-less wiring_changed is staged with
    the ledger advanced (ADR 0009 phase 7).
    """
    topology_id = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=topology_id)
    create = AsyncMock(return_value=1)
    with (
        _forks(),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
        patch("app.services.reservation_service._create_reservation_fork", new=create),
    ):
        await _run_fork_archive_reconcile()

    create.assert_awaited_once_with(rid, topology_id, str(USER_ID))
    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 1
    assert payload["released"] is None
    assert payload["built"] is None
    assert (await _ledger(rid)).last_staged_fork_version == 1


@pytest.mark.asyncio
async def test_sweep_backstop_skips_reservation_with_existing_fork():
    """A reservation cabling already holds a fork for is not re-created."""
    topology_id = uuid.uuid4()
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=topology_id)
    async with TestSessionLocal() as db:
        db.add(ForkWiringLedger(reservation_id=rid, last_staged_fork_version=1))
        await db.commit()
    create = AsyncMock(return_value=1)
    with (
        _forks((rid, 1)),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
        patch("app.services.reservation_service._create_reservation_fork", new=create),
    ):
        await _run_fork_archive_reconcile()

    create.assert_not_awaited()
    assert await _wiring_rows() == []


@pytest.mark.asyncio
async def test_sweep_backstop_skips_topologyless_and_non_active():
    """Case A (no parent topology) and non-ACTIVE reservations are never backstopped."""
    await _insert(ReservationStatus.ACTIVE, topology_id=None)
    await _insert(ReservationStatus.PENDING, topology_id=uuid.uuid4())
    await _insert(ReservationStatus.COMPLETED, topology_id=uuid.uuid4())
    create = AsyncMock(return_value=1)
    with (
        _forks(),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
        patch("app.services.reservation_service._create_reservation_fork", new=create),
    ):
        await _run_fork_archive_reconcile()

    create.assert_not_awaited()
    assert await _wiring_rows() == []


@pytest.mark.asyncio
async def test_sweep_backstop_one_failure_does_not_block_the_rest():
    """A fork-create failure for one reservation leaves the other backstopped."""
    topo_a, topo_b = uuid.uuid4(), uuid.uuid4()
    rid_a = await _insert(ReservationStatus.ACTIVE, topology_id=topo_a)
    rid_b = await _insert(ReservationStatus.ACTIVE, topology_id=topo_b)

    async def _create(reservation_id, topology_id, created_by=None):
        if reservation_id == rid_a:
            raise RuntimeError("cabling down for a")
        return 1

    with (
        _forks(),
        patch("app.tasks.expiration._archive_reservation_fork_best_effort", AsyncMock()),
        patch(
            "app.services.reservation_service._create_reservation_fork",
            new=AsyncMock(side_effect=_create),
        ),
    ):
        await _run_fork_archive_reconcile()

    rows = await _wiring_rows()
    assert [r.payload["reservation_id"] for r in rows] == [str(rid_b)]
    assert await _ledger(rid_a) is None
    assert (await _ledger(rid_b)).last_staged_fork_version == 1


# --- Save handler: stages on success, nothing on the ARCHIVED 409 -------------------


def _resp(status_code: int, json_body=None) -> httpx.Response:
    if json_body is None:
        return httpx.Response(status_code)
    return httpx.Response(status_code, json=json_body)


def _client_as(sub: str, role: str = "user") -> AsyncClient:
    app.dependency_overrides[get_db] = get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="fake-token"
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_save_handler_relays_body_and_stages_event():
    """A successful save relays the cabling body verbatim AND stages the event + ledger."""
    rid = await _insert(ReservationStatus.ACTIVE)
    built = [_wire()]
    released = [_wire()]
    save_body = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 4,
        "released": released,
        "built": built,
        "unchanged_count": 1,
    }
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(200, save_body)),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {"nodes": []}})

    assert resp.status_code == 200
    # The relay must not lose or change any field of the cabling save response.
    assert resp.json() == save_body

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["fork_version"] == 4
    assert payload["released"] == released
    assert payload["built"] == built
    assert (await _ledger(rid)).last_staged_fork_version == 4

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_staging_payload_carries_edge_key_verbatim():
    """Per-wire edge_key (issue #345 P3b) passes through the payload unchanged, null too.

    reservations relays cabling's delta wires verbatim, so the canvas edge_key each wire
    carries (or its null when a hop is ungrouped) must survive into the staged
    reservation.wiring_changed payload for the execution consumer to group hops per edge.
    """
    rid = uuid.uuid4()
    grouped = {**_wire(), "edge_key": "edge-42"}
    ungrouped = {**_wire(), "edge_key": None}
    async with TestSessionLocal() as db:
        await stage_wiring_changed(db, rid, 7, released=[ungrouped], built=[grouped])

    payload = (await _wiring_rows())[0].payload
    assert payload["built"][0]["edge_key"] == "edge-42"
    assert payload["released"][0]["edge_key"] is None


@pytest.mark.asyncio
async def test_save_handler_archived_409_stages_nothing():
    """An ARCHIVED-fork 409 from cabling relays the error and stages neither row."""
    rid = await _insert(ReservationStatus.ACTIVE)
    with patch(
        "app.routers.reservations._cabling_fork_call",
        new=AsyncMock(return_value=_resp(409, {"detail": "Fork is archived and cannot be edited"})),
    ):
        async with _client_as(str(USER_ID)) as ac:
            resp = await ac.post(f"/{rid}/fork/save", json={"canvas_data": {}})

    assert resp.status_code == 409
    assert await _wiring_rows() == []
    assert await _ledger(rid) is None

    app.dependency_overrides.clear()


# --- Decision 6 REMOVE half: PATCH-remove prunes the intended set (#459) ------------
#
# The prune is ONE cabling call (POST /internal/forks/{id}/prune-devices): cabling
# computes the release from fork_connections plus the last SAVED canvas, never the
# draft, and reservations only relays and stages. The which-rows-release rules
# (through-hops, pruned-edge far hops, stale edge_keys, no-op replays) are cabling's
# and are pinned in services/cabling/tests/test_fork_prune.py; this section pins the
# reservations side: the call shape, the outcome mapping, and the staging contract.


def _fork_resp(status_code, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = body or {}
    return resp


@pytest.mark.asyncio
async def test_patch_remove_calls_prune_devices_and_stages_released_delta():
    """The prune posts the removed ids to prune-devices EXACTLY once and stages the
    returned released delta (built empty) atomically with the ledger (Decision 6)."""
    rid = uuid.uuid4()
    dut_id = uuid.uuid4()
    released = [_wire()]
    prune_body = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 2,
        "changed": True,
        "released": released,
    }
    calls = []

    async def fork_call(method, path, json_body=None):
        calls.append((method, path, json_body))
        return _fork_resp(200, prune_body)

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(side_effect=fork_call),
    ):
        outcome = await _prune_removed_devices_from_fork(rid, [dut_id])

    assert outcome == _PRUNE_OUTCOME_CONVERGED
    assert calls == [
        ("POST", f"/internal/forks/{rid}/prune-devices", {"device_ids": [str(dut_id)]})
    ]

    rows = await _wiring_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reservation_id"] == str(rid)
    assert payload["fork_version"] == 2
    assert payload["released"] == released
    assert payload["built"] == []
    assert (await _ledger(rid)).last_staged_fork_version == 2


@pytest.mark.asyncio
async def test_patch_remove_unchanged_prune_stages_nothing():
    """A changed-false prune (idempotent replay, or no saved wiring for the devices)
    converges without staging: no event, no ledger row, no spurious version."""
    rid = uuid.uuid4()
    prune_body = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 3,
        "changed": False,
        "released": [],
    }

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_fork_resp(200, prune_body)),
    ):
        outcome = await _prune_removed_devices_from_fork(rid, [uuid.uuid4()])

    assert outcome == _PRUNE_OUTCOME_CONVERGED
    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


@pytest.mark.asyncio
async def test_patch_remove_no_fork_reports_no_fork_outcome():
    """A 404 (PENDING, Case A before lazy-create, or a failed activation fork create)
    stages nothing and reports NO_FORK so the marker logic can decide convergence."""
    rid = uuid.uuid4()

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_fork_resp(404)),
    ):
        outcome = await _prune_removed_devices_from_fork(rid, [uuid.uuid4()])

    assert outcome == _PRUNE_OUTCOME_NO_FORK
    assert await _wiring_rows() == []


@pytest.mark.asyncio
async def test_patch_remove_archived_409_converges_without_staging():
    """The prune endpoint's only 409 is an ARCHIVED fork (a pure release checks no
    port claims, issue #462): the reservation ended, terminal teardown owns the
    release, so the outcome converges and nothing is staged."""
    rid = uuid.uuid4()

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(
            return_value=_fork_resp(409, {"detail": "Fork is archived and cannot be edited"})
        ),
    ):
        outcome = await _prune_removed_devices_from_fork(rid, [uuid.uuid4()])

    assert outcome == _PRUNE_OUTCOME_CONVERGED
    assert await _wiring_rows() == []
    assert await _ledger(rid) is None


@pytest.mark.asyncio
async def test_patch_remove_5xx_raises_for_retry():
    """A cabling 5xx raises so the best-effort wrapper's retry loop (and the sweep's
    next tick) can see it; nothing is staged."""
    rid = uuid.uuid4()

    with patch(
        "app.services.reservation_service._cabling_fork_call",
        new=AsyncMock(return_value=_fork_resp(503, text="upstream down")),
    ):
        with pytest.raises(RuntimeError):
            await _prune_removed_devices_from_fork(rid, [uuid.uuid4()])

    assert await _wiring_rows() == []


@pytest.mark.asyncio
async def test_patch_remove_staging_failure_still_converges():
    """A staging failure after a committed prune converges anyway: cabling's version
    now outruns the ledger, so the sweep's delta-less heal covers it, and the marker
    may clear (the prune itself landed)."""
    rid = uuid.uuid4()
    prune_body = {
        "fork_id": str(uuid.uuid4()),
        "version_number": 2,
        "changed": True,
        "released": [_wire()],
    }

    with (
        patch(
            "app.services.reservation_service._cabling_fork_call",
            new=AsyncMock(return_value=_fork_resp(200, prune_body)),
        ),
        patch.object(
            reservation_service,
            "stage_wiring_changed",
            new=AsyncMock(side_effect=RuntimeError("stage boom")),
        ),
    ):
        outcome = await _prune_removed_devices_from_fork(rid, [uuid.uuid4()])

    assert outcome == _PRUNE_OUTCOME_CONVERGED
    assert await _wiring_rows() == []
