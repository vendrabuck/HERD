"""Every upstream answer reservations reads, against the bodies it cannot use (#1577).

The invariant: every body the service reads from another service passes through
`herd_common.upstream_body.read_json_body`, and a body a site cannot use (one that
does not parse, one of the wrong JSON type, one missing a field the site needs or
holding it with the wrong type) takes that site's documented failure path. It never
escapes as an unhandled exception, never answers a client-error status for an
upstream fault, and no upstream text or decoder message reaches a response, a row,
or a log line.

Nothing between the route (or the background function) and the network is patched:
respx answers under httpx with the bodies of tests/_upstream_bodies.py, served as raw
bytes with a JSON content type. The tables per site:

- create and PATCH, inventory `POST /devices/batch`: 503 with the fixed
  `INVENTORY_ANSWER_UNUSABLE`, nothing written (RES-CREATE-9, RES-PATCH-6).
- create, cabling `validate/internal`: 503 with the fixed
  `CABLING_VALIDATION_ANSWER_UNUSABLE`, nothing written (RES-TOPO-6).
- create, inventory `GET /templates/{id}`: 503 `INVENTORY_ANSWER_UNUSABLE`
  (RES-DYN-1); inventory `GET /hypervisors/{id}/internal`: 503
  `HYPERVISOR_CHECK_UNAVAILABLE` (RES-DYN-10).
- cancel, release, the provision-result failure, auto-complete, and scheduled
  activation, inventory `GET /devices/{id}/internal`: the device is treated as
  exclusive, so it is released (or, on activation, reserved), and the fork is
  archived, after the terminal status committed (RES-HOLD-5, RES-HOLD-9).
- fork create: a 2xx that cannot be read is a logged failure with a fixed action; a
  409 that cannot be read is still the final refusal (RES-FORK-2).
- prune: a 2xx that cannot be read keeps the marker and is logged with a fixed
  action (RES-FORK-16).
- the active-fork listing: the tick's fork reconcile is skipped (RES-FORK-15).
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from app.config import settings
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.routers.reservations import bearer_scheme
from app.services import reservation_service as svc
from app.services.reservation_service import (
    CABLING_VALIDATION_ANSWER_UNUSABLE,
    HYPERVISOR_CHECK_UNAVAILABLE,
    INVENTORY_ANSWER_UNUSABLE,
    _create_reservation_fork_best_effort,
    _fetch_active_forks,
    _fork_membership_refused,
    _prune_removed_devices_from_fork_best_effort,
)
from app.tasks.expiration import _run_expiration_cycle, _run_fork_archive_reconcile
from fastapi.security import HTTPAuthorizationCredentials
from herd_common.upstream_body import UpstreamBodyUnusable
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests._upstream_bodies import (
    EVERY_BAD_BODY,
    MARKER,
    UNPARSEABLE,
    answer,
    assert_nothing_leaked,
    objects,
)

Session = async_sessionmaker(engine, expire_on_commit=False)

TOKEN = "internal-token-1577"
INV = settings.inventory_service_url
CAB = settings.cabling_service_url
OWNER = uuid.uuid4()
DEVICE = uuid.uuid4()
TEMPLATE = uuid.uuid4()
HYPERVISOR = uuid.uuid4()
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


@pytest.fixture
def network(monkeypatch):
    """respx under httpx for every outbound call; the internal token is set."""
    monkeypatch.setattr(settings, "internal_api_token", TOKEN)
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _override_get_db():
    async with Session() as session:
        yield session


def _client(role: str = "admin") -> AsyncClient:
    """The caller owns OWNER's rows; an admin skips the visibility pre-check."""
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(OWNER),
        "username": "owner",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="caller-jwt"
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _device(device_id=DEVICE, **overrides) -> dict:
    return {
        "id": str(device_id),
        "name": "sw-1",
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "exclusive": True,
        **overrides,
    }


