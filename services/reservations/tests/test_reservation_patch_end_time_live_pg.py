"""Postgres-live proof that PATCH judges its window bounds as instants on a real timestamptz.

test_reservation_patch_end_time_utc.py enumerates the offsets and the window's
boundaries on SQLite, which stores a datetime as text with no offset. Postgres
stores `timestamptz` as an instant, so this file proves on the real column type,
through the real PATCH route with its session bound to Postgres, that:

  - an extension into another booking on the same exclusive device is
    refused with the extension conflict wording in each offset form tested, also
    when that booking starts exactly at the row's old end, and afterwards no two
    live rows overlap on that device;
  - a free extension written in any offset form stores the instant it names and
    stages `reservation.updated` with that instant in UTC;
  - adding a device to an ACTIVE row conflict-checks from the current UTC instant
    whatever the process time zone (asyncpg reads a naive datetime bound to a
    `timestamptz` in the process's local zone, so the service binds only aware ones).

Env contract and gate-ledger scoping follow test_reservation_patch_race_live_pg.py
(issue #819): every test owns two random users and a random device, snapshots every
other reservation (status and window) before and after, and deletes its own rows
and outbox events in a finally. Rows start 30 hours ahead so a live sweeper never
activates or completes them.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDevice,
    ReservationStatus,
    TopologyType,
)
from app.routers.reservations import bearer_scheme
from app.schemas.reservation import ReservationUpdate
from app.services.reservation_service import update_reservation
from fastapi.security import HTTPAuthorizationCredentials
from herd_common.pg_live_suite import pg_live_target
from httpx import ASGITransport, AsyncClient
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased

_PG = pg_live_target()
PG_DSN = _PG.dsn

_CONNECT_ARGS = {"server_settings": {"search_path": "reservations"}}


def _make_engine():
    return create_async_engine(PG_DSN, connect_args=_CONNECT_ARGS)


pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")


@pytest.fixture(autouse=True)
def _fail_when_pg_live_cannot_run():
    if _PG.failure is not None:
        pytest.fail(_PG.failure)


SVC = "app.services.reservation_service"
UTC = timezone.utc
LIVE = [
    ReservationStatus.ACTIVE,
    ReservationStatus.PENDING,
    ReservationStatus.PENDING_PROVISION,
]

FORMS = {
    "-12:00": timezone(timedelta(hours=-12)),
    "-00:30": timezone(-timedelta(minutes=30)),
    "+00:00": UTC,
    "Z": UTC,
    "+05:30": timezone(timedelta(hours=5, minutes=30)),
    "+14:00": timezone(timedelta(hours=14)),
    "naive": None,
}


def render(instant: datetime, form: str) -> str:
    """`instant` (aware) as the ISO 8601 text a client would send in `form`."""
    tz = FORMS[form]
    if tz is None:
        return instant.astimezone(UTC).replace(tzinfo=None).isoformat()
    text_value = instant.astimezone(tz).isoformat()
    return text_value.replace("+00:00", "Z") if form == "Z" else text_value


@pytest.fixture
async def pg_engine():
    engine = _make_engine()
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(pg_engine):
    # expire_on_commit=False mirrors herd_common.database.make_database.
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def scope(session_factory):
    """Two random owners and a random device; their rows are removed in the finally.

    Yields the scope and the snapshot of every other reservation taken before the
    test, for the untouched-rows assertion.
    """

    class Scope:
        owner = uuid.uuid4()
        other_owner = uuid.uuid4()
        device = uuid.uuid4()
        device_b = uuid.uuid4()
        now = datetime.now(UTC).replace(microsecond=0)

        async def my_ids(self):
            async with session_factory() as s:
                return set(
                    (
                        await s.execute(
                            select(Reservation.id).where(
                                Reservation.user_id.in_([self.owner, self.other_owner])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

        async def foreign_snapshot(self):
            async with session_factory() as s:
                rows = await s.execute(
                    select(
                        Reservation.id,
                        Reservation.status,
                        Reservation.start_time,
                        Reservation.end_time,
                    ).where(Reservation.user_id.notin_([self.owner, self.other_owner]))
                )
                return {r.id: (r.status, r.start_time, r.end_time) for r in rows}

    sc = Scope()
    before = await sc.foreign_snapshot()
    try:
        yield sc, before
    finally:
        mine = list(await sc.my_ids())
        async with session_factory() as s:
            if mine:
                await s.execute(
                    text("DELETE FROM reservation_devices WHERE reservation_id = ANY(:ids)"),
                    {"ids": mine},
                )
                await s.execute(
                    text("DELETE FROM outbox WHERE payload->>'reservation_id' = ANY(:ids)"),
                    {"ids": [str(i) for i in mine]},
                )
                await s.execute(
                    text("DELETE FROM reservations WHERE id = ANY(:ids)"),
                    {"ids": mine},
                )
            await s.commit()


async def _insert(
    session_factory, user_id, device, start, end, status=ReservationStatus.PENDING
) -> uuid.UUID:
    rid = uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=user_id,
                device_ids=[str(device)],
                topology_type=TopologyType.PHYSICAL,
                purpose="live pg end_time",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
        await s.commit()
    return rid


async def _window(session_factory, rid):
    async with session_factory() as s:
        row = (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()
    return row.start_time, row.end_time


async def _overlapping_pairs(session_factory, device) -> list:
    """Every pair of live rows sharing `device` over overlapping time, judged by Postgres."""
    a, b = aliased(Reservation), aliased(Reservation)
    da, db_ = aliased(ReservationDevice), aliased(ReservationDevice)
    stmt = (
        select(a.id, b.id)
        .join(da, da.reservation_id == a.id)
        .join(db_, and_(db_.device_id == da.device_id, db_.reservation_id != a.id))
        .join(b, b.id == db_.reservation_id)
        .where(
            da.device_id == device,
            a.status.in_(LIVE),
            b.status.in_(LIVE),
            a.start_time < b.end_time,
            b.start_time < a.end_time,
        )
    )
    async with session_factory() as s:
        return list((await s.execute(stmt)).all())


async def _updated_payloads(session_factory, rid) -> list[dict]:
    async with session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.subject == "herd.reservations.updated",
                        text("payload->>'reservation_id' = :rid").bindparams(rid=str(rid)),
                    )
                )
            )
            .scalars()
            .all()
        )
    return [r.payload for r in rows]


async def _route_patch(session_factory, sc, rid, end_text):
    """PATCH through the real app, its session bound to this Postgres."""

    async def pg_db():
        async with session_factory() as session:
            yield session

    device = {
        "id": str(sc.device),
        "name": "live-utc",
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "exclusive": True,
    }
    app.dependency_overrides[get_db] = pg_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(sc.owner),
        "username": "owner",
        "role": "user",
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="t"
    )
    try:
        with patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=[device])):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.patch(f"/{rid}", json={"end_time": end_text})
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("form", list(FORMS))
async def test_overlapping_extension_is_refused_in_every_offset_form(session_factory, scope, form):
    sc, foreign_before = scope
    start = sc.now + timedelta(hours=30)
    end = start + timedelta(hours=10)
    other_start = end + timedelta(hours=1)
    other_end = other_start + timedelta(hours=2)
    mine = await _insert(session_factory, sc.owner, sc.device, start, end)
    theirs = await _insert(session_factory, sc.other_owner, sc.device, other_start, other_end)

    resp = await _route_patch(session_factory, sc, mine, render(other_end, form))

    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": (
            f"Time conflict: devices ['{sc.device}'] already reserved in the extended window"
        )
    }
    assert await _window(session_factory, mine) == (start, end)
    assert await _window(session_factory, theirs) == (other_start, other_end)
    assert await _overlapping_pairs(session_factory, sc.device) == []
    assert await _updated_payloads(session_factory, mine) == []
    assert await sc.foreign_snapshot() == foreign_before


@pytest.mark.parametrize("form", list(FORMS))
async def test_free_extension_stores_the_instant_in_every_offset_form(session_factory, scope, form):
    sc, foreign_before = scope
    start = sc.now + timedelta(hours=30)
    end = start + timedelta(hours=10)
    other_start = end + timedelta(hours=1)
    other_end = other_start + timedelta(hours=2)
    target = end + timedelta(minutes=30, microseconds=1)
    mine = await _insert(session_factory, sc.owner, sc.device, start, end)
    theirs = await _insert(session_factory, sc.other_owner, sc.device, other_start, other_end)

    resp = await _route_patch(session_factory, sc, mine, render(target, form))

    assert resp.status_code == 200, resp.text
    assert datetime.fromisoformat(resp.json()["end_time"]) == target
    assert await _window(session_factory, mine) == (start, target)
    payloads = await _updated_payloads(session_factory, mine)
    assert [(p["end_time_changed"], p["end_time"]) for p in payloads] == [
        (True, target.isoformat())
    ]
    assert await _window(session_factory, theirs) == (other_start, other_end)
    assert await _overlapping_pairs(session_factory, sc.device) == []
    assert await sc.foreign_snapshot() == foreign_before


ADJACENT_CASES = [
    ("one_microsecond_into_a_2h_booking", timedelta(hours=2), timedelta(microseconds=1), 409),
    ("twenty_minutes_into_a_30min_booking", timedelta(minutes=30), timedelta(minutes=20), 409),
    ("end_equal_to_the_old_end", timedelta(hours=2), timedelta(0), 200),
]


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize(
    ("name", "other_length", "extension", "status_code"),
    ADJACENT_CASES,
    ids=[c[0] for c in ADJACENT_CASES],
)
async def test_extension_into_a_booking_that_starts_at_the_old_end_is_refused(
    session_factory, scope, form, name, other_length, extension, status_code
):
    sc, foreign_before = scope
    start = sc.now + timedelta(hours=30)
    end = start + timedelta(hours=10)
    mine = await _insert(session_factory, sc.owner, sc.device, start, end)
    theirs = await _insert(session_factory, sc.other_owner, sc.device, end, end + other_length)

    resp = await _route_patch(session_factory, sc, mine, render(end + extension, form))

    assert resp.status_code == status_code, resp.text
    payloads = await _updated_payloads(session_factory, mine)
    if status_code == 409:
        assert resp.json() == {
            "detail": (
                f"Time conflict: devices ['{sc.device}'] already reserved in the extended window"
            )
        }
        assert payloads == []
    else:
        assert [(p["end_time_changed"], p["end_time"]) for p in payloads] == [(False, None)]
    assert await _window(session_factory, mine) == (start, end)
    assert await _window(session_factory, theirs) == (end, end + other_length)
    assert await _overlapping_pairs(session_factory, sc.device) == []
    assert await sc.foreign_snapshot() == foreign_before


@pytest.fixture(params=["XST8", "XST-8"], ids=["utc_minus_8", "utc_plus_8"])
def process_zone(request, monkeypatch):
    """Run with the process's local zone at a fixed -08:00 or +08:00 (POSIX TZ), then restore.

    asyncpg encodes a datetime bound to a timestamptz with `astimezone`, which reads
    the process zone at call time, so a tzset() here takes effect in-process.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


