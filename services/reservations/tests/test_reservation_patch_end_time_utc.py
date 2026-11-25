"""PATCH end_time is judged and stored as the UTC instant it names, whatever its offset.

`ReservationUpdate.end_time` accepts any ISO 8601 offset, or none. The schema converts
it to UTC once (a naive value is taken as UTC, the project's standing decision) and
the service compares and stores that instant; the tests below require the outcome,
the stored value, the flag, and the payload to be a function of the instant alone.
They enumerate the space instead of sampling it: every offset form in FORMS against
every boundary of the window.

The service-level tests freeze the service's clock, so the boundaries at now are
exact. The route-level tests run the same conflict through the real PATCH route with
a second reservation. The real-Postgres proof is
test_reservation_patch_end_time_live_pg.py.
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
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from app.schemas.reservation import UTC_RANGE_TEXT, ReservationUpdate, to_utc_instant
from app.services.reservation_service import update_reservation
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from tests._harness import TestSessionLocal, override_bearer, override_get_db

SVC = "app.services.reservation_service"
UTC = timezone.utc
US = timedelta(microseconds=1)

OWNER = uuid.uuid4()
OTHER_OWNER = uuid.uuid4()
DEVICE = uuid.uuid4()

# The frozen "now" every service-level test runs at.
N = datetime(2031, 3, 14, 15, 0, 0, tzinfo=UTC)

# One instant, written every way a client can write it. "Z" and "+00:00" are the
# same offset in two spellings; "naive" carries none and is taken as UTC. -23:59 and
# +23:59 are the widest offsets the request parser accepts.
FORMS = {
    "-23:59": timezone(-timedelta(hours=23, minutes=59)),
    "-12:00": timezone(timedelta(hours=-12)),
    "-00:30": timezone(-timedelta(minutes=30)),
    "+00:00": UTC,
    "Z": UTC,
    "+05:30": timezone(timedelta(hours=5, minutes=30)),
    "+14:00": timezone(timedelta(hours=14)),
    "+23:59": timezone(timedelta(hours=23, minutes=59)),
    "naive": None,
}

OVERFLOW_TEXT = f"end_time must fall between {UTC_RANGE_TEXT} once converted to UTC"


def render(instant: datetime, form: str) -> str:
    """`instant` (aware) as the ISO 8601 text a client would send in `form`."""
    tz = FORMS[form]
    if tz is None:
        return instant.astimezone(UTC).replace(tzinfo=None).isoformat()
    text = instant.astimezone(tz).isoformat()
    return text.replace("+00:00", "Z") if form == "Z" else text


def _utc(value: datetime) -> datetime:
    """A stored value as an aware UTC datetime (SQLite returns it naive, in UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return N if tz is not None else N.replace(tzinfo=None)


@pytest.fixture
def frozen_now():
    with patch(f"{SVC}.datetime", _FrozenDatetime):
        yield N


def _device():
    return {
        "id": str(DEVICE),
        "name": "dut-utc",
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "exclusive": True,
    }


async def _insert(user_id, start, end, status=ReservationStatus.PENDING) -> uuid.UUID:
    rid = uuid.uuid4()
    async with TestSessionLocal() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=user_id,
                device_ids=[str(DEVICE)],
                topology_type=TopologyType.PHYSICAL,
                purpose="utc",
                start_time=start,
                end_time=end,
                status=status,
            )
        )
        await s.commit()
    return rid


async def _row(rid) -> Reservation:
    async with TestSessionLocal() as s:
        return (await s.execute(select(Reservation).where(Reservation.id == rid))).scalar_one()


async def _updated_events(rid) -> list[dict]:
    async with TestSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(OutboxEvent).where(OutboxEvent.subject == "herd.reservations.updated")
                )
            )
            .scalars()
            .all()
        )
    return [r.payload for r in rows if r.payload["reservation_id"] == str(rid)]


