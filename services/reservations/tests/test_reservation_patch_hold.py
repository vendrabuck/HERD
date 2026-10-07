"""PATCH on a reservation: hold rule, status guard, add check, and duration cap.

Issue #994: the device-set change commits first under a status guard (the status
the PATCH read must still hold at commit), and only then is inventory written,
with three attempts, removals through the ONE holder-aware release filter. A PATCH
that loses to a cancel or an activation claim keeps nothing; a cancel that commits
after the edit gets the added hold reverted.

Issue #999: adding a device to a PENDING reservation applies create's rule for a
future window: the window conflict check decides, not the device's status now.

Issue #995: PATCH applies RESERVATION_MAX_DURATION_SECONDS to the effective window
with create's own check and wording.

Uses app.database's own engine (like test_expiration_hold_invariant.py) so the one
test that runs the sweep's activation sees the same rows.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base, engine
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.schemas.reservation import ReservationUpdate
from app.services import reservation_service as svc
from app.services.reservation_service import ReservationStatusChanged, update_reservation
from app.tasks.expiration import _run_expiration_cycle
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

Session = async_sessionmaker(engine, expire_on_commit=False)

SVC = "app.services.reservation_service"
EXP = "app.tasks.expiration"
NOW = datetime.now(timezone.utc)
OWNER = uuid.uuid4()
HELD = uuid.uuid4()
OTHER = uuid.uuid4()

PENDING = ReservationStatus.PENDING
PROVISION = ReservationStatus.PENDING_PROVISION
ACTIVE = ReservationStatus.ACTIVE
CANCELLED = ReservationStatus.CANCELLED


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _device(device_id, status="AVAILABLE", exclusive=True):
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "topology_type": "PHYSICAL",
        "status": status,
        "exclusive": exclusive,
    }


async def _insert(status, device_ids, *, user_id=OWNER, start=None, end=None):
    rid = uuid.uuid4()
    if start is None:
        start = NOW - timedelta(minutes=5) if status == ACTIVE else NOW + timedelta(hours=1)
    if end is None:
        end = start + timedelta(hours=2)
    async with Session() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=user_id,
                device_ids=[str(d) for d in device_ids],
                topology_type=TopologyType.PHYSICAL,
                purpose="t",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
        await s.commit()
    return rid


async def _row(rid):
    async with Session() as s:
        return (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()


async def _subjects():
    async with Session() as s:
        rows = (await s.execute(select(OutboxEvent))).scalars().all()
    return sorted(r.subject for r in rows)


async def _set_status_on_second_session(rid, status):
    async with Session() as other:
        await other.execute(update(Reservation).where(Reservation.id == rid).values(status=status))
        await other.commit()


class Inventory:
    """Fake inventory status write: records calls, optionally fails or runs a hook."""

    def __init__(self, *, fail=False, on_reserved=None):
        self.calls: list[tuple[list[str], str]] = []
        self.fail = fail
        self.on_reserved = on_reserved

    async def __call__(self, ids, status, *, raise_on_failure=False, succeeded=None):
        self.calls.append(([str(i) for i in ids], status))
        if self.fail:
            if raise_on_failure:
                raise RuntimeError("inventory 503")
            return []
        if succeeded is not None:
            succeeded.update(ids)
        if status == "RESERVED" and self.on_reserved is not None:
            await self.on_reserved()
        return list(ids)


@pytest.fixture
def seams():
    """Stub the HTTP seams; retries keep their attempt count but do not sleep."""
    real_retry = svc.retry_with_backoff

    async def no_sleep_retry(fn, **kw):
        return await real_retry(fn, **{**kw, "initial_delay": 0, "max_delay": 0})

    async def fetch(ids, token):
        return [_device(d) for d in ids]

    async def best_effort(ids):
        return [_device(d) for d in ids]

    with (
        patch(f"{SVC}._fetch_devices", new=fetch),
        patch(f"{SVC}._fetch_devices_best_effort", new=best_effort),
        patch(f"{SVC}._prune_removed_devices_from_fork_best_effort", new=AsyncMock()),
        patch(f"{SVC}.retry_with_backoff", new=no_sleep_retry),
    ):
        yield


async def _patch(rid, **body):
    async with Session() as db:
        return await update_reservation(db, rid, OWNER, ReservationUpdate(**body), token="t")


def _actions(caplog, action):
    return [r for r in caplog.records if getattr(r, "action", None) == action]


# --- issue #994: inventory follows the committed edit ---


async def test_active_add_reserves_only_after_the_edit_committed(seams):
    rid = await _insert(ACTIVE, [HELD])
    seen_committed: list[set[str]] = []

    async def read_committed_set():
        row = await _row(rid)
        seen_committed.append({str(d) for d in row.device_ids})

    inv = Inventory(on_reserved=read_committed_set)
    with patch(f"{SVC}._update_device_statuses", new=inv):
        out = await _patch(rid, device_ids=[HELD, OTHER])
    assert inv.calls == [([str(OTHER)], "RESERVED")]
    assert seen_committed == [{str(HELD), str(OTHER)}]
    assert {str(d) for d in out.device_ids} == {str(HELD), str(OTHER)}
    assert await _subjects() == ["herd.reservations.updated"]


async def test_active_add_inventory_failure_is_retried_logged_and_the_edit_stands(seams, caplog):
    rid = await _insert(ACTIVE, [HELD])
    inv = Inventory(fail=True)
    with patch(f"{SVC}._update_device_statuses", new=inv), caplog.at_level(logging.ERROR):
        out = await _patch(rid, device_ids=[HELD, OTHER])
    assert inv.calls == [([str(OTHER)], "RESERVED")] * 3
    assert {str(d) for d in out.device_ids} == {str(HELD), str(OTHER)}
    assert {str(d) for d in (await _row(rid)).device_ids} == {str(HELD), str(OTHER)}
    assert await _subjects() == ["herd.reservations.updated"]
    (rec,) = _actions(caplog, "reservation_update_hold_failed")
    assert rec.device_ids == [str(OTHER)]
    assert rec.reservation_id == str(rid)


async def test_active_remove_inventory_failure_is_retried_logged_and_the_edit_stands(seams, caplog):
    rid = await _insert(ACTIVE, [HELD, OTHER])
    inv = Inventory(fail=True)
    with patch(f"{SVC}._update_device_statuses", new=inv), caplog.at_level(logging.ERROR):
        out = await _patch(rid, device_ids=[HELD])
    assert inv.calls == [([str(OTHER)], "AVAILABLE")] * 3
    assert [str(d) for d in out.device_ids] == [str(HELD)]
    assert await _subjects() == ["herd.reservations.updated"]
    (rec,) = _actions(caplog, "reservation_update_release_failed")
    assert rec.device_ids == [str(OTHER)]


async def test_active_remove_skips_a_device_another_live_row_holds(seams, caplog):
    rid = await _insert(ACTIVE, [HELD, OTHER])
    await _insert(ACTIVE, [OTHER], user_id=uuid.uuid4())
    inv = Inventory()
    with patch(f"{SVC}._update_device_statuses", new=inv), caplog.at_level(logging.INFO):
        await _patch(rid, device_ids=[HELD])
    assert inv.calls == []
    (rec,) = _actions(caplog, "release_skipped_device_held")
    assert rec.device_id == str(OTHER)


async def test_active_remove_of_a_non_exclusive_device_writes_nothing(seams):
    rid = await _insert(ACTIVE, [HELD, OTHER])

    async def shared(ids):
        return [_device(d, exclusive=False) for d in ids]

    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._fetch_devices_best_effort", new=shared),
    ):
        await _patch(rid, device_ids=[HELD])
    assert inv.calls == []


async def test_commit_failure_writes_nothing_to_inventory(seams):
    rid = await _insert(ACTIVE, [HELD, OTHER])
    inv = Inventory()
    with patch(f"{SVC}._update_device_statuses", new=inv):
        async with Session() as db:
            with (
                patch.object(db, "commit", side_effect=RuntimeError("commit lost")),
                pytest.raises(RuntimeError, match="commit lost"),
            ):
                await update_reservation(
                    db, rid, OWNER, ReservationUpdate(device_ids=[HELD, uuid.uuid4()]), token="t"
                )
    assert inv.calls == []
    assert {str(d) for d in (await _row(rid)).device_ids} == {str(HELD), str(OTHER)}
    assert await _subjects() == []


def _racing_claim(rid, winner_status):
    """Wrap the real CAS so a concurrent winner's write lands first, on a second session."""
    real = svc._claim_status_transition

    async def wrapper(db, reservation_id, expected, new_status):
        await _set_status_on_second_session(rid, winner_status)
        return await real(db, reservation_id, expected, new_status)

    return wrapper


