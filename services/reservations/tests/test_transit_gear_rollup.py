"""Unit tests for transit-gear inheritance in the device rollups (issue #646
phase 3, ADR 0013 "Delivery phases" point 3, D1-D3).

Exercises build_utilization_report's by_device / by_device_purpose folding of
cabling's fork-connection device union, and the chunking / fail-closed
behavior of the cabling batch call. Cabling itself is always mocked here: the
low-level transport is `app.services.reporting_service._cabling_fork_devices_batch`
and the chunk-and-merge layer above it is `_fetch_transit_devices`; the
directory-wide autouse fixture in tests/conftest.py stubs `_fetch_transit_devices`
to return {} by default, so every test below overrides it (or the lower-level
transport) explicitly for the behavior it wants to prove.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from app.config import settings
from app.models.reservation import Base, Reservation, ReservationStatus
from app.services.reporting_service import TransitGearUnavailable, build_utilization_report
from app.services.reporting_service import _fetch_transit_devices as _real_fetch_transit_devices
from herd_common.enums import TopologyType
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _reservation(
    user_id: uuid.UUID,
    owner_name: str,
    device_ids: list[str],
    start: datetime,
    end: datetime,
    status: ReservationStatus = ReservationStatus.COMPLETED,
    purpose_category: str | None = None,
) -> Reservation:
    return Reservation(
        id=uuid.uuid4(),
        user_id=user_id,
        owner_name=owner_name,
        device_ids=device_ids,
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=start,
        end_time=end,
        status=status,
        purpose_category=purpose_category,
    )


def _patch_transit(mapping: dict) -> "patch":
    """Patch _fetch_transit_devices to return the given reservation_id -> ids map."""
    return patch(
        "app.services.reporting_service._fetch_transit_devices",
        new=AsyncMock(return_value=mapping),
    )


@pytest.mark.asyncio
async def test_transit_device_inherits_category_and_hours(db_session):
    """One reservation, category training, reserved device A, fork devices {A, S}."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({r.id: [device_a, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].hours == pytest.approx(2.0)
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)

    assert by_device[switch].reservation_count == 1
    assert by_device[switch].hours == pytest.approx(2.0)
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].transit_hours == pytest.approx(2.0)

    by_device_purpose = {(b.device_id, b.purpose_category): b for b in report.by_device_purpose}
    a_bucket = by_device_purpose[(device_a, "training")]
    assert a_bucket.reservations == 1
    assert a_bucket.device_hours == pytest.approx(2.0)
    assert a_bucket.transit_reservations == 0
    assert a_bucket.transit_device_hours == pytest.approx(0.0)

    s_bucket = by_device_purpose[(switch, "training")]
    assert s_bucket.reservations == 1
    assert s_bucket.device_hours == pytest.approx(2.0)
    assert s_bucket.transit_reservations == 1
    assert s_bucket.transit_device_hours == pytest.approx(2.0)
    assert report.transit_included is True