def _booking(**extra) -> dict:
    return {
        "device_ids": [str(DEVICE)],
        "purpose": "issue 1577",
        "start_time": (NOW + timedelta(days=1)).isoformat(),
        "end_time": (NOW + timedelta(days=1, hours=2)).isoformat(),
        **extra,
    }


async def _rows() -> list[Reservation]:
    async with Session() as s:
        return list((await s.execute(select(Reservation))).scalars().all())


async def _insert(status: ReservationStatus, *, start=None, end=None, topology_id=None):
    async with Session() as s:
        res = Reservation(
            user_id=OWNER,
            owner_name="owner",
            device_ids=[str(DEVICE)],
            topology_id=topology_id,
            topology_type=TopologyType.PHYSICAL,
            purpose="issue 1577",
            start_time=start or NOW - timedelta(hours=1),
            end_time=end or NOW + timedelta(hours=2),
            status=status,
        )
        s.add(res)
        await s.commit()
        return res.id


async def _status_of(reservation_id) -> ReservationStatus:
    async with Session() as s:
        return (await s.get(Reservation, reservation_id)).status


# Device records of the right top-level type with a field missing or wrongly typed.
BAD_DEVICE_BATCHES = {
    **objects(
        {},
        {"items": None},
        {"items": "ab"},
        {"items": 5},
        {"items": {"id": str(DEVICE)}},
        {"items": ["sw-1"]},
        {"items": [[str(DEVICE)]]},
    ),
    "no_topology_type": json.dumps({"items": [_device(topology_type=None)]}).encode(),
    "unknown_topology_type": json.dumps({"items": [_device(topology_type="MAINFRAME")]}).encode(),
    "list_topology_type": json.dumps({"items": [_device(topology_type=["PHYSICAL"])]}).encode(),
    "id_not_a_uuid": json.dumps({"items": [_device(id="not-a-uuid")]}).encode(),
    "id_an_integer": json.dumps({"items": [_device(id=7)]}).encode(),
    "name_an_integer": json.dumps({"items": [_device(name=7)]}).encode(),
    "status_null": json.dumps({"items": [_device(status=None)]}).encode(),
    "exclusive_a_string": json.dumps({"items": [_device(exclusive="yes")]}).encode(),
    "exclusive_an_integer": json.dumps({"items": [_device(exclusive=0)]}).encode(),
}
EVERY_BAD_BATCH = {**EVERY_BAD_BODY, **BAD_DEVICE_BATCHES}


# --- create and PATCH: inventory POST /devices/batch ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", EVERY_BAD_BATCH.values(), ids=EVERY_BAD_BATCH.keys())
async def test_create_answers_503_for_a_device_batch_it_cannot_use(network, caplog, body):
    network.post(f"{INV}/devices/batch").mock(return_value=answer(200, body))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": INVENTORY_ANSWER_UNUSABLE}
    assert await _rows() == []
    assert_nothing_leaked(resp.text, caplog.text)


def _inventory_device_response(**overrides) -> dict:
    """A record shaped like inventory's full `DeviceResponse`, extra fields included:
    the honest answer the field checks must accept."""
    record = {
        **_device(),
        "template_id": str(uuid.uuid4()),
        "template_name": "Switch",
        "template_icon": None,
        "template_vendor": "Acme",
        "template_model": "S1",
        "template_part_number": None,
        "field_data": {"rack": "A1", "nested": {"slot": 3}},
        "driver_id": None,
        "driver_name": None,
        "driver_filename": None,
        "connection_type": "ssh",
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
        "created_by": str(uuid.uuid4()),
        "created_by_name": "admin",
        "modified_by": None,
        "modified_by_name": None,
        "poll_interval_seconds": None,
        "resolved_poll_interval_seconds": 60,
    }
    record.update(overrides)
    return record