@pytest.mark.parametrize(
    "observed,winner",
    [(ACTIVE, CANCELLED), (ACTIVE, ReservationStatus.COMPLETED), (PENDING, PROVISION)],
    ids=["active-vs-cancel", "active-vs-auto-complete", "pending-vs-activation-claim"],
)
async def test_patch_losing_its_status_guard_keeps_nothing(seams, caplog, observed, winner):
    """The winner commits while the PATCH waits on inventory's device fetch.

    The fetch is the PATCH's slow step, before anything is flushed. (The in-memory
    SQLite engine shares one connection between sessions, so a racing commit after
    the PATCH flushed would commit the PATCH's own pending SQL too; the real
    concurrent proof is test_reservation_patch_race_live_pg.py.)
    """
    rid = await _insert(observed, [HELD, OTHER])
    added = uuid.uuid4()
    inv = Inventory()
    prune = AsyncMock()

    async def fetch_while_the_winner_commits(ids, token):
        await _set_status_on_second_session(rid, winner)
        return [_device(d) for d in ids]

    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._fetch_devices", new=fetch_while_the_winner_commits),
        patch(f"{SVC}._prune_removed_devices_from_fork_best_effort", new=prune),
        caplog.at_level(logging.WARNING),
        pytest.raises(ReservationStatusChanged) as info,
    ):
        await _patch(rid, device_ids=[HELD, added], purpose="changed")
    assert str(info.value) == (
        f"Reservation changed status during the update ({observed.value} to {winner.value}); "
        "nothing was changed"
    )
    assert isinstance(info.value, LookupError)
    row = await _row(rid)
    assert row.status == winner
    assert {str(d) for d in row.device_ids} == {str(HELD), str(OTHER)}
    assert row.purpose == "t"
    assert row.pending_fork_prune_device_ids in (None, [])
    assert inv.calls == []
    prune.assert_not_called()
    assert await _subjects() == []
    assert _actions(caplog, "reservation_update_lost_race")