@pytest.mark.asyncio
async def test_reserved_and_transit_dedupes_to_reserved_only(db_session):
    """A device both reserved and on the fork's path counts once, as reserved."""
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    # The fork touched device_a too (it is, after all, the endpoint the
    # reservation reserved), so the union is just {device_a}: nothing left
    # over once the reserved set is subtracted.
    with _patch_transit({r.id: [device_a]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_duplicate_device_ids_in_fork_response_count_once(db_session):
    """Many hops touching one switch must not inflate its transit_reservations."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    # Simulates three hops through the same switch: cabling's own endpoint
    # already dedupes, but the reporting layer must not assume that and
    # re-count per occurrence.
    with _patch_transit({r.id: [device_a, switch, switch, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].transit_hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_mixed_reserved_in_one_transit_in_another(db_session):
    """S reserved in reservation 1, transit in reservation 2, different categories."""
    switch = uuid.uuid4()
    device_b = uuid.uuid4()
    r1 = _reservation(
        USER_A,
        "alice",
        [str(switch)],
        NOW - timedelta(hours=4),
        NOW - timedelta(hours=3),
        purpose_category="training",
    )
    r2 = _reservation(
        USER_B,
        "bob",
        [str(device_b)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="qa_regression",
    )
    db_session.add_all([r1, r2])
    await db_session.commit()

    with _patch_transit({r1.id: [switch], r2.id: [device_b, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].reservation_count == 2
    assert by_device[switch].transit_reservations == 1
    assert by_device[switch].hours == pytest.approx(3.0)  # 1h reserved + 2h transit
    assert by_device[switch].transit_hours == pytest.approx(2.0)

    by_device_purpose = {(b.device_id, b.purpose_category): b for b in report.by_device_purpose}
    training_row = by_device_purpose[(switch, "training")]
    assert training_row.reservations == 1
    assert training_row.transit_reservations == 0
    qa_row = by_device_purpose[(switch, "qa_regression")]
    assert qa_row.reservations == 1
    assert qa_row.transit_reservations == 1
    assert qa_row.transit_device_hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_reservation_absent_from_cabling_map_has_no_transit(db_session):
    """A reservation with no fork (absent from the response map) reports reserved-only."""
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[device_a].reservation_count == 1
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].hours == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_include_transit_false_skips_the_cabling_call(db_session):
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = _reservation(
        USER_A,
        "alice",
        [str(device_a)],
        NOW - timedelta(hours=2),
        NOW,
        purpose_category="training",
    )
    db_session.add(r)
    await db_session.commit()

    fetch_mock = AsyncMock(return_value={r.id: [device_a, switch]})
    with patch("app.services.reporting_service._fetch_transit_devices", new=fetch_mock):
        report = await build_utilization_report(
            db_session,
            NOW - timedelta(days=1),
            NOW,
            [ReservationStatus.COMPLETED],
            include_transit=False,
        )

    fetch_mock.assert_not_called()
    assert report.transit_included is False
    by_device = {b.device_id: b for b in report.by_device}
    assert switch not in by_device
    assert by_device[device_a].transit_reservations == 0
    assert by_device[device_a].transit_hours == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_ai_suggested_only_reservation_contributes_no_device_purpose_transit(db_session):
    """A transit device follows the SAME is_confirmed gate as a reserved one:
    an ai_suggested-but-unconfirmed reservation folds into by_device's transit
    fields but never into by_device_purpose (mirroring the reserved-device rule)."""
    device_a = uuid.uuid4()
    switch = uuid.uuid4()
    r = Reservation(
        id=uuid.uuid4(),
        user_id=USER_A,
        owner_name="alice",
        device_ids=[str(device_a)],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=NOW - timedelta(hours=2),
        end_time=NOW,
        status=ReservationStatus.COMPLETED,
        purpose_category=None,
        purpose_suggestion={"top_category": "training", "distribution": []},
    )
    db_session.add(r)
    await db_session.commit()

    with _patch_transit({r.id: [device_a, switch]}):
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    by_device = {b.device_id: b for b in report.by_device}
    assert by_device[switch].transit_reservations == 1
    assert not any(b.device_id == switch for b in report.by_device_purpose)


# --- Chunking and fail-closed behavior -------------------------------------------
#
# These three tests exercise _fetch_transit_devices itself (the chunk-and-merge
# layer the rest of this file stubs out), so each one first un-stubs it back to
# the real implementation for its own body, then mocks the lower-level
# transport, _cabling_fork_devices_batch, instead.


def _real_transit_lookup():
    return patch(
        "app.services.reporting_service._fetch_transit_devices",
        new=_real_fetch_transit_devices,
    )


@pytest.mark.asyncio
async def test_chunking_splits_into_500_and_1(db_session):
    reservations = []
    now_hours = NOW - timedelta(minutes=1)
    for _ in range(501):
        r = _reservation(
            USER_A,
            "alice",
            [],
            now_hours - timedelta(hours=1),
            now_hours,
            purpose_category="other",
        )
        reservations.append(r)
    db_session.add_all(reservations)
    await db_session.commit()

    call_sizes: list[int] = []

    async def _fake_batch(reservation_ids):
        call_sizes.append(len(reservation_ids))
        return httpx.Response(200, json={"devices": {}})

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(side_effect=_fake_batch),
        ),
    ):
        await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    assert call_sizes == [500, 1]


@pytest.mark.asyncio
async def test_transport_failure_raises_transit_gear_unavailable(db_session):
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(side_effect=RuntimeError("Failed to contact cabling service: boom")),
        ),
    ):
        with pytest.raises(TransitGearUnavailable):
            await build_utilization_report(
                db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
            )


@pytest.mark.asyncio
async def test_non_200_raises_transit_gear_unavailable(db_session):
    device_a = uuid.uuid4()
    r = _reservation(
        USER_A, "alice", [str(device_a)], NOW - timedelta(hours=2), NOW, purpose_category="other"
    )
    db_session.add(r)
    await db_session.commit()

    with (
        _real_transit_lookup(),
        patch(
            "app.services.reporting_service._cabling_fork_devices_batch",
            new=AsyncMock(return_value=httpx.Response(500, text="boom")),
        ),
    ):
        with pytest.raises(TransitGearUnavailable):
            await build_utilization_report(
                db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
            )


# --- The real transport under respx (#1526) -----------------------------------------
#
# The tests above patch _cabling_fork_devices_batch, so it never runs in them. Below it
# runs, with call_service, and respx answers under httpx.

INTERNAL_TOKEN = "transit-internal-token"
BATCH_URL = f"{settings.cabling_service_url}/internal/forks/devices/batch"


@pytest.fixture
def cabling(monkeypatch):
    """respx under httpx with the internal token set; the test adds cabling's answer."""
    monkeypatch.setattr(settings, "internal_api_token", INTERNAL_TOKEN)
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _seed_chunks(db_session, count: int) -> list[Reservation]:
    """`count` COMPLETED rows in the window, each reserving one device of its own, plus
    a CANCELLED row in the window that must not be sent to cabling."""
    rows = [
        _reservation(USER_A, "alice", [str(uuid.uuid4())], NOW - timedelta(hours=1), NOW)
        for _ in range(count)
    ]
    cancelled = _reservation(
        USER_B,
        "bob",
        [str(uuid.uuid4())],
        NOW - timedelta(hours=1),
        NOW,
        status=ReservationStatus.CANCELLED,
    )
    db_session.add_all([*rows, cancelled])
    await db_session.commit()
    return rows


@pytest.mark.asyncio
async def test_transit_devices_of_every_chunk_are_merged(db_session, cabling):
    """OPS-REPORT-7 and OPS-REPORT-8 over two chunks (500 and 1), parsed from the
    real response bodies: one row of each chunk has a fork with its own transit
    switch, and both switches are counted. The two requests carry, between them,
    exactly the 501 counted rows' ids, one request 500 and the other 1 (their order is
    not pinned), and each is a POST to the batch
    path with the internal token, no `Authorization` header, and a 10 second timeout.
    The timeout is pinned as built: the spec's upstream table lists it, but no
    OPS-REPORT rule states it."""
    rows = await _seed_chunks(db_session, 501)
    reserved = {str(r.id): str(r.device_ids[0]) for r in rows}
    switch_one, switch_two = uuid.uuid4(), uuid.uuid4()
    unused_switches = [switch_one, switch_two]

    def _answer(request: httpx.Request) -> httpx.Response:
        # The first row each request names gets a fork through a switch of its own.
        ids = json.loads(request.content)["reservation_ids"]
        switch = unused_switches.pop(0)
        return httpx.Response(200, json={"devices": {ids[0]: [reserved[ids[0]], str(switch)]}})

    batch = cabling.post(BATCH_URL).mock(side_effect=_answer)
    with _real_transit_lookup():
        report = await build_utilization_report(
            db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
        )

    transit = {
        b.device_id: (b.transit_reservations, b.transit_hours)
        for b in report.by_device
        if b.transit_reservations
    }
    assert transit == {switch_one: (1, 1.0), switch_two: (1, 1.0)}
    requests = [call.request for call in batch.calls]
    sent_ids = [json.loads(r.content)["reservation_ids"] for r in requests]
    assert sorted(len(ids) for ids in sent_ids) == [1, 500]
    assert sorted(sent_ids[0] + sent_ids[1]) == sorted(str(r.id) for r in rows)
    for sent in requests:
        assert (sent.method, str(sent.url)) == ("POST", BATCH_URL)
        assert sent.headers["X-Internal-Token"] == INTERNAL_TOKEN
        assert "Authorization" not in sent.headers
        assert sent.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }


TRANSIT_FAILURES = [
    pytest.param(httpx.Response(201, json={"devices": {}}), id="201"),
    pytest.param(httpx.Response(404, json={"devices": {}}), id="404"),
    pytest.param(httpx.Response(500, json={"devices": {}}), id="500"),
    pytest.param(httpx.ConnectError("refused"), id="ConnectError"),
    pytest.param(httpx.ReadTimeout("slow"), id="ReadTimeout"),
    pytest.param(httpx.RemoteProtocolError("garbled"), id="RemoteProtocolError"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_size", [500, 1], ids=["the-500-chunk", "the-1-chunk"])
@pytest.mark.parametrize("failure", TRANSIT_FAILURES)
async def test_a_failure_from_any_chunk_raises_transit_gear_unavailable(
    db_session, cabling, failure, failing_size
):
    """OPS-REPORT-8: "a transport error or non-200 from any chunk fails the report
    closed". The 501 counted rows make a chunk of 500 and a chunk of 1, told apart by
    their size, not by the order they arrive in. The other chunk answers 200, and the
    failing one answers each failure class through the real transport, so the helper's
    own handling of an httpx error runs. Pinned: the failing chunk is asked and the
    report raises TransitGearUnavailable. Not pinned: the order of the requests, and
    whether the other chunk is asked at all once one has failed."""
    await _seed_chunks(db_session, 501)
    asked: list[int] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        size = len(json.loads(request.content)["reservation_ids"])
        asked.append(size)
        if size != failing_size:
            return httpx.Response(200, json={"devices": {}})
        if isinstance(failure, Exception):
            raise failure
        return failure

    cabling.post(BATCH_URL).mock(side_effect=_answer)
    with _real_transit_lookup():
        with pytest.raises(TransitGearUnavailable):
            await build_utilization_report(
                db_session, NOW - timedelta(days=1), NOW, [ReservationStatus.COMPLETED]
            )
    assert failing_size in asked


# --- A 200 of the wrong type (#1553) --------------------------------------------------
#
# These call the real `_fetch_transit_devices` directly with the transport patched to
# answer a built `httpx.Response(200)`, so only the body's shape is under test.

_BODY_TEXT = "body-text-that-must-stay-out-of-the-log"

# Bodies that are not an object holding a `devices` object: each fails the read closed.
WRONG_BODIES = {
    "list": [1, 2],
    "string": "x",
    "true": True,
    "int": 7,
    "null": None,
    "object_without_devices": {"other": {}},
    "empty_object": {},
    "devices_list": {"devices": ["rid"]},
    "devices_string": {"devices": "abc"},
    "devices_int": {"devices": 7},
    "devices_true": {"devices": True},
    "devices_null": {"devices": None},
}
# Bodies that are not JSON at all: also closed, and the decoder's text never travels.
NOT_JSON_BODIES = {
    "plain_text": _BODY_TEXT.encode(),
    "empty": b"",
    "truncated_object": b'{"devices": {',
    "not_utf8": b"\xff\xfe\xfa",
}


async def _fetch_with_answers(reservation_ids, answers):
    """Run the real fetch; the transport hands out `answers` one per chunk, in order."""
    queue = list(answers)

    async def _batch(chunk):
        return queue.pop(0)

    with patch(
        "app.services.reporting_service._cabling_fork_devices_batch",
        new=AsyncMock(side_effect=_batch),
    ):
        return await _real_fetch_transit_devices(reservation_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", list(WRONG_BODIES.values()), ids=list(WRONG_BODIES))
async def test_a_200_that_is_not_an_object_with_a_devices_object_is_unavailable(body):
    """OPS-REPORT-8 (issue #1553): the whole-body shapes of the issue's table, which
    answered 500 before (or, for `{}`, `null`, and `{"devices": null}`, silently
    reported no transit gear), raise TransitGearUnavailable. The body is encoded by
    hand: `httpx.Response(json=None)` would send no body at all, not `null`."""
    answer = httpx.Response(200, content=json.dumps(body).encode())
    assert answer.json() == body
    with pytest.raises(TransitGearUnavailable):
        await _fetch_with_answers([uuid.uuid4()], [answer])


@pytest.mark.asyncio
@pytest.mark.parametrize("content", list(NOT_JSON_BODIES.values()), ids=list(NOT_JSON_BODIES))
async def test_a_200_that_is_not_json_is_unavailable_without_its_text(content):
    """OPS-REPORT-8 (issue #1553): a 200 whose body does not parse raises
    TransitGearUnavailable, not the decoder's ValueError (which the report routes turn
    into a 422 quoting the decoder's text), and nothing about the body is chained."""
    with pytest.raises(TransitGearUnavailable) as raised:
        await _fetch_with_answers([uuid.uuid4()], [httpx.Response(200, content=content)])
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert _BODY_TEXT not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_chunk", [0, 1], ids=["the-500-chunk", "the-1-chunk"])
async def test_a_wrongly_shaped_body_in_any_chunk_is_unavailable(failing_chunk):
    """Every chunk's body is checked: a good first chunk does not excuse a bad second."""
    answers = [httpx.Response(200, json={"devices": {}}) for _ in range(2)]
    answers[failing_chunk] = httpx.Response(200, json=[1, 2])
    with pytest.raises(TransitGearUnavailable):
        await _fetch_with_answers([uuid.uuid4() for _ in range(501)], answers)


def _skip_records(caplog):
    return [
        r for r in caplog.records if getattr(r, "action", None) == "transit_gear_entries_skipped"
    ]


# A device list that is not a list (null is the pinned "adds nothing", not wrong).
WRONG_DEVICE_LISTS = {
    "int": 7,
    "string": "abc",
    "true": True,
    "float": 1.5,
    "object": {"k": _BODY_TEXT},
}
# A device id that is not a string.
WRONG_DEVICE_IDS = {
    "int": 7,
    "true": True,
    "null": None,
    "float": 1.5,
    "object": {"k": _BODY_TEXT},
    "list": [_BODY_TEXT],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_list", list(WRONG_DEVICE_LISTS.values()), ids=list(WRONG_DEVICE_LISTS)
)
async def test_a_device_list_of_the_wrong_type_is_skipped_and_logged_once(caplog, wrong_list):
    """OPS-REPORT-7 (issue #1553, owner decision): inside a well-formed map, a device
    list that is not a list is skipped (its reservation gets no transit devices, as a
    null list), the other reservation's devices still count, and ONE warning with a
    fixed `action` and the counts, and no body text, is logged. Before the fix a
    number answered 500 and a string was read character by character."""
    bad_rid, good_rid, switch = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    body = {"devices": {str(bad_rid): wrong_list, str(good_rid): [str(switch)]}}
    with caplog.at_level("DEBUG", logger="app.services.reporting_service"):
        devices = await _fetch_with_answers([bad_rid, good_rid], [httpx.Response(200, json=body)])
    assert devices == {good_rid: [switch]}
    records = _skip_records(caplog)
    assert [(r.levelname, r.skipped_device_lists, r.skipped_device_ids) for r in records] == [
        ("WARNING", 1, 0)
    ]
    assert _BODY_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_device_ids_of_the_wrong_type_are_skipped_and_the_rest_count(caplog):
    """OPS-REPORT-7 (issue #1553): every wrongly typed device id is skipped while the
    entry's UUID strings still count, on either side of each one; the report logs ONE
    warning counting all of them. Before the fix `uuid.UUID(7)` raised AttributeError
    and the report answered 500."""
    rid, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entry = [str(first), *WRONG_DEVICE_IDS.values(), "not-a-uuid", str(second)]
    with caplog.at_level("DEBUG", logger="app.services.reporting_service"):
        devices = await _fetch_with_answers(
            [rid], [httpx.Response(200, json={"devices": {str(rid): entry}})]
        )
    assert devices == {rid: [first, second]}
    records = _skip_records(caplog)
    assert [(r.skipped_device_lists, r.skipped_device_ids) for r in records] == [
        (0, len(WRONG_DEVICE_IDS))
    ]
    assert _BODY_TEXT not in caplog.text


@pytest.mark.asyncio
async def test_wrongly_typed_entries_across_chunks_log_one_line_per_report(caplog):
    """The skip is logged once per report, not once per chunk or entry: two chunks each
    with wrongly typed entries give one warning with the sums."""
    ids = [uuid.uuid4() for _ in range(501)]
    switch = uuid.uuid4()
    first = {"devices": {str(ids[0]): 7, str(ids[1]): [True, str(switch)]}}
    second = {"devices": {str(ids[500]): "abc"}}
    with caplog.at_level("DEBUG", logger="app.services.reporting_service"):
        devices = await _fetch_with_answers(
            ids, [httpx.Response(200, json=first), httpx.Response(200, json=second)]
        )
    assert devices == {ids[1]: [switch]}
    records = _skip_records(caplog)
    assert [(r.skipped_device_lists, r.skipped_device_ids) for r in records] == [(2, 1)]


@pytest.mark.asyncio
async def test_the_pinned_leniency_logs_nothing(caplog):
    """The leniency PR #1547 pinned is unchanged and is not a wrong type: a key that is
    not a UUID, a device id string that is not a UUID, and a null device list are
    ignored without a log line."""
    rid, null_rid, switch = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    body = {
        "devices": {
            "not-a-reservation-id": [str(uuid.uuid4())],
            str(rid): ["not-a-uuid", str(switch), ""],
            str(null_rid): None,
        }
    }
    with caplog.at_level("DEBUG", logger="app.services.reporting_service"):
        devices = await _fetch_with_answers([rid, null_rid], [httpx.Response(200, json=body)])
    assert devices == {rid: [switch], null_rid: []}
    assert _skip_records(caplog) == []
