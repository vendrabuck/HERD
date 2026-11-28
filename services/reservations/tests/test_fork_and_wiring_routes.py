"""RES-FORK-4, RES-FORK-10, and RES-FORK-18 on every fork and wiring route (#1532).

The router tests of the sibling files (test_fork_endpoints.py,
test_fork_version_endpoints.py, test_wiring_proxy_endpoints.py) patch
`_cabling_fork_call` or `_execution_wiring_call`, so the transport helpers do not run
in them. Here nothing on these routes' path between the router and the network is
patched: the router, both helpers, and `call_service` run, and respx answers under
httpx and records every request that leaves the service. What is replaced: the
dependency overrides of `get_db` (the test database), `get_current_user_payload` (the
caller's claims), and `bearer_scheme` (a fixed bearer, `fake-token`); the internal
token setting, which the `network` fixture sets; and the directory's autouse patch of
`reporting_service._fetch_transit_devices` (tests/conftest.py), which these routes do
not call.

The routes are read from the app (tests/_route_table.py): every route whose FIRST
path segment is a path parameter and whose SECOND segment is exactly `fork`,
`wiring`, or `wiring-status`, for each method it declares except HEAD and OPTIONS.
The reservation's id goes in the first segment's parameter and VERSION_ID in a
`version_id`; any other path or query parameter must be a required UUID, and a body
model must be in FORK_BODIES, or the test fails with a message naming what to add.
The non-owner test needs nothing beyond that, so it tests a new route in the family
unedited.

The other route tests assume each route, asked by its owner about an ACTIVE
reservation, makes exactly one call, to cabling or execution, at the path its
UPSTREAM entry names, whose only parameters are the reservation's id and
`version_id`. A route missing from UPSTREAM fails each of them, and
`test_every_fork_and_wiring_route_has_an_upstream_entry`, with a message naming
UPSTREAM; a route that breaks one of those assumptions needs its own tests and goes
in OWN_TESTS with the reason, which takes it out of every test here but the
non-owner one. An entry for a route the app no longer has fails with a message to
remove it.

Limits: a fork or wiring route of any other shape (`/{id}/forks`,
`/{id}/fork-preview`, `/x/{id}/fork`, `/fork/{id}`) is not read, however it is
guarded; a mounted sub-application is not read (iter_route_contexts does not
descend into a Mount); HEAD and OPTIONS are not requested; a header, cookie, or
dependency the route needs besides the caller's claims and bearer is not supplied,
so such a route fails on its answer rather than with a hint. The probe test below
shows each selection limit on a small app.
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.fork_wiring_ledger import ForkWiringLedger
from app.models.outbox import OutboxEvent
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import ForkCanvasBody, ForkSaveBody, bearer_scheme
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests._harness import TestSessionLocal, override_bearer, override_get_db
from tests._route_table import otherwise_valid_request, path_segments, routes_where
from tests._upstream_bodies import MARKER, NOT_AN_OBJECT, UNPARSEABLE, assert_nothing_leaked
from tests._upstream_bodies import answer as bad_answer

OWNER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
TOKEN = "fork-routes-internal-token"

CANVAS = {"nodes": [{"id": "n1"}], "edges": []}
FORK_BODIES = {ForkCanvasBody: {"canvas_data": CANVAS}, ForkSaveBody: {"canvas_data": CANVAS}}

# The upstream call each route makes, as (service, method, path, body). The body is
# what the route must send: None, the canvas alone, or the save body that RES-FORK-7
# stamps with the caller and the reservation's own devices.
UPSTREAM = {
    ("GET", "/{reservation_id}/fork"): (
        "cabling",
        "GET",
        "/internal/forks/{reservation_id}",
        None,
    ),
    ("PUT", "/{reservation_id}/fork/canvas"): (
        "cabling",
        "PUT",
        "/internal/forks/{reservation_id}/canvas",
        "canvas",
    ),
    ("POST", "/{reservation_id}/fork/save"): (
        "cabling",
        "POST",
        "/internal/forks/{reservation_id}/save",
        "save",
    ),
    ("GET", "/{reservation_id}/fork/versions/{version_id}"): (
        "cabling",
        "GET",
        "/internal/forks/{reservation_id}/versions/{version_id}",
        None,
    ),
    ("POST", "/{reservation_id}/fork/versions/{version_id}/restore"): (
        "cabling",
        "POST",
        "/internal/forks/{reservation_id}/versions/{version_id}/restore",
        None,
    ),
    ("GET", "/{reservation_id}/wiring-status"): (
        "execution",
        "GET",
        "/internal/reservations/{reservation_id}/wiring-status",
        None,
    ),
    ("POST", "/{reservation_id}/wiring/retry"): (
        "execution",
        "POST",
        "/internal/reservations/{reservation_id}/wiring/retry",
        None,
    ),
}
FAMILY_SEGMENTS = {"fork", "wiring", "wiring-status"}

# Routes of the family that break the assumptions in the module docstring, each with
# the reason and the file of its own tests. The non-owner test still covers them.
OWN_TESTS: dict[tuple[str, str], str] = {}

# The routes whose upstream call carries the caller's bearer: `docs/specs/CONVENTIONS.md`
# (reservations) says reservations forwards it beside the internal token on the fork
# GET ONLY and never from the wiring path (TOPO-FORK-25 is the read it redacts).
FORWARDS_BEARER = {("GET", "/{reservation_id}/fork")}
CALLER_BEARER = "Bearer fake-token"  # tests._harness.override_bearer


@pytest.fixture(autouse=True)
def _clock():
    """Read the clock when each test starts, never at import (#1561)."""
    global NOW
    NOW = datetime.now(timezone.utc)


def _is_path_param(segment: str) -> bool:
    return re.fullmatch(r"\{[^{}]+\}", segment) is not None


def _is_fork_or_wiring_path(path: str) -> bool:
    segments = path_segments(path)
    return len(segments) >= 2 and _is_path_param(segments[0]) and segments[1] in FAMILY_SEGMENTS


def _fork_and_wiring_routes(routes=None):
    return routes_where(_is_fork_or_wiring_path, routes)


EVERY_FORK_AND_WIRING_ROUTE = pytest.mark.parametrize(
    ("method", "route"),
    _fork_and_wiring_routes(),
    ids=[f"{method} {route.path}" for method, route in _fork_and_wiring_routes()],
)


_UPSTREAM_ROUTES = [
    (method, route)
    for method, route in _fork_and_wiring_routes()
    if (method, route.path) not in OWN_TESTS
]
EVERY_UPSTREAM_ROUTE = pytest.mark.parametrize(
    ("method", "route"),
    _UPSTREAM_ROUTES,
    ids=[f"{method} {route.path}" for method, route in _UPSTREAM_ROUTES],
)

_CABLING_ROUTES = [
    (method, route)
    for method, route in _UPSTREAM_ROUTES
    if UPSTREAM.get((method, route.path), ("",))[0] == "cabling"
]
# The routes UPSTREAM sends to cabling; one missing from UPSTREAM is left out here and
# fails test_every_fork_and_wiring_route_has_an_upstream_entry instead.
EVERY_CABLING_ROUTE = pytest.mark.parametrize(
    ("method", "route"),
    _CABLING_ROUTES,
    ids=[f"{method} {route.path}" for method, route in _CABLING_ROUTES],
)


def _request_for(route, reservation_id: uuid.UUID) -> dict:
    """An otherwise valid request for `route` naming `reservation_id` in its first segment."""
    first = re.fullmatch(r"\{([^}:]+)[^}]*\}", path_segments(route.path)[0]).group(1)
    return otherwise_valid_request(
        route,
        bodies=FORK_BODIES,
        bodies_name="FORK_BODIES",
        where="_request_for in services/reservations/tests/test_fork_and_wiring_routes.py",
        path_values={first: str(reservation_id), "version_id": VERSION_ID},
    )


def _upstream_of(method: str, route) -> tuple:
    entry = UPSTREAM.get((method, route.path))
    assert entry is not None, (
        f"{method} {route.path}: add its upstream call to UPSTREAM in "
        "services/reservations/tests/test_fork_and_wiring_routes.py; if, asked by its "
        "owner about an ACTIVE reservation, it makes no upstream call or more than one, "
        "give it its own tests and add it to OWN_TESTS with the reason"
    )
    return entry


def _base_url(service: str) -> str:
    urls = {"cabling": settings.cabling_service_url, "execution": settings.execution_service_url}
    assert service in urls, (
        f"UPSTREAM names service {service!r}; these tests know cabling and execution "
        "only, so give the route its own tests and add it to OWN_TESTS with the reason"
    )
    return urls[service]


def _upstream_url(service: str, path: str, reservation_id: uuid.UUID) -> str:
    try:
        return _base_url(service) + path.format(
            reservation_id=reservation_id, version_id=VERSION_ID
        )
    except KeyError as exc:
        raise AssertionError(
            f"{path}: names {exc}, which has no fixed value here; add one to the "
            "path_values of _request_for and to this format call"
        ) from exc


def _client_as(sub: str, role: str) -> AsyncClient:
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _insert_reservation(
    *,
    owner: str,
    status: ReservationStatus,
) -> Reservation:
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(owner),
            owner_name="owner",
            device_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
            topology_id=uuid.uuid4(),
            topology_type=TopologyType.PHYSICAL,
            purpose="test",
            start_time=NOW - timedelta(hours=1),
            end_time=NOW + timedelta(hours=2),
            status=status,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res


@pytest.fixture
def network(monkeypatch):
    """respx under httpx for every outbound call; the internal token is set to TOKEN."""
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_every_fork_and_wiring_route_has_an_upstream_entry():
    """The derived list is the spec's seven routes: not vacuous, and none unaccounted for."""
    derived = {(method, route.path) for method, route in _fork_and_wiring_routes()}
    missing = derived - set(UPSTREAM) - set(OWN_TESTS)
    assert not missing, (
        f"{sorted(missing)}: add each route's upstream call to UPSTREAM in "
        "services/reservations/tests/test_fork_and_wiring_routes.py, or, if it breaks "
        "the assumptions in the module docstring, add it to OWN_TESTS with the reason"
    )
    stale = (set(UPSTREAM) | set(OWN_TESTS)) - derived
    assert not stale, (
        f"{sorted(stale)}: the app has no such route any more; remove its entry from "
        "UPSTREAM or OWN_TESTS"
    )
    assert not set(UPSTREAM) & set(OWN_TESTS)


def test_fork_and_wiring_routes_match_segments_and_skip_head_and_mounts():
    """The selection rule and its limits in the module docstring, on a small app."""
    probe = FastAPI()

    @probe.api_route("/{reservation_id}/fork", methods=["GET", "HEAD", "OPTIONS"])
    async def fork(reservation_id: uuid.UUID):
        return {}

    @probe.post("/{rid}/wiring/retry")
    async def retry(rid: uuid.UUID):
        return {}

    @probe.get("/{reservation_id}/forks")
    async def forks(reservation_id: uuid.UUID):
        return {}

    @probe.get("/{reservation_id}/fork-preview")
    async def preview(reservation_id: uuid.UUID):
        return {}

    @probe.get("/x/{reservation_id}/fork")
    async def nested(reservation_id: uuid.UUID):
        return {}

    @probe.get("/fork/{reservation_id}")
    async def leading(reservation_id: uuid.UUID):
        return {}

    sub = FastAPI()

    @sub.get("/fork")
    async def mounted():
        return {}

    probe.mount("/{reservation_id}/fork/sub", sub)
    assert [(m, r.path) for m, r in _fork_and_wiring_routes(probe.routes)] == [
        ("GET", "/{reservation_id}/fork"),
        ("POST", "/{rid}/wiring/retry"),
    ]


# --- RES-FORK-4: owner or admin, else 404 with no upstream call ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(ReservationStatus), ids=lambda s: s.value)
@EVERY_FORK_AND_WIRING_ROUTE
async def test_a_caller_who_is_neither_owner_nor_admin_gets_404_and_no_upstream_call(
    network, method, route, status
):
    """In every status of the target: a status rule that answered before the owner
    gate (a 409 on a reservation that is not ACTIVE) would tell this caller the
    reservation exists. The caller owns an ACTIVE reservation of their own, a row a
    load that ignored the id would find."""
    target = await _insert_reservation(owner=OWNER_ID, status=status)
    await _insert_reservation(owner=OTHER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=httpx.Response(200, json={"version_number": 1}))
    async with _client_as(OTHER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}
    assert network.calls.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sub", "role"),
    [(OWNER_ID, "user"), (ADMIN_ID, "admin"), (ADMIN_ID, "superadmin")],
    ids=["owner", "admin", "superadmin"],
)
@EVERY_UPSTREAM_ROUTE
async def test_the_owner_or_any_admin_reaches_the_upstream_for_the_reservation_named(
    network, method, route, sub, role
):
    """Another user's PENDING reservation is inserted first, so an admin load without
    its id filter finds two rows. A load of that wrong row would be refused on the
    four routes with a status rule (canvas, save, and restore need ACTIVE; retry
    refuses PENDING) and would send its devices on save; the three reads with no
    status rule build the upstream path from the path's id, so there only the two
    rows show. The answer relayed is the upstream's body, and the one upstream
    request names the target. It carries the caller's bearer exactly on the routes in
    FORWARDS_BEARER (the fork GET, per `docs/specs/CONVENTIONS.md`) and no
    `Authorization` header on any other."""
    await _insert_reservation(owner=OTHER_ID, status=ReservationStatus.PENDING)
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service, up_method, up_path, body_kind = _upstream_of(method, route)
    answer = {"version_number": 1, "released": [], "built": [], "marker": str(uuid.uuid4())}
    network.route().mock(return_value=httpx.Response(200, json=answer))
    async with _client_as(sub, role) as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 200
    assert resp.json() == answer
    assert network.calls.call_count == 1
    sent = network.calls.last.request
    assert (sent.method, str(sent.url)) == (up_method, _upstream_url(service, up_path, target.id))
    assert sent.headers["X-Internal-Token"] == TOKEN
    forwards = (method, route.path) in FORWARDS_BEARER
    assert sent.headers.get("Authorization") == (CALLER_BEARER if forwards else None)
    expected_body = {
        None: None,
        "canvas": {"canvas_data": CANVAS},
        "save": {
            "canvas_data": CANVAS,
            "created_by": sub,
            "member_device_ids": [str(d) for d in target.device_ids],
        },
    }[body_kind]
    assert (json.loads(sent.content) if sent.content else None) == expected_body