async def test_cancel_committing_after_the_edit_reverts_the_added_hold(seams, caplog):
    """The cancel released the row's devices before the added one was flipped."""
    rid = await _insert(ACTIVE, [HELD])

    async def cancel_now():
        await _set_status_on_second_session(rid, CANCELLED)

    inv = Inventory(on_reserved=cancel_now)
    with patch(f"{SVC}._update_device_statuses", new=inv), caplog.at_level(logging.WARNING):
        await _patch(rid, device_ids=[HELD, OTHER])
    assert inv.calls == [([str(OTHER)], "RESERVED"), ([str(OTHER)], "AVAILABLE")]
    assert _actions(caplog, "reservation_update_hold_reverted")


async def test_patch_route_answers_409_when_the_status_guard_loses(seams):
    """The router maps the lost guard to 409 with the pinned wording."""
    from app.database import get_db
    from app.dependencies.auth import get_current_user_payload
    from app.main import app
    from app.routers.reservations import bearer_scheme
    from httpx import ASGITransport, AsyncClient

    from tests._harness import override_bearer

    rid = await _insert(ACTIVE, [HELD])
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(OWNER),
        "username": "u",
        "role": "admin",
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    app.dependency_overrides[get_db] = _get_db
    try:
        with (
            patch(f"{SVC}._update_device_statuses", new=Inventory()),
            patch(f"{SVC}._claim_status_transition", new=_racing_claim(rid, CANCELLED)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                resp = await ac.patch(f"/{rid}", json={"purpose": "x"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json() == {
        "detail": (
            "Reservation changed status during the update (ACTIVE to CANCELLED); "
            "nothing was changed"
        )
    }


async def _get_db():
    async with Session() as s:
        yield s


# --- issue #999: PENDING add behaves like create for a future window ---


async def test_pending_add_accepts_a_device_reserved_now_but_free_in_the_window(seams):
    """RESERVED today by someone else, free in this row's window: create would accept it,
    so the edit does too (decided 2026-10-05). A PENDING row holds nothing, so the
    device's status today says nothing about the window, and no inventory is written."""
    rid = await _insert(PENDING, [HELD])
    await _insert(
        ACTIVE,
        [OTHER],
        user_id=uuid.uuid4(),
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(minutes=30),
    )

    async def busy_now(ids, token):
        return [_device(d, status="RESERVED") for d in ids]

    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._fetch_devices", new=busy_now),
    ):
        out = await _patch(rid, device_ids=[HELD, OTHER])
    assert {str(d) for d in out.device_ids} == {str(HELD), str(OTHER)}
    assert out.status == PENDING
    assert inv.calls == []


async def test_pending_add_refuses_a_device_booked_over_the_window_with_creates_wording(seams):
    rid = await _insert(PENDING, [HELD])
    row = await _row(rid)
    await _insert(
        PENDING,
        [OTHER],
        user_id=uuid.uuid4(),
        start=row.start_time + timedelta(minutes=30),
        end=row.end_time + timedelta(hours=1),
    )
    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        pytest.raises(LookupError) as info,
    ):
        await _patch(rid, device_ids=[HELD, OTHER])
    assert str(info.value) == (
        f"Time conflict: devices ['{OTHER}'] already reserved in the requested window"
    )
    assert inv.calls == []
    assert [str(d) for d in (await _row(rid)).device_ids] == [str(HELD)]


async def test_active_add_still_refuses_a_device_that_is_not_available_now(seams):
    rid = await _insert(ACTIVE, [HELD])

    async def busy_now(ids, token):
        return [_device(d, status="RESERVED") for d in ids]

    inv = Inventory()
    with (
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{SVC}._fetch_devices", new=busy_now),
        pytest.raises(ValueError, match=r"^The following devices are not available: dev-"),
    ):
        await _patch(rid, device_ids=[HELD, OTHER])
    assert inv.calls == []


async def test_device_added_to_a_pending_row_is_reserved_at_activation(seams):
    rid = await _insert(PENDING, [HELD])

    async def busy_now(ids, token):
        return [_device(d, status="RESERVED") for d in ids]

    with (
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
        patch(f"{SVC}._fetch_devices", new=busy_now),
    ):
        await _patch(rid, device_ids=[HELD, OTHER])

    # The window arrives: move the start into the past and run one sweep tick.
    async with Session() as s:
        await s.execute(
            update(Reservation)
            .where(Reservation.id == rid)
            .values(start_time=NOW - timedelta(seconds=30))
        )
        await s.commit()
    inv = Inventory()

    async def exclusive(ids):
        return [{"id": str(i), "exclusive": True} for i in ids]

    with (
        patch(f"{EXP}._update_device_statuses", new=inv),
        patch(f"{SVC}._update_device_statuses", new=inv),
        patch(f"{EXP}._fetch_devices_best_effort", new=exclusive),
        patch(f"{EXP}._create_reservation_fork_best_effort", new=AsyncMock()),
        patch(f"{EXP}._archive_reservation_fork_best_effort", new=AsyncMock()),
    ):
        await _run_expiration_cycle()
    assert (await _row(rid)).status == ACTIVE
    reserved = [set(ids) for ids, st in inv.calls if st == "RESERVED"]
    assert reserved == [{str(HELD), str(OTHER)}]


# --- issue #995: the duration cap applies to the effective window ---

CAP = 7200


@pytest.fixture
def cap():
    with patch("app.config.settings.reservation_max_duration_seconds", CAP):
        yield CAP


@pytest.mark.parametrize(
    "extra_seconds,ok",
    [(0, True), (1, False), (400 * 24 * 3600, False)],
    ids=["exactly-the-cap", "cap-plus-one-second", "far-over"],
)
async def test_patch_end_time_is_judged_against_the_cap(seams, cap, extra_seconds, ok):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(hours=1))
    new_end = start + timedelta(seconds=CAP + extra_seconds)
    with patch(f"{SVC}._update_device_statuses", new=Inventory()):
        if ok:
            out = await _patch(rid, end_time=new_end)
            assert out.end_time.replace(tzinfo=timezone.utc) == new_end
        else:
            with pytest.raises(ValueError) as info:
                await _patch(rid, end_time=new_end)
            assert str(info.value) == f"reservation duration exceeds the maximum of {CAP}s"
            assert await _subjects() == []


async def test_patch_cap_uses_the_stored_start_on_an_active_row(seams, cap):
    """An ACTIVE row's window is measured from its stored start, not from now."""
    start = NOW - timedelta(minutes=30)
    rid = await _insert(ACTIVE, [HELD], start=start, end=start + timedelta(hours=1))
    with (
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
        pytest.raises(ValueError, match=f"maximum of {CAP}s"),
    ):
        await _patch(rid, end_time=start + timedelta(seconds=CAP + 1))


async def test_over_cap_legacy_row_stays_editable_outside_its_window(seams, cap):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(seconds=CAP * 5))
    with patch(f"{SVC}._update_device_statuses", new=Inventory()):
        out = await _patch(rid, purpose="renamed", device_ids=[HELD, OTHER])
    assert out.purpose == "renamed"
    assert {str(d) for d in out.device_ids} == {str(HELD), str(OTHER)}


async def test_over_cap_legacy_row_cannot_set_a_window_still_over_the_cap(seams, cap):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(seconds=CAP * 5))
    with (
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
        pytest.raises(ValueError, match=f"maximum of {CAP}s"),
    ):
        await _patch(rid, end_time=start + timedelta(seconds=CAP * 2))


async def test_cap_zero_disables_the_patch_check(seams):
    start = NOW + timedelta(hours=1)
    rid = await _insert(PENDING, [HELD], start=start, end=start + timedelta(hours=1))
    with (
        patch("app.config.settings.reservation_max_duration_seconds", 0),
        patch(f"{SVC}._update_device_statuses", new=Inventory()),
    ):
        out = await _patch(rid, end_time=start + timedelta(days=400))
    assert out.end_time.replace(tzinfo=timezone.utc) == start + timedelta(days=400)