_WITHOUT_EXCLUSIVE = {k: v for k, v in _inventory_device_response().items() if k != "exclusive"}
HONEST_BATCHES = {
    "full_record": {"items": [_inventory_device_response()], "total": 1},
    "non_exclusive": {"items": [_inventory_device_response(exclusive=False)]},
    # `exclusive` is defaulted in DeviceResponse; a record without it reads as exclusive.
    "no_exclusive_field": {"items": [_WITHOUT_EXCLUSIVE]},
    "cloud": {"items": [_inventory_device_response(topology_type="CLOUD")]},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", HONEST_BATCHES.values(), ids=HONEST_BATCHES.keys())
async def test_create_books_with_a_usable_device_batch(network, batch):
    """The control for the table above: honest answers book, a record without
    `exclusive` included."""
    network.post(f"{INV}/devices/batch").mock(return_value=httpx.Response(200, json=batch))
    async with _client() as ac:
        resp = await ac.post("/", json=_booking())
    assert resp.status_code == 201, resp.text
    assert len(await _rows()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", EVERY_BAD_BATCH.values(), ids=EVERY_BAD_BATCH.keys())
async def test_patch_devices_answers_503_for_a_device_batch_it_cannot_use(network, caplog, body):
    rid = await _insert(
        ReservationStatus.PENDING,
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=2),
    )
    other = uuid.uuid4()
    network.post(f"{INV}/devices/batch").mock(return_value=answer(200, body))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.patch(f"/{rid}", json={"device_ids": [str(DEVICE), str(other)]})
    assert resp.status_code == 503
    assert resp.json() == {"detail": INVENTORY_ANSWER_UNUSABLE}
    (row,) = await _rows()
    assert [str(d) for d in row.device_ids] == [str(DEVICE)]
    assert_nothing_leaked(resp.text, caplog.text)


# --- create: cabling validate/internal ------------------------------------------------

BAD_VALIDATIONS = {
    **EVERY_BAD_BODY,
    **objects(
        {},
        {"valid": "yes"},
        {"valid": None, "invalid_edges": []},
        {"valid": True, "device_ids": "ab"},
        {"valid": True, "device_ids": [1]},
        {"valid": True, "device_ids": {"a": 1}},
        {"valid": False, "invalid_edges": [1]},
        {"valid": False, "invalid_edges": "x"},
        {"valid": False, "invalid_routes": ["x"]},
        {"valid": False, "invalid_routes": 3},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_VALIDATIONS.values(), ids=BAD_VALIDATIONS.keys())
async def test_create_with_a_topology_answers_503_for_a_validation_it_cannot_use(
    network, caplog, body
):
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device()]})
    )
    validate = network.post(f"{CAB}/topologies/{TOPOLOGY}/validate/internal").mock(
        return_value=answer(200, body)
    )
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_booking(topology_id=str(TOPOLOGY)))
    assert validate.call_count == 1
    assert resp.status_code == 503
    assert resp.json() == {"detail": CABLING_VALIDATION_ANSWER_UNUSABLE}
    assert await _rows() == []
    assert_nothing_leaked(resp.text, caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_VALIDATIONS.values(), ids=BAD_VALIDATIONS.keys())
async def test_patch_devices_on_a_topology_answers_503_for_a_validation_it_cannot_use(
    network, caplog, body
):
    """RES-PATCH-8: the same fixed 503 on a device-set PATCH, and the row unchanged."""
    rid = await _insert(
        ReservationStatus.PENDING,
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=2),
        topology_id=TOPOLOGY,
    )
    other = uuid.uuid4()
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device(), _device(other)]})
    )
    validate = network.post(f"{CAB}/topologies/{TOPOLOGY}/validate/internal").mock(
        return_value=answer(200, body)
    )
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.patch(f"/{rid}", json={"device_ids": [str(DEVICE), str(other)]})
    assert validate.call_count == 1
    assert resp.status_code == 503
    assert resp.json() == {"detail": CABLING_VALIDATION_ANSWER_UNUSABLE}
    (row,) = await _rows()
    assert [str(d) for d in row.device_ids] == [str(DEVICE)]
    assert_nothing_leaked(resp.text, caplog.text)