# --- RES-FORK-10 and RES-FORK-18: the upstream's answer, relayed or mapped -----------

RELAYED_4XX = [400, 403, 409, 422, 499]
MAPPED_5XX = [500, 502, 503, 599]
TRANSPORT_ERRORS = [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", RELAYED_4XX)
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_4xx_is_relayed_with_its_status_and_unwrapped_detail(
    network, method, route, status
):
    """400 and 499 are the edges of the class. A cabling 404 on the fork read is the
    lazy create of RES-FORK-5 (test_fork_endpoints.py), so 404 is not in this list."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    detail = {"error": "refused_upstream", "status": status}
    network.route().mock(return_value=httpx.Response(status, json={"detail": detail}))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == status
    assert resp.json() == {"detail": detail}
    assert network.calls.call_count == 1
    assert str(network.calls.last.request.url).startswith(_base_url(service) + "/")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", MAPPED_5XX)
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_5xx_is_503(network, method, route, status):
    """RES-FORK-10 carries cabling's JSON detail on a 5xx. RES-FORK-18 states only the
    503 for execution and, unlike RES-FORK-10, names no detail, so on a wiring route
    what is pinned is that execution's own detail is not passed on: its text appears
    nowhere in the answer. The wording that replaces it is in no rule and is not
    asserted."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    marker = f"upstream-detail-{uuid.uuid4()}"
    detail = {"error": marker, "status": status}
    network.route().mock(return_value=httpx.Response(status, json={"detail": detail}))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert network.calls.call_count == 1
    assert str(network.calls.last.request.url).startswith(_base_url(service) + "/")
    if service == "cabling":
        assert resp.json() == {"detail": detail}
    else:
        assert marker not in resp.text


@pytest.mark.asyncio
@EVERY_CABLING_ROUTE
async def test_a_cabling_5xx_without_a_json_body_is_503_cabling_service_is_unavailable(
    network, method, route
):
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=httpx.Response(502, content=b"<html>bad gateway</html>"))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Cabling service is unavailable"}
    assert network.calls.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: e.__name__)
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_transport_error_is_503_failed_to_contact(network, method, route, error):
    """The fake raises the httpx exception itself, under call_service, so each helper's
    own translation to RuntimeError runs. The three are all `httpx.TransportError`s (a
    connect failure, a timeout, a protocol error): what RES-FORK-10 calls a transport
    failure and RES-FORK-18 an absence. An httpx error outside TransportError (`DecodingError`,
    `TooManyRedirects`) is not tried: no rule says what it becomes, so an `except`
    narrowed to TransportError passes here. The error's own text (here a marker and an
    address with userinfo) never reaches the answer: the detail names the class only."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    network.route().mock(side_effect=error(f"{MARKER} http://svc-user:svc-pw@upstream"))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert resp.json() == {"detail": f"Failed to contact {service} service: {error.__name__}"}
    assert MARKER not in resp.text
    assert "svc-pw" not in resp.text
    assert network.calls.call_count == 1


@pytest.mark.asyncio
@EVERY_UPSTREAM_ROUTE
async def test_a_runtime_error_no_rule_words_answers_the_fixed_detail(
    network, caplog, method, route
):
    """RES-UPSTREAM-2: a RuntimeError that is not HERD's typed upstream error (here one
    the transport raises under call_service, which neither helper wraps) answers the
    fixed `A service this request depends on is unavailable`, never its own text, and
    is logged by class name."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(side_effect=RuntimeError(f"{MARKER} http://svc-user:svc-pw@upstream"))
    caplog.set_level("DEBUG")
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "A service this request depends on is unavailable"}
    assert network.calls.call_count == 1
    (record,) = [
        r for r in caplog.records if r.__dict__.get("action") == "unexpected_runtime_error"
    ]
    assert record.error_class == "RuntimeError"
    assert_nothing_leaked(resp.text, caplog.text)
    assert "svc-pw" not in resp.text + caplog.text


