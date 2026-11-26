"""OPS-REPORT-3, OPS-REPORT-7, OPS-REPORT-8, and OPS-REPORT-9 on both report routes (#1526).

`GET /reports/utilization` (JSON) and `GET /reports/utilization.csv` each pick their
own status defaults and each map a transit failure to 503, so every test here runs
against both routes through one parametrization, and the two must agree.

What is replaced: the dependency overrides of `get_db` (the test database of
tests/_harness.py), the caller's claims (an admin), and `bearer_scheme`, through the
directory's `make_client` fixture; the internal token setting; and, in the transit tests only, the
directory's autouse stub of `reporting_service._fetch_transit_devices`
(tests/conftest.py), which those tests put back to the real function. Everything else
between the router and the network runs, `call_service` included, and respx answers
under httpx and records every request that leaves the service: inventory's device
list, auth's group lookup, execution's run count, and cabling's transit batch. The
status tests keep the conftest stub, so they make no cabling call.
"""

import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx
from app.config import settings
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.services.reporting_service import _fetch_transit_devices as _real_fetch_transit_devices

from tests._harness import TestSessionLocal

ADMIN = {"sub": str(uuid.uuid4()), "username": "report-admin", "role": "admin"}
CALLER_BEARER = "Bearer report-caller-token"
TOKEN = "report-internal-token"

WINDOW_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(days=10)
# Every seeded row starts here, so a row of more than 24 hours spans two UTC days.
ROW_START = WINDOW_START + timedelta(days=1)
DAY_ONE = ROW_START.date().isoformat()
DAY_TWO = (ROW_START.date() + timedelta(days=1)).isoformat()

CABLING_BATCH_URL = f"{settings.cabling_service_url}/internal/forks/devices/batch"
CSV_SECTIONS = [
    "user",
    "device",
    "fleet",
    "purpose",
    "user_purpose",
    "device_purpose",
    "purpose_suggested",
]
ROUTES = ["json", "csv"]


@pytest.fixture
def network(monkeypatch):
    """respx under httpx for every outbound call, with the internal token set.

    Inventory answers from `network.inventory` (a list the test fills, one page, its
    length as `total`), auth answers that no user is in a group, and execution answers
    a run count of zero. Cabling has no route until a test adds one, so a cabling call
    a test did not expect fails it.
    """
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    with respx.mock(assert_all_called=False) as router:
        router.inventory = []

        def _inventory(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"items": router.inventory, "total": len(router.inventory)}
            )

        router.get(url__startswith=f"{settings.inventory_service_url}/devices").mock(
            side_effect=_inventory
        )
        router.post(f"{settings.auth_service_url}/groups/users/groups").mock(
            return_value=httpx.Response(200, json={})
        )
        router.get(url__startswith=f"{settings.execution_service_url}/runs").mock(
            return_value=httpx.Response(200, json={"total": 0})
        )
        yield router


@pytest.fixture
def real_transit_lookup():
    """Put the real `_fetch_transit_devices` back over the conftest stub."""
    with patch(
        "app.services.reporting_service._fetch_transit_devices",
        new=_real_fetch_transit_devices,
    ):
        yield


async def _insert(
    *,
    status: ReservationStatus,
    device_ids: list[uuid.UUID],
    hours: float,
    owner_name: str,
    start: datetime = ROW_START,
) -> Reservation:
    async with TestSessionLocal() as db:
        res = Reservation(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            owner_name=owner_name,
            device_ids=[str(d) for d in device_ids],
            topology_id=None,
            topology_type=TopologyType.PHYSICAL,
            start_time=start,
            end_time=start + timedelta(hours=hours),
            status=status,
        )
        db.add(res)
        await db.commit()
        return res


def _params(status: list[ReservationStatus] | None, **extra) -> list[tuple[str, str]]:
    params = [("start", WINDOW_START.isoformat()), ("end", WINDOW_END.isoformat())]
    params += [("status", s.value) for s in status or []]
    params += [(key, str(value)) for key, value in extra.items()]
    return params