@pytest.mark.asyncio
async def test_create_with_a_topology_books_with_a_usable_validation(network):
    network.post(f"{INV}/devices/batch").mock(
        return_value=httpx.Response(200, json={"items": [_device()]})
    )
    network.post(f"{CAB}/topologies/{TOPOLOGY}/validate/internal").mock(
        return_value=httpx.Response(
            200, json={"valid": True, "invalid_edges": [], "device_ids": [str(DEVICE)]}
        )
    )
    async with _client() as ac:
        resp = await ac.post("/", json=_booking(topology_id=str(TOPOLOGY)))
    assert resp.status_code == 201, resp.text


# --- create: dynamic templates and hypervisors ----------------------------------------


def _template(**overrides) -> dict:
    return {
        "id": str(TEMPLATE),
        "name": "vm",
        "template_type": "dynamic",
        "hypervisor_id": str(HYPERVISOR),
        **overrides,
    }


def _dynamic_booking() -> dict:
    return _booking(device_ids=[], dynamic_requests=[{"template_id": str(TEMPLATE)}])


BAD_TEMPLATES = {
    **EVERY_BAD_BODY,
    **objects(
        {},
        _template(id=None),
        _template(id="not-a-uuid"),
        _template(template_type=None),
        _template(template_type=["dynamic"]),
        _template(hypervisor_id=5),
        _template(hypervisor_id="not-a-uuid"),
        _template(name={"a": 1}),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_TEMPLATES.values(), ids=BAD_TEMPLATES.keys())
async def test_a_dynamic_booking_answers_503_for_a_template_it_cannot_use(network, caplog, body):
    network.get(f"{INV}/templates/{TEMPLATE}").mock(return_value=answer(200, body))
    hypervisor = network.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(
        return_value=httpx.Response(200, json={"enabled": True, "name": "pve"})
    )
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_dynamic_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": INVENTORY_ANSWER_UNUSABLE}
    assert hypervisor.call_count == 0
    assert await _rows() == []
    assert_nothing_leaked(resp.text, caplog.text)


BAD_HYPERVISORS = {
    **EVERY_BAD_BODY,
    **objects({}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "name": ["pve"]}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_HYPERVISORS.values(), ids=BAD_HYPERVISORS.keys())
async def test_a_dynamic_booking_answers_the_fixed_503_for_a_hypervisor_it_cannot_use(
    network, caplog, body
):
    """RES-DYN-10's fixed wording for every body, a deep one included (#1577: a
    RecursionError used to carry its own message through the RuntimeError mapping)."""
    network.get(f"{INV}/templates/{TEMPLATE}").mock(
        return_value=httpx.Response(200, json=_template())
    )
    network.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(return_value=answer(200, body))
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post("/", json=_dynamic_booking())
    assert resp.status_code == 503
    assert resp.json() == {"detail": HYPERVISOR_CHECK_UNAVAILABLE}
    assert await _rows() == []
    assert_nothing_leaked(resp.text, caplog.text)


# --- cancel, release, provision failure: inventory GET /devices/{id}/internal ----------

BAD_DEVICE_RECORDS = {
    **EVERY_BAD_BODY,
    **objects({"exclusive": "no"}, {"exclusive": 0}, {"exclusive": None}, {"exclusive": []}),
}


def _release_network(network, body: bytes):
    """Inventory answers `body` for the device read and accepts the status write;
    cabling accepts the archive. Returns (status route, archive route)."""
    network.get(f"{INV}/devices/{DEVICE}/internal").mock(return_value=answer(200, body))
    status = network.post(f"{INV}/devices/{DEVICE}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    archive = network.post(url__regex=rf"^{CAB}/internal/forks/[^/]+/archive$").mock(
        return_value=httpx.Response(200, json={})
    )
    return status, archive


def _assert_released_and_archived(status, archive, reservation_id) -> None:
    assert status.call_count == 1
    assert json.loads(status.calls.last.request.content) == {"status": "AVAILABLE"}
    assert archive.call_count == 1
    assert str(archive.calls.last.request.url) == (f"{CAB}/internal/forks/{reservation_id}/archive")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_DEVICE_RECORDS.values(), ids=BAD_DEVICE_RECORDS.keys())