# --- Issue #1577: an upstream body the relay cannot read -----------------------------

# The 2xx a relay cannot parse answers 503 with fixed wording: the RES-FORK-10 answer
# for a cabling 5xx, the RES-FORK-18 one for execution, and on save its own wording,
# since a 2xx save most likely committed (the sweep's wiring heal stages it).
UNREADABLE_2XX_DETAIL = {
    "cabling": "Cabling service is unavailable",
    "execution": "Execution service is unavailable",
}
SAVE_ROUTE = ("POST", "/{reservation_id}/fork/save")
UNREADABLE_SAVE_DETAIL = (
    "Cabling answered the save, but its answer could not be read; "
    "reload the fork to see whether the save was applied"
)
UNREADABLE_BODIES = {name: body for name, body in UNPARSEABLE.items() if name != "empty"}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", UNREADABLE_BODIES.values(), ids=UNREADABLE_BODIES.keys())
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_2xx_that_does_not_parse_is_503_with_fixed_wording(
    network, caplog, method, route, body
):
    """Not JSON, not UTF-8, an integer past the digit limit, or nested past the
    decoder's recursion limit: a 500 before #1577. An empty 2xx is not here: it
    relays as null, the 204 path."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    network.route().mock(return_value=bad_answer(200, body))
    caplog.set_level("DEBUG")
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    if (method, route.path) == SAVE_ROUTE:
        expected = UNREADABLE_SAVE_DETAIL
    else:
        expected = UNREADABLE_2XX_DETAIL[service]
    assert resp.json() == {"detail": expected}
    assert network.calls.call_count == 1
    assert_nothing_leaked(resp.text, caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 204], ids=["empty_200", "no_content_204"])
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_204_or_empty_2xx_relays_as_null(network, method, route, status):
    """RES-FORK-10 and RES-FORK-18: a 2xx with no body is not "a body that does not
    parse"; it relays as null with 200, the route's own status."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=httpx.Response(status, content=b""))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 200
    assert resp.json() is None
    assert network.calls.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NOT_AN_OBJECT.values(), ids=NOT_AN_OBJECT.keys())
