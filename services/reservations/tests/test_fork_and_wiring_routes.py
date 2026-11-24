"""RES-FORK-4, RES-FORK-10, and RES-FORK-18 on every fork and wiring route (#1532).

The router tests of the sibling files (test_fork_endpoints.py,
test_fork_version_endpoints.py, test_wiring_proxy_endpoints.py) patch
`_cabling_fork_call` or `_execution_wiring_call`, so the transport helpers do not run
in them. Here no function of the service is patched (the `network` fixture sets only
the internal token setting): respx stands in for the network under httpx, so the
router, both helpers, and `call_service` all run, and every request that leaves the
service is recorded.

The routes are read from the app (tests/_route_table.py): every route whose FIRST
path segment is a path parameter and whose SECOND segment is exactly `fork`,
`wiring`, or `wiring-status`, for each method it declares except HEAD and OPTIONS.
The reservation's id goes in the first segment's parameter and VERSION_ID in a
`version_id`; any other path or query parameter must be a required UUID, and a body
model must be in FORK_BODIES, or the test fails with a message naming what to add.
The non-owner test needs nothing beyond that, so it tests a new route in the family
unedited. The owner-or-admin, 4xx, 5xx, and transport tests also need the route's
upstream call from UPSTREAM: a route missing from it fails each of them, and
`test_every_fork_and_wiring_route_has_an_upstream_entry`, with a message naming
UPSTREAM. The non-JSON 5xx test takes its cabling routes from UPSTREAM, so it leaves
such a route out.

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
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import ForkCanvasBody, ForkSaveBody, bearer_scheme
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests._harness import TestSessionLocal, override_bearer, override_get_db
from tests._route_table import otherwise_valid_request, path_segments, routes_where

OWNER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
TOKEN = "fork-routes-internal-token"
NOW = datetime.now(timezone.utc)

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


_CABLING_ROUTES = [
    (method, route)
    for method, route in _fork_and_wiring_routes()
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
        "services/reservations/tests/test_fork_and_wiring_routes.py"
    )
    return entry


def _base_url(service: str) -> str:
    return {"cabling": settings.cabling_service_url, "execution": settings.execution_service_url}[
        service
    ]


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
    missing = derived - set(UPSTREAM)
    assert not missing, (
        f"{sorted(missing)}: add each route's upstream call to UPSTREAM in "
        "services/reservations/tests/test_fork_and_wiring_routes.py"
    )
    assert derived == set(UPSTREAM)


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
@EVERY_FORK_AND_WIRING_ROUTE
async def test_a_caller_who_is_neither_owner_nor_admin_gets_404_and_no_upstream_call(
    network, method, route
):
    """The target is ACTIVE, so no status rule answers first. The caller owns an
    ACTIVE reservation of their own, a row a load that ignored the id would find."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
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
@EVERY_FORK_AND_WIRING_ROUTE
async def test_the_owner_or_any_admin_reaches_the_upstream_for_the_reservation_named(
    network, method, route, sub, role
):
    """Another user's PENDING reservation is inserted first, so an admin load without
    its id filter finds two rows. A load of that wrong row would be refused on the
    four routes with a status rule (canvas, save, and restore need ACTIVE; retry
    refuses PENDING) and would send its devices on save; the three reads with no
    status rule build the upstream path from the path's id, so there only the two
    rows show. The answer relayed is the upstream's body, and the one upstream
    request names the target."""
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
    expected_path = up_path.format(reservation_id=target.id, version_id=VERSION_ID)
    assert (sent.method, str(sent.url)) == (up_method, _base_url(service) + expected_path)
    assert sent.headers["X-Internal-Token"] == TOKEN
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
@EVERY_FORK_AND_WIRING_ROUTE
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
@EVERY_FORK_AND_WIRING_ROUTE
async def test_an_upstream_5xx_is_503(network, method, route, status):
    """RES-FORK-10 also carries cabling's JSON detail on a 5xx; RES-FORK-18 states
    only the 503 for execution, so only the status is asserted for a wiring route."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    detail = {"error": "upstream_down", "status": status}
    network.route().mock(return_value=httpx.Response(status, json={"detail": detail}))
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert network.calls.call_count == 1
    assert str(network.calls.last.request.url).startswith(_base_url(service) + "/")
    if service == "cabling":
        assert resp.json() == {"detail": detail}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: e.__name__)
@EVERY_FORK_AND_WIRING_ROUTE
async def test_an_upstream_transport_error_is_503_failed_to_contact(network, method, route, error):
    """The fake raises the httpx exception itself, under call_service, so each helper's
    own translation to RuntimeError runs (one error of each httpx branch: connect,
    timeout, protocol)."""
    target = await _insert_reservation(owner=OWNER_ID, status=ReservationStatus.ACTIVE)
    service = _upstream_of(method, route)[0]
    network.route().mock(side_effect=error)
    async with _client_as(OWNER_ID, "user") as ac:
        resp = await ac.request(method, **_request_for(route, target.id))
    assert resp.status_code == 503
    assert resp.json()["detail"].startswith(f"Failed to contact {service} service: ")
    assert network.calls.call_count == 1