async def _get(client, route: str, status, section: str = "device", **extra) -> httpx.Response:
    if route == "json":
        return await client.get(
            "/reports/utilization",
            params=_params(status, **extra),
            headers={"Authorization": CALLER_BEARER},
        )
    return await client.get(
        "/reports/utilization.csv",
        params=_params(status, section=section, **extra),
        headers={"Authorization": CALLER_BEARER},
    )


def _csv_rows(resp: httpx.Response) -> list[dict]:
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.text)))


# --- OPS-REPORT-3: the status defaults and an explicit status list ---------------------

# One reservation per status, each with its own owner and device and a power-of-two
# number of hours, so every set of statuses has its own hour total.
STATUS_HOURS = {status: float(2**i) for i, status in enumerate(ReservationStatus)}

STATUS_CASES = (
    [None]
    + [[status] for status in ReservationStatus]
    + [[ReservationStatus.PENDING, ReservationStatus.CANCELLED], list(ReservationStatus)]
)


def _status_case_id(case) -> str:
    return "default" if case is None else "+".join(s.value for s in case)


async def _seed_one_row_per_status() -> dict[ReservationStatus, tuple[str, str]]:
    """{status: (owner name, device id)} for one in-window row per status."""
    rows = {}
    for status in ReservationStatus:
        device = uuid.uuid4()
        owner = f"owner-{status.value.lower()}"
        await _insert(
            status=status, device_ids=[device], hours=STATUS_HOURS[status], owner_name=owner
        )
        rows[status] = (owner, str(device))
    return rows