@EVERY_UPSTREAM_ROUTE
async def test_an_upstream_2xx_of_another_json_type_relays_unchanged(
    network, caplog, method, route, body
):
    """The proxy routes declare no response model, so a parsed body of any JSON type
    passes through (the recommended option on #1577). On save it stages nothing and
    logs the fixed action with no body text."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=bad_answer(200, body))
    caplog.set_level("DEBUG")
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 200
    assert resp.json() == json.loads(body)
    if (method, route.path) == SAVE_ROUTE:
        actions = [r.__dict__.get("action") for r in caplog.records]
        assert "wiring_changed_stage_failed" in actions
        assert_nothing_leaked(caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer_body",
    [
        {"version_number": "2", "released": [], "built": []},
        {"version_number": True, "released": [], "built": []},
        {"version_number": 2, "released": "x", "built": []},
        {"version_number": 2, "released": [], "built": [1]},
        {"released": [], "built": []},
    ],
)
async def test_a_save_answer_of_the_wrong_shape_relays_and_stages_nothing(network, answer_body):
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=httpx.Response(200, json=answer_body))
    route = next(r for m, r in _UPSTREAM_ROUTES if (m, r.path) == SAVE_ROUTE)
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request("POST", **_request_for(route, target.id))
    assert resp.status_code == 200
    assert resp.json() == answer_body
    async with TestSessionLocal() as db:
        assert (await db.execute(select(OutboxEvent))).scalars().all() == []
        assert await db.get(ForkWiringLedger, target.id) is None


@pytest.mark.asyncio
async def test_a_save_answer_of_the_declared_shape_stages_its_version(network):
    """The control for the table above."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    answer_body = {"version_number": 2, "released": [], "built": []}
    network.route().mock(return_value=httpx.Response(200, json=answer_body))
    route = next(r for m, r in _UPSTREAM_ROUTES if (m, r.path) == SAVE_ROUTE)
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request("POST", **_request_for(route, target.id))
    assert resp.status_code == 200
    async with TestSessionLocal() as db:
        assert len((await db.execute(select(OutboxEvent))).scalars().all()) == 1
        assert (await db.get(ForkWiringLedger, target.id)).last_staged_fork_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 502])
@pytest.mark.parametrize(
    "body",
    [UNPARSEABLE["deep_array"], UNPARSEABLE["deep_object"]],
    ids=["deep_array", "deep_object"],
)
@EVERY_CABLING_ROUTE
async def test_a_deep_cabling_error_body_is_relayed_like_any_body_that_does_not_parse(
    network, method, route, body, status
):
    """RES-FORK-10 for a body nested past the recursion limit, a 500 before #1577: a
    4xx keeps its status and relays its text, as any other 4xx body that does not
    parse does; a 5xx is 503 `Cabling service is unavailable`."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    network.route().mock(return_value=bad_answer(status, body))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    if status == 502:
        assert resp.status_code == 503
        assert resp.json() == {"detail": "Cabling service is unavailable"}
    else:
        assert resp.status_code == 409
        assert resp.json() == {"detail": body.decode()}