async def _add_device(session_factory, sc, rid, device_ids):
    """PATCH `device_ids` onto `rid` through the real service; return the outcome."""

    async def fetch(ids, token):
        return [
            {
                "id": str(d),
                "name": f"live-{str(d)[:6]}",
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "exclusive": True,
            }
            for d in ids
        ]

    async with session_factory() as db:
        with (
            patch(f"{SVC}._fetch_devices", new=fetch),
            patch(f"{SVC}._update_device_statuses", new=AsyncMock(return_value=[])),
        ):
            try:
                await update_reservation(
                    db, rid, sc.owner, ReservationUpdate(device_ids=device_ids), token="t"
                )
            except LookupError as exc:
                return ("409", str(exc))
    return ("200", None)


async def _device_set(session_factory, rid):
    async with session_factory() as s:
        row = (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()
    return sorted(str(d) for d in row.device_ids)


async def test_added_device_is_checked_from_now_in_any_process_zone(
    session_factory, scope, process_zone
):
    """An ACTIVE row adding a device booked by another reservation from now + 1h is refused."""
    sc, foreign_before = scope
    assert datetime(2031, 1, 1).astimezone().utcoffset() != timedelta(0)  # the zone took
    now = datetime.now(UTC)
    mine = await _insert(
        session_factory,
        sc.owner,
        sc.device,
        now - timedelta(minutes=5),
        now + timedelta(hours=4),
        status=ReservationStatus.ACTIVE,
    )
    theirs = await _insert(
        session_factory,
        sc.other_owner,
        sc.device_b,
        now + timedelta(hours=1),
        now + timedelta(hours=3),
    )

    outcome = await _add_device(session_factory, sc, mine, [sc.device, sc.device_b])

    assert outcome == (
        "409",
        f"Time conflict: devices ['{sc.device_b}'] already reserved in the requested window",
    )
    assert await _device_set(session_factory, mine) == [str(sc.device)]
    assert await _window(session_factory, theirs) == (
        now + timedelta(hours=1),
        now + timedelta(hours=3),
    )
    assert await _overlapping_pairs(session_factory, sc.device_b) == []
    assert await sc.foreign_snapshot() == foreign_before


async def test_added_device_free_from_now_is_accepted_in_any_process_zone(
    session_factory, scope, process_zone
):
    """A device whose live booking ended before the row started is free to add."""
    sc, foreign_before = scope
    assert datetime(2031, 1, 1).astimezone().utcoffset() != timedelta(0)  # the zone took
    now = datetime.now(UTC)
    mine = await _insert(
        session_factory,
        sc.owner,
        sc.device,
        now - timedelta(minutes=5),
        now + timedelta(hours=4),
        status=ReservationStatus.ACTIVE,
    )
    # Ended ten minutes ago and not yet completed by a sweep: still a live row, but
    # it ends before `mine` starts, so the two never overlap. A stack's sweeper may
    # complete it first, which leaves the expected answer unchanged.
    await _insert(
        session_factory,
        sc.other_owner,
        sc.device_b,
        now - timedelta(hours=3),
        now - timedelta(minutes=10),
        status=ReservationStatus.ACTIVE,
    )

    assert await _add_device(session_factory, sc, mine, [sc.device, sc.device_b]) == ("200", None)

    assert await _device_set(session_factory, mine) == sorted([str(sc.device), str(sc.device_b)])
    assert await _overlapping_pairs(session_factory, sc.device_b) == []
    assert await sc.foreign_snapshot() == foreign_before
