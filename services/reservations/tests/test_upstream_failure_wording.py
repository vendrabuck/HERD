"""Failure details and log lines for calls to other services use fixed wording.

RES-UPSTREAM-1 to RES-UPSTREAM-4. The invariant: when a call reservations makes to
inventory, cabling, execution, or auth fails, what reservations writes about it (a
503 detail, an exception message, a log line, a row, an outbox payload) holds fixed
text, the service's name, the HTTP status code, the exception's class name, and ids.
Never the body the other service answered, and never an exception's own text, which
can quote that body or the address the service was reached at (`user:password@`
included).

Each test makes the upstream answer or raise with `SENTINEL` (in the body, in the
exception's text, and in an address with userinfo) and asserts that it reaches none
of the answer, the rows, the outbox payloads, or any log record at any level (its
message, its traceback, and every attribute), while the fixed wording, the class
name, and the status do. One test is narrower, and says why: a background fork call
that raised logs a traceback that chains the httpx exception itself, so only the
line's message and the exception it logs are checked there. Nothing between the
route (or the background function) and the network is patched: respx answers under
httpx.

The sites:

- create and PATCH: inventory's device batch raising or answering an error status,
  cabling's validate raising or answering an error status, the dynamic-template read
  raising, and the inventory flip exhausting its attempts (RES-UPSTREAM-1).
- the fork routes: a lazy create answering an error status, no internal token
  (RES-UPSTREAM-1), and a RuntimeError no rule words (RES-UPSTREAM-2).
- the background fork calls: create, archive, and prune answering an error status or
  raising (RES-UPSTREAM-1), and a definitive fork-create refusal (RES-UPSTREAM-3).
- the utilization report's inventory, auth, and execution reads (RES-UPSTREAM-3).
- the provision-result callback's `error` (RES-UPSTREAM-4).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import herd_common.retry as retry_module
import httpx
import pytest
import respx
from app.config import settings
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.routers.reservations import bearer_scheme
from app.services.reporting_service import (
    fetch_execution_run_count,
    fetch_fleet_devices,
    fetch_user_groups_map,
)
from app.services.reservation_service import (
    INVENTORY_RESERVE_FAILED,
    _archive_reservation_fork_best_effort,
    _create_reservation_fork_best_effort,
    _fork_membership_refused,
    _prune_removed_devices_from_fork_best_effort,
)
from app.services.upstream_failure import (
    SERVICE_UNAVAILABLE,
    UpstreamUnavailable,
    answered,
    failure_label,
    unavailable_detail,
    unreachable,
)
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

Session = async_sessionmaker(engine, expire_on_commit=False)

SENTINEL = "upstream-text-sentinel-9c4e"
USERINFO_SECRET = "upstream-userinfo-pw-3f7a"
# What an exception raised on the way to another service can quote: the body, and
# the address with userinfo.
EXC_TEXT = f"{SENTINEL} at http://svc-user:{USERINFO_SECRET}@upstream:8000/x"
BODY = {"detail": f"{SENTINEL} refused", "trace": EXC_TEXT}

TOKEN = "internal-token-wording"
INV = settings.inventory_service_url
CAB = settings.cabling_service_url
EXE = settings.execution_service_url
AUTH = settings.auth_service_url
OWNER = uuid.uuid4()
DEVICE = uuid.uuid4()
TEMPLATE = uuid.uuid4()
TOPOLOGY = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clock():
    """Read the clock when each test starts, never at import (#1561)."""
    global NOW
    NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """retry_with_backoff waits between attempts; the attempts still all run."""

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(retry_module, "asyncio", SimpleNamespace(sleep=_sleep))


@pytest.fixture
def network(monkeypatch):
    """respx under httpx for every outbound call; the internal token is set."""
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def _override_get_db():
    async with Session() as session:
        yield session


def _client() -> AsyncClient:
    """An admin who owns OWNER's rows (an admin skips the visibility pre-check)."""
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(OWNER),
        "username": "owner",
        "role": "admin",
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="caller-jwt"
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _error_status(status: int = 500) -> httpx.Response:
    """An error answer whose JSON body holds the sentinel."""
    return httpx.Response(status, json=BODY)


def _device() -> dict:
    return {
        "id": str(DEVICE),
        "name": "sw-1",
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "exclusive": True,
    }


def _booking(**extra) -> dict:
    return {
        "device_ids": [str(DEVICE)],
        "purpose": "wording",
        "start_time": (NOW - timedelta(minutes=1)).isoformat(),
        "end_time": (NOW + timedelta(hours=2)).isoformat(),
        **extra,
    }


async def _insert(status: ReservationStatus, *, topology_id=None, start=None, end=None):
    async with Session() as s:
        res = Reservation(
            user_id=OWNER,
            owner_name="owner",
            device_ids=[str(DEVICE)],
            topology_id=topology_id,
            topology_type=TopologyType.PHYSICAL,
            purpose="wording",
            start_time=start or NOW - timedelta(hours=1),
            end_time=end or NOW + timedelta(hours=2),
            status=status,
        )
        s.add(res)
        await s.commit()
        return res.id


async def _stored_text() -> str:
    """Every reservation row and outbox payload, as text."""
    async with Session() as s:
        rows = (await s.execute(select(Reservation))).scalars().all()
        events = (await s.execute(select(OutboxEvent))).scalars().all()
    parts = [str({c.name: getattr(r, c.name) for c in Reservation.__table__.columns}) for r in rows]
    parts += [f"{e.subject} {e.payload}" for e in events]
    return "\n".join(parts)


def _assert_absent(caplog, *texts: str) -> None:
    """The sentinel and the userinfo appear in no text given, no log record's message,
    traceback, or attribute (every `extra=` key), at any level."""
    record_texts = [caplog.text] + [str(vars(r)) for r in caplog.records]
    for text in [*texts, *record_texts]:
        assert SENTINEL not in text
        assert USERINFO_SECRET not in text


# --- the helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, KeyError, RuntimeError],
    ids=lambda e: e.__name__,
)
def test_unreachable_names_the_class_and_never_the_text(error):
    exc = unreachable("inventory", error(EXC_TEXT))
    assert isinstance(exc, UpstreamUnavailable)
    assert str(exc) == f"Failed to contact inventory service: {error.__name__}"