async def _read_sections(client, route: str, status) -> dict:
    """The user, device, purpose, and fleet sections as {key: (hours, count)}.

    The JSON route answers all four in one request; the CSV route is asked once per
    section, so each CSV request runs its own status defaults.
    """
    if route == "json":
        resp = await _get(client, "json", status)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return {
            "user": {
                b["owner_name"]: (b["hours"], b["reservation_count"]) for b in body["by_user"]
            },
            "device": {
                b["device_id"]: (b["hours"], b["reservation_count"]) for b in body["by_device"]
            },
            "purpose": {
                b["purpose_category"]: (b["device_hours"], b["reservations"])
                for b in body["by_purpose"]
            },
            "fleet": {
                d["device_id"]: (d["hours"], d["reservation_count"])
                for d in body["fleet"]["devices"]
            },
            "json": body,
        }
    user = _csv_rows(await _get(client, "csv", status, section="user"))
    device = _csv_rows(await _get(client, "csv", status, section="device"))
    purpose = _csv_rows(await _get(client, "csv", status, section="purpose"))
    fleet = _csv_rows(await _get(client, "csv", status, section="fleet"))
    return {
        "user": {r["owner_name"]: (float(r["hours"]), int(r["reservation_count"])) for r in user},
        "device": {
            r["device_id"]: (float(r["hours"]), int(r["reservation_count"])) for r in device
        },
        "purpose": {
            r["purpose_category"]: (float(r["device_hours"]), int(r["reservations"]))
            for r in purpose
        },
        "fleet": {r["device_id"]: (float(r["hours"]), int(r["reservation_count"])) for r in fleet},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", STATUS_CASES, ids=_status_case_id)
@pytest.mark.parametrize("route", ROUTES)
async def test_each_section_counts_the_rule_s_statuses_on_both_routes(
    network, make_client, route, status
):
    """OPS-REPORT-3, for no `status`, each single status, a pair, and every status.

    Without `status` the user, device, and purpose sections count only the COMPLETED
    row and the fleet section the ACTIVE and COMPLETED rows; a given list applies to
    every section. Every row is in the window, and each has its own status, owner,
    device, and hour count, so a section that counted the wrong statuses shows another
    owner, another device, or another hour total. The fleet lists every inventory
    device, so a device whose row is not counted is there with 0 hours. On the JSON
    route the totals and the topology type, day, and group sections are checked as
    well; the CSV route does not serve them.
    """
    rows = await _seed_one_row_per_status()
    network.inventory = [
        {"id": device, "name": f"dev-{s.value.lower()}", "status": "AVAILABLE"}
        for s, (_, device) in rows.items()
    ]
    legacy = {ReservationStatus.COMPLETED} if status is None else set(status)
    fleet = (
        {ReservationStatus.ACTIVE, ReservationStatus.COMPLETED} if status is None else set(status)
    )
    legacy_hours = sum(STATUS_HOURS[s] for s in legacy)

    async with make_client(ADMIN) as client:
        seen = await _read_sections(client, route, status)

    assert seen["user"] == {rows[s][0]: (STATUS_HOURS[s], 1) for s in legacy}
    assert seen["device"] == {rows[s][1]: (STATUS_HOURS[s], 1) for s in legacy}
    assert seen["purpose"] == ({"unclassified": (legacy_hours, len(legacy))} if legacy else {})
    assert seen["fleet"] == {
        device: (STATUS_HOURS[s], 1) if s in fleet else (0.0, 0) for s, (_, device) in rows.items()
    }
    if route == "json":
        body = seen["json"]
        assert body["total_hours"] == legacy_hours
        assert body["total_reservations"] == len(legacy)
        assert [
            (b["topology_type"], b["hours"], b["reservation_count"])
            for b in body["by_topology_type"]
        ] == [("PHYSICAL", legacy_hours, len(legacy))]
        day_one = sum(min(STATUS_HOURS[s], 24.0) for s in legacy)
        day_two = sum(max(STATUS_HOURS[s] - 24.0, 0.0) for s in legacy)
        expected_days = [(DAY_ONE, day_one, len(legacy))]
        if day_two:
            expected_days.append((DAY_TWO, day_two, sum(1 for s in legacy if STATUS_HOURS[s] > 24)))
        assert [(b["day"], b["hours"], b["reservation_count"]) for b in body["by_day"]] == (
            expected_days
        )
        assert [
            (b["group_name"], b["hours"], b["reservation_count"]) for b in body["by_group"]
        ] == [("Ungrouped", legacy_hours, len(legacy))]


def test_the_status_rows_cross_a_utc_midnight_and_have_distinct_totals():
    """The fixture above is not vacuous: some row spans two days, and no two sets of
    statuses share an hour total."""
    assert any(hours > 24 for hours in STATUS_HOURS.values())
    totals = {}
    for mask in range(1 << len(STATUS_HOURS)):
        chosen = frozenset(s for i, s in enumerate(STATUS_HOURS) if mask >> i & 1)
        totals.setdefault(sum(STATUS_HOURS[s] for s in chosen), set()).add(chosen)
    assert all(len(sets) == 1 for sets in totals.values())
    assert date.fromisoformat(DAY_TWO) == date.fromisoformat(DAY_ONE) + timedelta(days=1)


# --- OPS-REPORT-7 and OPS-REPORT-8: the transit read on both routes ---------------------


async def _device_rows(client, route: str, **extra) -> dict[str, tuple[float, int, int, float]]:
    """The device section as {device id: (hours, count, transit count, transit hours)}."""
    resp = await _get(client, route, None, section="device", **extra)
    assert resp.status_code == 200, resp.text
    if route == "json":
        return {
            b["device_id"]: (
                b["hours"],
                b["reservation_count"],
                b["transit_reservations"],
                b["transit_hours"],
            )
            for b in resp.json()["by_device"]
        }
    return {
        r["device_id"]: (
            float(r["hours"]),
            int(r["reservation_count"]),
            int(r["transit_reservations"]),
            float(r["transit_hours"]),
        )
        for r in _csv_rows(resp)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_transit_devices_come_from_one_internal_cabling_call_for_the_counted_rows(
    network, make_client, real_transit_lookup, route
):
    """OPS-REPORT-7 and OPS-REPORT-8, read through the real lookup and `call_service`.

    Three COMPLETED rows are counted; cabling's answer names fork devices for two of
    them, and a third without a fork counts only its reserved device. An ACTIVE row and
    a CANCELLED row in the window and a COMPLETED row after it are not counted, and
    cabling's answer names a fork device for each of them too; none of those devices
    may be counted, and none of those rows' ids may be sent. (On the JSON route the
    ACTIVE row is loaded, for the fleet section, so its id is one a lookup over every
    loaded row would send.) The one request is a POST to cabling's batch path with the
    internal token, without the caller's bearer, with a 10 second timeout, and with
    the counted rows' ids as its body. The timeout is pinned as built: the spec's
    upstream table lists it, but no OPS-REPORT rule states it.
    """
    reserved_a, reserved_b, reserved_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    shared_switch, own_switch = uuid.uuid4(), uuid.uuid4()
    active_switch, cancelled_switch, later_switch = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    first = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_a], hours=3, owner_name="ann"
    )
    second = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_b], hours=5, owner_name="bo"
    )
    no_fork = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_c], hours=17, owner_name="fay"
    )
    active = await _insert(
        status=ReservationStatus.ACTIVE,
        device_ids=[uuid.uuid4()],
        hours=13,
        owner_name="ed",
    )
    cancelled = await _insert(
        status=ReservationStatus.CANCELLED,
        device_ids=[uuid.uuid4()],
        hours=7,
        owner_name="cy",
    )
    later = await _insert(
        status=ReservationStatus.COMPLETED,
        device_ids=[uuid.uuid4()],
        hours=11,
        owner_name="di",
        start=WINDOW_END + timedelta(hours=1),
    )
    batch = network.post(CABLING_BATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "devices": {
                    str(first.id): [str(reserved_a), str(shared_switch)],
                    str(second.id): [str(reserved_b), str(shared_switch), str(own_switch)],
                    str(active.id): [str(active_switch)],
                    str(cancelled.id): [str(cancelled_switch)],
                    str(later.id): [str(later_switch)],
                }
            },
        )
    )

    async with make_client(ADMIN) as client:
        rows = await _device_rows(client, route)

    assert rows == {
        str(reserved_a): (3.0, 1, 0, 0.0),
        str(reserved_b): (5.0, 1, 0, 0.0),
        str(reserved_c): (17.0, 1, 0, 0.0),
        str(shared_switch): (8.0, 2, 2, 8.0),
        str(own_switch): (5.0, 1, 1, 5.0),
    }
    assert batch.call_count == 1
    sent = batch.calls.last.request
    assert (sent.method, str(sent.url)) == ("POST", CABLING_BATCH_URL)
    assert sent.headers["X-Internal-Token"] == TOKEN
    assert "Authorization" not in sent.headers
    assert sent.extensions["timeout"] == {
        "connect": 10.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    body = json.loads(sent.content)
    assert list(body) == ["reservation_ids"]
    assert sorted(body["reservation_ids"]) == sorted(
        [str(first.id), str(second.id), str(no_fork.id)]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_valid_fork_devices_count_around_entries_that_are_not_uuids(
    network, make_client, real_transit_lookup, route
):
    """OPS-REPORT-7: the device sections count every device on a counted row's fork
    wiring that it did not reserve, so a malformed entry in cabling's answer must not
    cost the valid ones around it. In the answer, in this order: the first row's list
    has a string that is not a UUID before, between, and after its two switches; a key
    that is not a UUID lists a switch of its own; the second row, after that key,
    lists its reserved device and a switch; the third row's list is null. Each counted
    row has its own hours. Expected: the first row's two switches with its hours, the
    second row's switch with its hours, the third row's reserved device alone, and
    nothing from the key that names no reservation.

    Only string values are sent here. Wrongly typed values (issue #1553) are pinned by
    `test_wrongly_typed_entries_are_skipped_and_the_report_is_still_produced` and
    `test_a_200_of_the_wrong_shape_is_503_transit_gear_unavailable_on_both_routes`.
    """
    reserved_a, reserved_b, reserved_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    switch_a1, switch_a2, switch_b, orphan_switch = (uuid.uuid4() for _ in range(4))
    first = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_a], hours=3, owner_name="ann"
    )
    second = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_b], hours=5, owner_name="bo"
    )
    third = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_c], hours=7, owner_name="cy"
    )
    network.post(CABLING_BATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "devices": {
                    str(first.id): [
                        "not-a-uuid",
                        str(switch_a1),
                        "",
                        str(switch_a2),
                        "also-not-a-uuid",
                    ],
                    "not-a-reservation-id": [str(orphan_switch)],
                    str(second.id): [str(reserved_b), str(switch_b)],
                    str(third.id): None,
                }
            },
        )
    )

    async with make_client(ADMIN) as client:
        rows = await _device_rows(client, route)

    assert rows == {
        str(reserved_a): (3.0, 1, 0, 0.0),
        str(reserved_b): (5.0, 1, 0, 0.0),
        str(reserved_c): (7.0, 1, 0, 0.0),
        str(switch_a1): (3.0, 1, 1, 3.0),
        str(switch_a2): (3.0, 1, 1, 3.0),
        str(switch_b): (5.0, 1, 1, 5.0),
    }