async def _live_overlaps() -> list[tuple]:
    """Every pair of live rows that share DEVICE over overlapping time."""
    async with TestSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(Reservation).where(
                        Reservation.status.in_(
                            [
                                ReservationStatus.ACTIVE,
                                ReservationStatus.PENDING,
                                ReservationStatus.PENDING_PROVISION,
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    spans = [(r.id, _utc(r.start_time), _utc(r.end_time)) for r in rows]
    return [
        (a[0], b[0])
        for i, a in enumerate(spans)
        for b in spans[i + 1 :]
        if a[1] < b[2] and b[1] < a[2]
    ]


async def _patch_end(rid, text: str):
    """Run the real schema and the real service on `text`; return the outcome."""
    data = ReservationUpdate.model_validate({"end_time": text})
    async with TestSessionLocal() as db:
        with patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=[_device()])):
            try:
                await update_reservation(db, rid, OWNER, data, token="t")
            except ValueError as exc:
                return ("400", str(exc))
            except LookupError as exc:
                return ("409", str(exc))
    return ("200", None)


# --- the normalization helper and the schema -------------------------------------


@pytest.mark.parametrize("form", list(FORMS))
def test_schema_converts_every_form_to_its_utc_instant(form):
    instant = datetime(2031, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    value = ReservationUpdate.model_validate({"end_time": render(instant, form)}).end_time
    assert value == instant
    assert value.utcoffset() == timedelta(0)
    assert value.isoformat() == "2031-01-01T00:00:00.000001+00:00"


def test_schema_leaves_an_absent_end_time_absent():
    assert ReservationUpdate.model_validate({"purpose": "x"}).end_time is None
    assert ReservationUpdate.model_validate({"end_time": None}).end_time is None


@pytest.fixture
def host_zone_far_from_utc(monkeypatch):
    """Run with the process's local zone at a fixed -12:00 (POSIX TZ, no zone files).

    The assertion below confirms the zone took effect, whatever zone the host running
    the suite is in.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX only")
    monkeypatch.setenv("TZ", "XST12")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_naive_end_time_is_utc_not_the_host_zone(host_zone_far_from_utc):
    assert datetime(2031, 1, 1).astimezone(UTC).hour == 12  # the fixture took effect
    value = ReservationUpdate.model_validate({"end_time": "2031-01-01T00:00:00"}).end_time
    assert value == datetime(2031, 1, 1, tzinfo=UTC)
    assert to_utc_instant(datetime(2031, 1, 1)) == datetime(2031, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "0001-01-01T00:00:00+05:00",
        "0001-01-01T04:59:59.999999+05:00",
        "9999-12-31T23:00:00-05:00",
        "9999-12-31T23:59:59.999999-00:30",
    ],
)
def test_an_end_time_with_no_utc_equivalent_is_a_validation_error(text):
    with pytest.raises(ValidationError) as caught:
        ReservationUpdate.model_validate({"end_time": text})
    errors = caught.value.errors()
    assert [(e["loc"], e["msg"]) for e in errors] == [(("end_time",), OVERFLOW_TEXT)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=UTC)),
        ("0001-01-01T05:00:00+05:00", datetime(1, 1, 1, tzinfo=UTC)),
        ("9999-12-31T23:59:59.999999Z", datetime.max.replace(tzinfo=UTC)),
        ("9999-12-31T18:59:59.999999-05:00", datetime.max.replace(tzinfo=UTC)),
        ("9999-12-31T23:59:59.999999", datetime.max.replace(tzinfo=UTC)),
    ],
)
def test_an_end_time_at_the_edge_of_the_utc_range_is_accepted(text, expected):
    assert ReservationUpdate.model_validate({"end_time": text}).end_time == expected


def test_to_utc_instant_raises_overflow_for_no_utc_equivalent():
    with pytest.raises(OverflowError):
        to_utc_instant(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))))


# --- update_reservation across the window, every form ----------------------------
#
# Owner's PENDING row [S, E); another PENDING row on the same exclusive device
# [B_START, B_START + 2h), one hour after E. Each boundary is rendered in every offset
# form from the same instant.

S = N + timedelta(hours=30)
E = S + timedelta(hours=10)
B_START = E + timedelta(hours=1)
B_END = B_START + timedelta(hours=2)


def _conflict(device=DEVICE):
    return f"Time conflict: devices ['{device}'] already reserved in the extended window"


AFTER_START = ("400", "end_time must be after start_time")
IN_FUTURE = ("400", "end_time must be in the future")
OK = ("200", None)

# (name, instant, expected outcome, expected end_time_changed when 200)
WINDOW_CASES = [
    ("start_minus_1us", S - US, AFTER_START, None),
    ("start", S, AFTER_START, None),
    ("start_plus_1us", S + US, OK, True),
    ("old_end_minus_1us", E - US, OK, True),
    ("old_end", E, OK, False),
    ("old_end_plus_1us", E + US, OK, True),
    ("other_start", B_START, OK, True),
    ("other_start_plus_1us", B_START + US, ("409", _conflict()), None),
    ("other_start_plus_2h", B_START + timedelta(hours=2), ("409", _conflict()), None),
    ("past_the_other_booking", B_END + timedelta(hours=1), ("409", _conflict()), None),
]


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize(
    ("name", "instant", "expected", "changed"), WINDOW_CASES, ids=[c[0] for c in WINDOW_CASES]
)
async def test_patch_end_time_is_judged_by_its_instant(
    frozen_now, form, name, instant, expected, changed
):
    mine = await _insert(OWNER, S, E)
    theirs = await _insert(OTHER_OWNER, B_START, B_END)

    outcome = await _patch_end(mine, render(instant, form))

    assert outcome == expected
    row = await _row(mine)
    events = await _updated_events(mine)
    if expected == OK:
        assert _utc(row.end_time) == instant
        assert [(e["end_time_changed"], e["end_time"]) for e in events] == [
            (changed, instant.isoformat() if changed else None)
        ]
    else:
        assert _utc(row.end_time) == E
        assert events == []
    other = await _row(theirs)
    assert (_utc(other.start_time), _utc(other.end_time)) == (B_START, B_END)
    assert await _live_overlaps() == []


# Another booking that starts AT the old end E: the extension check's lower
# bound is the old end itself and any extension at all triggers it.
# (name, the other booking's end, the new end, expected outcome)
ADJACENT_CASES = [
    ("one_microsecond_into_a_2h_booking", E + timedelta(hours=2), E + US, ("409", _conflict())),
    (
        "twenty_minutes_into_a_30min_booking",
        E + timedelta(minutes=30),
        E + timedelta(minutes=20),
        ("409", _conflict()),
    ),
    ("end_equal_to_the_old_end", E + timedelta(hours=2), E, OK),
]


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize(
    ("name", "other_end", "instant", "expected"),
    ADJACENT_CASES,
    ids=[c[0] for c in ADJACENT_CASES],
)
async def test_extension_into_a_booking_that_starts_at_the_old_end_is_refused(
    frozen_now, form, name, other_end, instant, expected
):
    mine = await _insert(OWNER, S, E)
    theirs = await _insert(OTHER_OWNER, E, other_end)

    assert await _patch_end(mine, render(instant, form)) == expected

    assert _utc((await _row(mine)).end_time) == E
    events = await _updated_events(mine)
    if expected == OK:
        assert [(e["end_time_changed"], e["end_time"]) for e in events] == [(False, None)]
    else:
        assert events == []
    other = await _row(theirs)
    assert (_utc(other.start_time), _utc(other.end_time)) == (E, other_end)
    assert await _live_overlaps() == []


# An ACTIVE row [N - 1h, N + 1h): the boundary is now itself.
NOW_CASES = [
    ("now_minus_1us", N - US, IN_FUTURE),
    ("now", N, IN_FUTURE),
    ("now_plus_1us", N + US, OK),
]


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize(("name", "instant", "expected"), NOW_CASES, ids=[c[0] for c in NOW_CASES])
async def test_patch_end_time_is_judged_against_now_by_its_instant(
    frozen_now, form, name, instant, expected
):
    start, end = N - timedelta(hours=1), N + timedelta(hours=1)
    mine = await _insert(OWNER, start, end, status=ReservationStatus.ACTIVE)

    assert await _patch_end(mine, render(instant, form)) == expected

    row = await _row(mine)
    assert _utc(row.end_time) == (instant if expected == OK else end)
    events = await _updated_events(mine)
    if expected == OK:
        assert [(e["end_time_changed"], e["end_time"]) for e in events] == [
            (True, instant.isoformat())
        ]
    else:
        assert events == []


CAP = 12 * 3600
CAP_CASES = [
    ("cap", S + timedelta(seconds=CAP), OK),
    (
        "cap_plus_1us",
        S + timedelta(seconds=CAP) + US,
        ("400", f"reservation duration exceeds the maximum of {CAP}s"),
    ),
]


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize(("name", "instant", "expected"), CAP_CASES, ids=[c[0] for c in CAP_CASES])
async def test_patch_end_time_is_judged_against_the_cap_by_its_instant(
    frozen_now, form, name, instant, expected
):
    mine = await _insert(OWNER, S, E)
    with patch("app.config.settings.reservation_max_duration_seconds", CAP):
        assert await _patch_end(mine, render(instant, form)) == expected
    assert _utc((await _row(mine)).end_time) == (instant if expected == OK else E)


# --- the window bounds the conflict query receives ---------------------------------
#
# asyncpg reads a naive datetime bound to a timestamptz in the process's local zone,
# so every bound the PATCH hands the conflict query must be an aware UTC instant.
# SQLite stores text and cannot show the difference, so these pin the bounds at the
# call; test_reservation_patch_end_time_live_pg.py proves the effect on Postgres.


@pytest.fixture
def window_calls():
    """Record the (start, end) of every conflict check while the real one still runs."""
    from app.services import reservation_service as svc

    real = svc._assert_no_window_conflicts
    calls = []

    async def spy(db, device_ids, start_time, end_time, **kwargs):
        calls.append((start_time, end_time, kwargs.get("window_label", "requested window")))
        return await real(db, device_ids, start_time, end_time, **kwargs)

    with patch(f"{SVC}._assert_no_window_conflicts", new=spy):
        yield calls


def _aware_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


@pytest.mark.parametrize("form", list(FORMS))
async def test_extension_check_receives_aware_utc_bounds(frozen_now, window_calls, form):
    mine = await _insert(OWNER, S, E)
    target = E + timedelta(minutes=20)

    assert await _patch_end(mine, render(target, form)) == OK

    assert window_calls == [(E, target, "extended window")]
    assert all(_aware_utc(v) for v in window_calls[0][:2])


ADDED = uuid.uuid4()


async def _patch_devices(rid, device_ids, end_text=None):
    body = {"device_ids": [str(d) for d in device_ids]}
    if end_text is not None:
        body["end_time"] = end_text
    data = ReservationUpdate.model_validate(body)
    devices = [
        {
            "id": str(d),
            "name": f"dut-{str(d)[:6]}",
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "exclusive": True,
        }
        for d in device_ids
    ]
    async with TestSessionLocal() as db:
        with (
            patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=devices)),
            patch(f"{SVC}._update_device_statuses", new=AsyncMock(return_value=[])),
        ):
            try:
                await update_reservation(db, rid, OWNER, data, token="t")
            except LookupError as exc:
                return ("409", str(exc))
    return ("200", None)


@pytest.mark.parametrize(
    ("name", "status", "start", "end", "expected_start"),
    [
        # ACTIVE since an hour ago: the window starts now.
        (
            "active",
            ReservationStatus.ACTIVE,
            N - timedelta(hours=1),
            N + timedelta(hours=2),
            N,
        ),
        # PENDING from tomorrow: the window starts at the row's start.
        ("pending", ReservationStatus.PENDING, S, E, S),
    ],
)
async def test_added_device_check_receives_aware_utc_bounds(
    frozen_now, window_calls, name, status, start, end, expected_start
):
    mine = await _insert(OWNER, start, end, status=status)

    assert await _patch_devices(mine, [DEVICE, ADDED]) == OK

    assert window_calls == [(expected_start, end, "requested window")]
    assert all(_aware_utc(v) for v in window_calls[0][:2])


@pytest.mark.parametrize("form", list(FORMS))
async def test_added_device_check_uses_the_new_end_in_utc(frozen_now, window_calls, form):
    mine = await _insert(OWNER, S, E)
    target = E + timedelta(minutes=20)

    assert await _patch_devices(mine, [DEVICE, ADDED], render(target, form)) == OK

    assert window_calls == [(E, target, "extended window"), (S, target, "requested window")]
    assert all(_aware_utc(v) for call in window_calls for v in call[:2])


# --- through the real PATCH route, a second reservation --------------------------


def _client_as(user_id) -> AsyncClient:
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(user_id),
        "username": "owner",
        "role": "user",
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def _route_patch(rid, text):
    async with _client_as(OWNER) as client:
        with patch(f"{SVC}._fetch_devices", new=AsyncMock(return_value=[_device()])):
            return await client.patch(f"/{rid}", json={"end_time": text})


@pytest.mark.parametrize("form", list(FORMS))
async def test_route_refuses_the_overlapping_extension_in_every_form(form):
    now = datetime.now(UTC).replace(microsecond=0)
    start = now + timedelta(hours=30)
    end = start + timedelta(hours=10)
    other = end + timedelta(hours=1)
    mine = await _insert(OWNER, start, end)
    theirs = await _insert(OTHER_OWNER, other, other + timedelta(hours=2))

    resp = await _route_patch(mine, render(other + timedelta(hours=2), form))

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": _conflict()}
    assert _utc((await _row(mine)).end_time) == end
    assert _utc((await _row(theirs)).end_time) == other + timedelta(hours=2)
    assert await _updated_events(mine) == []
    assert await _live_overlaps() == []


async def test_route_answers_the_same_for_one_instant_in_every_form():
    """Equivalent instants behave identically: same status, same body, same stored end."""
    now = datetime.now(UTC).replace(microsecond=0)
    start = now + timedelta(hours=30)
    end = start + timedelta(hours=10)
    other = end + timedelta(hours=1)
    target = end + timedelta(minutes=30)  # a real extension that stops short of `other`
    answers = {}
    for form in FORMS:
        mine = await _insert(OWNER, start, end)
        theirs = await _insert(OTHER_OWNER, other, other + timedelta(hours=2))
        resp = await _route_patch(mine, render(target, form))
        body = resp.json()
        answers[form] = (
            resp.status_code,
            body["end_time"],
            _utc((await _row(mine)).end_time),
            [(e["end_time_changed"], e["end_time"]) for e in await _updated_events(mine)],
        )
        async with TestSessionLocal() as s:  # free the device for the next form
            for rid in (mine, theirs):
                row = await s.get(Reservation, rid)
                row.status = ReservationStatus.CANCELLED
            await s.commit()
    # SQLite hands the refreshed row back naive, so the response carries no offset.
    expected = (200, target.replace(tzinfo=None).isoformat(), target, [(True, target.isoformat())])
    assert answers == dict.fromkeys(FORMS, expected)


@pytest.mark.parametrize("text", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"])
async def test_route_answers_422_for_an_end_time_with_no_utc_equivalent(text):
    now = datetime.now(UTC)
    mine = await _insert(OWNER, now + timedelta(hours=1), now + timedelta(hours=2))
    # Cap off, so nothing but the conversion can refuse the far end.
    with patch("app.config.settings.reservation_max_duration_seconds", 0):
        resp = await _route_patch(mine, text)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [{"type": "value_error", "loc": ["body", "end_time"], "msg": OVERFLOW_TEXT}]
    }
    assert _utc((await _row(mine)).end_time) == now + timedelta(hours=2)
    assert await _updated_events(mine) == []