def test_an_http_status_error_is_labelled_by_its_status():
    request = httpx.Request("GET", f"http://svc-user:{USERINFO_SECRET}@inventory/x")
    response = httpx.Response(502, json=BODY, request=request)
    exc = httpx.HTTPStatusError(EXC_TEXT, request=request, response=response)
    assert failure_label(exc) == "HTTP 502"
    assert str(unreachable("cabling", exc)) == "Failed to contact cabling service: HTTP 502"


def test_answered_names_the_call_and_the_status():
    assert str(answered("Cabling fork-archive", 500)) == "Cabling fork-archive returned 500"


def test_unavailable_detail_keeps_a_worded_failure_and_replaces_any_other(caplog):
    caplog.set_level(logging.DEBUG)
    assert unavailable_detail(answered("Cabling validation", 503)) == (
        "Cabling validation returned 503"
    )
    assert unavailable_detail(RuntimeError(EXC_TEXT)) == SERVICE_UNAVAILABLE
    assert unavailable_detail(RecursionError(EXC_TEXT)) == SERVICE_UNAVAILABLE
    (first, second) = [r for r in caplog.records if r.__dict__.get("action")]
    assert (first.error_class, second.error_class) == ("RuntimeError", "RecursionError")
    _assert_absent(caplog)


# --- create and PATCH --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout], ids=lambda e: e.__name__)
async def test_create_names_the_class_when_inventory_cannot_be_reached(network, caplog, error):
    network.post(f"{INV}/devices/batch").mock(side_effect=error(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": f"Failed to contact inventory service: {error.__name__}"}
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500, 503])
async def test_create_names_the_status_when_inventory_answers_an_error(network, caplog, status):
    network.post(f"{INV}/devices/batch").mock(return_value=_error_status(status))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": f"Failed to contact inventory service: HTTP {status}"}
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 500, 503])
async def test_create_names_only_the_status_of_a_failed_topology_validation(
    network, caplog, status
):
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device()]})
    )
    network.post(f"{CAB}/topologies/{TOPOLOGY}/validate/internal").mock(
        return_value=_error_status(status)
    )
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking(topology_id=str(TOPOLOGY)))
    assert resp.status_code == 503
    assert resp.json() == {"detail": f"Cabling validation returned {status}"}
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
async def test_create_names_the_class_when_cabling_validation_cannot_be_reached(network, caplog):
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device()]})
    )
    network.post(f"{CAB}/topologies/{TOPOLOGY}/validate/internal").mock(
        side_effect=httpx.ConnectError(EXC_TEXT)
    )
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking(topology_id=str(TOPOLOGY)))
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Failed to contact cabling service: ConnectError"}
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
async def test_create_names_the_class_when_a_dynamic_template_cannot_be_read(network, caplog):
    network.get(f"{INV}/templates/{TEMPLATE}").mock(side_effect=httpx.ReadTimeout(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    body = _booking(device_ids=[], dynamic_requests=[{"template_id": str(TEMPLATE)}])
    async with _client() as ac:
        resp = await ac.post("/", json=body)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Failed to contact inventory service: ReadTimeout"}
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
async def test_a_create_whose_inventory_flip_fails_answers_fixed_wording(network, caplog):
    """Three attempts, every one answering 500 with the sentinel: the row is FAILED,
    reservation.failed is staged, and neither they nor the answer nor any log line
    holds what inventory sent (RES-CREATE-15)."""
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device()]})
    )
    flip = network.post(f"{INV}/devices/{DEVICE}/status").mock(return_value=_error_status(500))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": INVENTORY_RESERVE_FAILED}
    assert flip.call_count == 3
    async with Session() as s:
        (row,) = (await s.execute(select(Reservation))).scalars().all()
        subjects = [e.subject for e in (await s.execute(select(OutboxEvent))).scalars()]
    assert row.status == ReservationStatus.FAILED
    assert subjects == ["herd.reservations.failed"]
    # Each failed device update is logged with its status, never the body.
    assert "status to RESERVED (HTTP 500)" in caplog.text
    _assert_absent(caplog, resp.text, await _stored_text())