# Every answer class OPS-REPORT-8 names as a failure: a transport error, and a non-200
# (another 2xx, a redirect httpx does not follow, a 4xx, a 5xx).
NON_200 = [201, 204, 302, 400, 404, 499, 500, 503, 599]
TRANSPORT_ERRORS = [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
FAILURES = [pytest.param(code, id=str(code)) for code in NON_200] + [
    pytest.param(error, id=error.__name__) for error in TRANSPORT_ERRORS
]


def _answer(batch_route, failure) -> None:
    if isinstance(failure, int):
        batch_route.mock(return_value=httpx.Response(failure, json={"devices": {}}))
    else:
        batch_route.mock(side_effect=failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("route", ROUTES)
async def test_a_cabling_failure_is_503_transit_gear_unavailable_on_both_routes(
    network, make_client, real_transit_lookup, route, failure
):
    """OPS-REPORT-8 and OPS-REPORT-9. A non-200 carries a well-formed empty body, so
    only its status can fail the report. The three transport errors are all
    `httpx.TransportError`s; an httpx error outside that class (`DecodingError`,
    `TooManyRedirects`) is not tried, since no rule calls it a transport error."""
    await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[uuid.uuid4()], hours=2, owner_name="ann"
    )
    batch = network.post(CABLING_BATCH_URL)
    _answer(batch, failure)

    async with make_client(ADMIN) as client:
        resp = await _get(client, route, None, section="device")

    assert resp.status_code == 503
    assert resp.json() == {"detail": {"error": "transit_gear_unavailable"}}
    assert batch.call_count == 1


# Issue #1553: a 200 whose body is not an object holding a `devices` object, or not
# JSON at all. Each answered 500 (or 422 for a body that does not parse) before.
WRONG_200_BODIES = [
    pytest.param({"json": [1, 2]}, id="list"),
    pytest.param({"json": "x"}, id="string"),
    pytest.param({"json": True}, id="true"),
    # httpx sends no body for `json=None`, so `null` is written out.
    pytest.param({"content": b"null"}, id="null"),
    pytest.param({"json": {}}, id="empty_object"),
    pytest.param({"json": {"devices": ["rid"]}}, id="devices_list"),
    pytest.param({"json": {"devices": "abc"}}, id="devices_string"),
    pytest.param({"json": {"devices": None}}, id="devices_null"),
    pytest.param({"content": b"<html>proxy error</html>"}, id="not_json"),
    pytest.param({"content": b"\xff\xfe"}, id="not_utf8"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", WRONG_200_BODIES)
@pytest.mark.parametrize("route", ROUTES)
async def test_a_200_of_the_wrong_shape_is_503_transit_gear_unavailable_on_both_routes(
    network, make_client, real_transit_lookup, route, body
):
    """OPS-REPORT-8 and OPS-REPORT-9 (issue #1553): a 200 from cabling whose body is
    not an object with a `devices` object fails the report closed with the fixed 503,
    on the JSON and the CSV route alike."""
    await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[uuid.uuid4()], hours=2, owner_name="ann"
    )
    batch = network.post(CABLING_BATCH_URL).mock(return_value=httpx.Response(200, **body))

    async with make_client(ADMIN) as client:
        resp = await _get(client, route, None, section="device")

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": {"error": "transit_gear_unavailable"}}
    assert batch.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_wrongly_typed_entries_are_skipped_and_the_report_is_still_produced(
    network, make_client, real_transit_lookup, route
):
    """OPS-REPORT-7 (issue #1553, owner decision): inside a well-formed map, a device
    list that is not a list and device ids that are not strings are skipped; the
    report answers 200 and still counts every well-typed transit device."""
    reserved_a, reserved_b, switch_a, switch_b = (uuid.uuid4() for _ in range(4))
    first = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_a], hours=3, owner_name="ann"
    )
    second = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved_b], hours=5, owner_name="bo"
    )
    third = await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[uuid.uuid4()], hours=7, owner_name="cy"
    )
    network.post(CABLING_BATCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "devices": {
                    str(first.id): [7, str(switch_a), True, None, {"k": 1}],
                    str(second.id): [str(reserved_b), [str(uuid.uuid4())], str(switch_b)],
                    str(third.id): 7,
                }
            },
        )
    )

    async with make_client(ADMIN) as client:
        rows = await _device_rows(client, route)

    transit = {device: row for device, row in rows.items() if row[2]}
    assert transit == {str(switch_a): (3.0, 1, 1, 3.0), str(switch_b): (5.0, 1, 1, 5.0)}