async def test_cancel_releases_the_device_and_archives_the_fork_for_a_record_it_cannot_use(
    network, caplog, body
):
    """The worst case of #1577: a cancel whose device read answered a list, a string,
    or null answered 500 after CANCELLED committed, with nothing released and the fork
    not archived. The device is now treated as exclusive (RES-HOLD-5)."""
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    status, archive = _release_network(network, body)
    caplog.set_level(logging.DEBUG)
    async with _client("user") as ac:
        resp = await ac.delete(f"/{rid}")
    assert resp.status_code == 204
    assert await _status_of(rid) == ReservationStatus.CANCELLED
    _assert_released_and_archived(status, archive, rid)
    assert_nothing_leaked(resp.text, caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_DEVICE_RECORDS.values(), ids=BAD_DEVICE_RECORDS.keys())
async def test_release_releases_the_device_and_archives_the_fork_for_a_record_it_cannot_use(
    network, caplog, body
):
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    status, archive = _release_network(network, body)
    caplog.set_level(logging.DEBUG)
    async with _client("user") as ac:
        resp = await ac.put(f"/{rid}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"
    _assert_released_and_archived(status, archive, rid)
    assert_nothing_leaked(resp.text, caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_DEVICE_RECORDS.values(), ids=BAD_DEVICE_RECORDS.keys())
async def test_a_failed_provision_result_releases_the_device_for_a_record_it_cannot_use(
    network, caplog, body
):
    rid = await _insert(ReservationStatus.PENDING_PROVISION, topology_id=TOPOLOGY)
    async with Session() as s:
        s.add(ReservationDynamicRequest(reservation_id=rid, template_id=TEMPLATE))
        await s.commit()
    status, archive = _release_network(network, body)
    caplog.set_level(logging.DEBUG)
    async with _client() as ac:
        resp = await ac.post(
            f"/internal/{rid}/provision-result",
            json={"succeeded": False, "error": "instance create failed"},
            headers={"X-Internal-Token": TOKEN},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "FAILED"
    _assert_released_and_archived(status, archive, rid)
    assert_nothing_leaked(resp.text, caplog.text)


# --- the expiration cycle: auto-complete and scheduled activation ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_DEVICE_RECORDS.values(), ids=BAD_DEVICE_RECORDS.keys())
async def test_the_expiration_cycle_completes_releases_and_activates_through_a_record_it_cannot_use(
    network, caplog, body
):
    """Two ACTIVE rows past their end and one PENDING row due now, each on its own
    device, every device read answering `body`. Before #1577 a list or null raised
    AttributeError out of the cycle after the completions committed: no release, no
    archive, and the due row never activated. Now each completed row's device is
    released and its fork archived (RES-HOLD-9), and the due row activates with its
    device reserved (treated as exclusive)."""
    devices = [uuid.uuid4() for _ in range(3)]
    ended = []
    for device in devices[:2]:
        async with Session() as s:
            row = Reservation(
                user_id=OWNER,
                device_ids=[str(device)],
                topology_type=TopologyType.PHYSICAL,
                start_time=NOW - timedelta(hours=3),
                end_time=NOW - timedelta(hours=1),
                status=ReservationStatus.ACTIVE,
            )
            s.add(row)
            await s.commit()
            ended.append(row.id)
    async with Session() as s:
        due = Reservation(
            user_id=OWNER,
            device_ids=[str(devices[2])],
            topology_type=TopologyType.PHYSICAL,
            start_time=NOW - timedelta(minutes=1),
            end_time=NOW + timedelta(hours=1),
            status=ReservationStatus.PENDING,
        )
        s.add(due)
        await s.commit()
        due_id = due.id
    network.get(url__regex=rf"^{INV}/devices/[^/]+/internal$").mock(return_value=answer(200, body))
    status = network.post(url__regex=rf"^{INV}/devices/[^/]+/status$").mock(
        return_value=httpx.Response(200, json={})
    )
    archive = network.post(url__regex=rf"^{CAB}/internal/forks/[^/]+/archive$").mock(
        return_value=httpx.Response(200, json={})
    )
    caplog.set_level(logging.DEBUG)

    await _run_expiration_cycle()

    assert [await _status_of(rid) for rid in ended] == [ReservationStatus.COMPLETED] * 2
    assert await _status_of(due_id) == ReservationStatus.ACTIVE
    writes = {
        str(call.request.url).split("/devices/")[1].split("/")[0]: json.loads(call.request.content)[
            "status"
        ]
        for call in status.calls
    }
    assert writes == {
        str(devices[0]): "AVAILABLE",
        str(devices[1]): "AVAILABLE",
        str(devices[2]): "RESERVED",
    }
    archived = {str(call.request.url) for call in archive.calls}
    assert archived == {f"{CAB}/internal/forks/{rid}/archive" for rid in ended}
    assert_nothing_leaked(caplog.text)


# --- fork create, prune, and the active-fork listing ------------------------------------

BAD_FORK_CREATES = {
    **EVERY_BAD_BODY,
    **objects({}, {"version_number": "3"}, {"version_number": True}, {"version_number": 1.5}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_FORK_CREATES.values(), ids=BAD_FORK_CREATES.keys())
async def test_a_fork_create_answer_it_cannot_use_is_a_logged_failure(network, caplog, body):
    """One attempt (not retried: a 2xx means cabling committed), False for the sweep's
    give-up counter, nothing staged, and one WARNING with the fixed action."""
    rid = uuid.uuid4()
    create = network.post(f"{CAB}/internal/forks").mock(return_value=answer(201, body))
    caplog.set_level(logging.DEBUG)
    assert await _create_reservation_fork_best_effort(rid, TOPOLOGY, "u", [DEVICE]) is False
    assert create.call_count == 1
    (record,) = [
        r for r in caplog.records if r.__dict__.get("action", "").startswith("reservation_fork")
    ]
    assert record.action == "reservation_fork_create_answer_unusable"
    assert record.reason in {"not_json", "too_deep", "wrong_type", "wrong_field"}
    assert record.exc_info is None
    assert_nothing_leaked(caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 422])
@pytest.mark.parametrize("body", EVERY_BAD_BODY.values(), ids=EVERY_BAD_BODY.keys())
async def test_a_fork_refusal_whose_body_it_cannot_use_is_still_final(network, body, status):
    """RES-FORK-2: a 409 or 422 is final whatever its body; a list used to raise
    AttributeError, so the refusal was retried as a failure."""
    rid = uuid.uuid4()
    create = network.post(f"{CAB}/internal/forks").mock(return_value=answer(status, body))
    try:
        assert await _create_reservation_fork_best_effort(rid, TOPOLOGY, "u", [DEVICE]) is True
        assert create.call_count == 1
        assert rid in _fork_membership_refused
    finally:
        _fork_membership_refused.discard(rid)


BAD_PRUNES = {
    **EVERY_BAD_BODY,
    **objects(
        {},
        {"changed": "yes"},
        {"changed": True},
        {"changed": True, "version_number": "4", "released": []},
        {"changed": True, "version_number": 4, "released": "x"},
        {"changed": True, "version_number": 4, "released": [1]},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_PRUNES.values(), ids=BAD_PRUNES.keys())
async def test_a_prune_answer_it_cannot_use_keeps_the_marker_and_logs_a_fixed_action(
    network, caplog, body
):
    rid = await _insert(ReservationStatus.ACTIVE, topology_id=TOPOLOGY)
    async with Session() as s:
        (await s.get(Reservation, rid)).pending_fork_prune_device_ids = [str(DEVICE)]
        await s.commit()
    prune = network.post(f"{CAB}/internal/forks/{rid}/prune-devices").mock(
        return_value=answer(200, body)
    )
    caplog.set_level(logging.DEBUG)
    await _prune_removed_devices_from_fork_best_effort(rid, [DEVICE], attempts=1)
    assert prune.call_count == 1
    async with Session() as s:
        assert (await s.get(Reservation, rid)).pending_fork_prune_device_ids == [str(DEVICE)]
    actions = [r.__dict__.get("action") for r in caplog.records]
    assert "reservation_fork_prune_answer_unusable" in actions
    assert "reservation_fork_prune_failed" not in actions
    assert_nothing_leaked(caplog.text)


_FORK_ID = str(uuid.uuid4())
BAD_FORK_LISTINGS = {
    **EVERY_BAD_BODY,
    **objects(
        {"forks": "x"},
        {"forks": {"reservation_id": _FORK_ID}},
        {"forks": [MARKER]},
        {"forks": [{"reservation_id": MARKER, "latest_fork_version": 1}]},
        {"forks": [{"reservation_id": 5, "latest_fork_version": 1}]},
        {"forks": [{"latest_fork_version": 1}]},
        {"forks": [{"reservation_id": _FORK_ID, "latest_fork_version": MARKER}]},
        {"forks": [{"reservation_id": _FORK_ID, "latest_fork_version": "3"}]},
        {"forks": [{"reservation_id": _FORK_ID, "latest_fork_version": 2.7}]},
        {"forks": [{"reservation_id": _FORK_ID, "latest_fork_version": True}]},
        {"forks": [{"reservation_id": _FORK_ID}]},
        {"forks": [], "total": MARKER},
        {"forks": [], "total": 1.5},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", BAD_FORK_LISTINGS.values(), ids=BAD_FORK_LISTINGS.keys())
async def test_an_active_fork_listing_it_cannot_use_skips_the_tick_reconcile(network, caplog, body):
    """RES-FORK-15: the listing raises the helper's one type, and the reconcile skips
    the tick (no archive, no heal, no create). A field that would have been converted
    unchecked (`int("<text>")` quotes the text into the caller's exc_info log) is
    refused before it is converted, so nothing it held reaches the log, and a float
    or a boolean is no longer taken as a version."""
    network.get(f"{CAB}/internal/forks").mock(return_value=answer(200, body))
    other = network.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(UpstreamBodyUnusable):
        await _fetch_active_forks()
    await _run_fork_archive_reconcile()
    assert other.call_count == 0
    assert "fork_reconcile_fetch_failed" in [r.__dict__.get("action") for r in caplog.records]
    assert_nothing_leaked(caplog.text)


@pytest.mark.asyncio
async def test_an_honest_active_fork_listing_is_read(network):
    """The control: the declared shape, with an extra field and an explicit total."""
    network.get(f"{CAB}/internal/forks").mock(
        return_value=httpx.Response(
            200,
            json={
                "forks": [
                    {"reservation_id": _FORK_ID, "latest_fork_version": 4, "status": "ACTIVE"}
                ],
                "reservation_ids": [_FORK_ID],
                "total": 1,
            },
        )
    )
    assert await _fetch_active_forks() == [(uuid.UUID(_FORK_ID), 4)]


def test_the_tables_cover_every_unparseable_kind_and_every_json_type():
    """Not vacuous: the shared tables hold each decoder failure and each JSON type."""
    assert set(UNPARSEABLE) == {
        "empty",
        "not_json",
        "not_utf8",
        "huge_integer",
        "deep_array",
        "deep_object",
        "nan",
        "infinity",
        "lone_surrogate",
        "lone_surrogate_key",
    }
    parsed_types = set()
    for name, body in EVERY_BAD_BODY.items():
        if name in UNPARSEABLE:
            continue
        parsed_types.add(type(json.loads(body)).__name__)
    assert parsed_types == {"list", "str", "int", "float", "bool", "NoneType"}
    assert svc.INVENTORY_ANSWER_UNUSABLE == INVENTORY_ANSWER_UNUSABLE