@pytest.mark.asyncio
async def test_patch_names_the_class_when_inventory_cannot_be_reached(network, caplog):
    rid = await _insert(
        ReservationStatus.PENDING,
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=2),
    )
    network.post(f"{INV}/devices/batch").mock(side_effect=httpx.ConnectError(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.patch(f"/{rid}", json={"device_ids": [str(DEVICE), str(uuid.uuid4())]})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Failed to contact inventory service: ConnectError"}
    _assert_absent(caplog, resp.text, await _stored_text())


# --- the fork routes ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 500])
async def test_a_failed_lazy_fork_create_names_only_the_status(network, caplog, status):
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    network.get(f"{CAB}/internal/forks/{rid}").mock(return_value=httpx.Response(404))
    network.post(f"{CAB}/internal/forks").mock(return_value=_error_status(status))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 503
    assert resp.json() == {"detail": f"Cabling fork-create returned {status}"}
    _assert_absent(caplog, resp.text)


@pytest.mark.asyncio
async def test_a_fork_route_without_an_internal_token_answers_fixed_wording(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    async with _client() as ac:
        resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 503
    assert resp.json() == {
        "detail": "internal_api_token not configured; cannot reach cabling forks"
    }


@pytest.mark.asyncio
async def test_a_runtime_error_no_rule_words_answers_the_fixed_detail(network, caplog):
    """A RuntimeError a library raises (here from the transport itself) is not relayed:
    the route answers SERVICE_UNAVAILABLE and logs the class name (RES-UPSTREAM-2)."""
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    network.get(f"{CAB}/internal/forks/{rid}").mock(side_effect=RuntimeError(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.get(f"/{rid}/fork")
    assert resp.status_code == 503
    assert resp.json() == {"detail": SERVICE_UNAVAILABLE}
    (record,) = [
        r for r in caplog.records if r.__dict__.get("action") == "unexpected_runtime_error"
    ]
    assert record.error_class == "RuntimeError"
    _assert_absent(caplog, resp.text)


# --- the background fork calls -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_fork_create_logs_only_the_status(network, caplog):
    """Three attempts on a 500; the ERROR line's traceback holds the fixed message."""
    rid = uuid.uuid4()
    create = network.post(f"{CAB}/internal/forks").mock(return_value=_error_status(500))
    caplog.set_level(logging.DEBUG)
    assert await _create_reservation_fork_best_effort(rid, TOPOLOGY, "u", [DEVICE]) is False
    assert create.call_count == 3
    assert "Cabling fork-create returned 500" in caplog.text
    _assert_absent(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 422])
async def test_a_definitive_fork_refusal_logs_the_status_and_device_ids_only(
    network, caplog, status
):
    """The membership shape names a UUID that is logged and a non-UUID entry that is
    not; the rest of cabling's detail is never logged (RES-FORK-2)."""
    rid = uuid.uuid4()
    named = str(uuid.uuid4())
    detail = {
        "error": "fork_device_not_member",
        "device_ids": [named, SENTINEL],
        "message": EXC_TEXT,
    }
    network.post(f"{CAB}/internal/forks").mock(
        return_value=httpx.Response(status, json={"detail": detail})
    )
    caplog.set_level(logging.DEBUG)
    try:
        assert await _create_reservation_fork_best_effort(rid, TOPOLOGY, "u", [DEVICE]) is True
        (record,) = [
            r
            for r in caplog.records
            if r.__dict__.get("action") == "reservation_fork_membership_refused"
        ]
        assert record.status_code == status
        assert record.device_ids == [named]
        assert f"cabling {status}" in record.getMessage()
        _assert_absent(caplog)
    finally:
        _fork_membership_refused.discard(rid)


@pytest.mark.asyncio
async def test_a_failed_fork_archive_logs_only_the_status(network, caplog):
    rid = uuid.uuid4()
    archive = network.post(f"{CAB}/internal/forks/{rid}/archive").mock(
        return_value=_error_status(500)
    )
    caplog.set_level(logging.DEBUG)
    await _archive_reservation_fork_best_effort(rid)
    assert archive.call_count == 3
    assert "Cabling fork-archive returned 500" in caplog.text
    _assert_absent(caplog)


@pytest.mark.asyncio
async def test_a_failed_fork_prune_logs_only_the_status(network, caplog):
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    prune = network.post(f"{CAB}/internal/forks/{rid}/prune-devices").mock(
        return_value=_error_status(503)
    )
    caplog.set_level(logging.DEBUG)
    await _prune_removed_devices_from_fork_best_effort(rid, [DEVICE], attempts=2)
    assert prune.call_count == 2
    assert "Cabling fork prune returned 503" in caplog.text
    _assert_absent(caplog)


@pytest.mark.asyncio
async def test_a_fork_call_that_cannot_reach_cabling_logs_only_the_class(network, caplog):
    rid = uuid.uuid4()
    network.post(f"{CAB}/internal/forks/{rid}/archive").mock(
        side_effect=httpx.ConnectError(EXC_TEXT)
    )
    caplog.set_level(logging.DEBUG)
    await _archive_reservation_fork_best_effort(rid)
    assert "Failed to contact cabling service: ConnectError" in caplog.text
    # The traceback chains the transport error itself; the test raises it with the
    # sentinel, so only its first line, the HERD-worded one, is checked here.
    (record,) = [
        r for r in caplog.records if r.__dict__.get("action") == "reservation_fork_archive_failed"
    ]
    assert SENTINEL not in record.getMessage()
    assert SENTINEL not in str(record.exc_info[1])


# --- the provision-result callback ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "logged"),
    [
        ("provisioning failed: KeyError", "KeyError"),
        (f"provisioning failed: {SENTINEL}", None),
        (EXC_TEXT, None),
        (f"provisioning failed: KeyError\n{SENTINEL}", None),
        (None, None),
    ],
    ids=["documented_form", "form_with_text", "free_text", "form_then_text", "none"],
)
async def test_a_failed_provision_result_logs_only_a_class_name(network, caplog, error, logged):
    """Execution's documented `error` is `provisioning failed: <ClassName>`; only that
    class name is logged, and any other text is not (RES-UPSTREAM-4)."""
    rid = await _insert(ReservationStatus.PENDING_PROVISION)
    async with Session() as s:
        s.add(ReservationDynamicRequest(reservation_id=rid, template_id=TEMPLATE))
        await s.commit()
    network.get(f"{INV}/devices/{DEVICE}/internal").mock(
        return_value=httpx.Response(200, json=_device())
    )
    network.post(f"{INV}/devices/{DEVICE}/status").mock(return_value=httpx.Response(200))
    network.post(f"{CAB}/internal/forks/{rid}/archive").mock(return_value=httpx.Response(204))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post(
            f"/internal/{rid}/provision-result",
            json={"succeeded": False, "error": error},
            headers={"X-Internal-Token": TOKEN},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FAILED"
    (record,) = [
        r
        for r in caplog.records
        if r.__dict__.get("action") == "reservation_provision_result_failed"
    ]
    assert record.error_class == logged
    assert not hasattr(record, "error")
    _assert_absent(caplog, resp.text, await _stored_text())


# --- the utilization report's reads --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fleet_read_logs_only_the_class(network, caplog):
    network.get(f"{INV}/devices").mock(side_effect=httpx.ConnectError(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    assert await fetch_fleet_devices("Bearer t") is None
    assert "building the fleet report (ConnectError)" in caplog.text
    _assert_absent(caplog)


@pytest.mark.asyncio
async def test_the_group_read_logs_only_the_class(network, caplog):
    users = [uuid.uuid4()]
    route = network.post(f"{AUTH}/groups/users/groups")
    route.mock(side_effect=httpx.ReadTimeout(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    assert await fetch_user_groups_map(users, "Bearer t") == {users[0]: []}
    assert "fetching user groups (ReadTimeout)" in caplog.text
    # A body whose entries cannot be converted is logged by the error's class too.
    route.mock(return_value=httpx.Response(200, json={str(users[0]): [{"name": SENTINEL}]}))
    assert await fetch_user_groups_map(users, "Bearer t") == {users[0]: []}
    assert "unexpected error in fetch_user_groups_map (KeyError)" in caplog.text
    _assert_absent(caplog)


@pytest.mark.asyncio
async def test_the_run_count_read_logs_only_the_class_or_status(network, caplog):
    route = network.get(f"{EXE}/runs")
    start, end = NOW - timedelta(days=1), NOW
    route.mock(side_effect=httpx.ConnectError(EXC_TEXT))
    caplog.set_level(logging.DEBUG)
    assert await fetch_execution_run_count(start, end, "Bearer t") is None
    assert "run-count lookup (ConnectError)" in caplog.text
    route.mock(return_value=_error_status(502))
    assert await fetch_execution_run_count(start, end, "Bearer t") is None
    assert "execution run-count returned 502" in caplog.text
    _assert_absent(caplog)