@pytest.mark.asyncio
@pytest.mark.parametrize("section", CSV_SECTIONS)
async def test_a_cabling_outage_fails_every_csv_section_closed(
    network, make_client, real_transit_lookup, section
):
    """OPS-REPORT-8 and OPS-REPORT-9: the transit read runs for any section, so a
    section without transit columns fails closed too."""
    network.inventory = [{"id": str(uuid.uuid4()), "name": "idle", "status": "AVAILABLE"}]
    await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[uuid.uuid4()], hours=2, owner_name="ann"
    )
    batch = network.post(CABLING_BATCH_URL).mock(side_effect=httpx.ConnectError)

    async with make_client(ADMIN) as client:
        resp = await _get(client, "csv", None, section=section)

    assert resp.status_code == 503
    assert resp.json() == {"detail": {"error": "transit_gear_unavailable"}}
    assert batch.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_include_transit_false_makes_no_cabling_call_on_both_routes(
    network, make_client, real_transit_lookup, route
):
    """OPS-REPORT-8's last clause: cabling is down, and with `include_transit=false`
    the report answers 200 with no transit share and sends cabling nothing."""
    reserved = uuid.uuid4()
    await _insert(
        status=ReservationStatus.COMPLETED, device_ids=[reserved], hours=2, owner_name="ann"
    )
    batch = network.post(CABLING_BATCH_URL).mock(side_effect=httpx.ConnectError)

    async with make_client(ADMIN) as client:
        rows = await _device_rows(client, route, include_transit="false")

    assert rows == {str(reserved): (2.0, 1, 0, 0.0)}
    assert batch.call_count == 0
