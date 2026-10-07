"""Dynamic-template gating and mixed bookings (issues #1053, #1033, #1030).

- #1053: a dynamic template is read with the caller's JWT, so a template hidden
  from a non-admin (inventory answers 404) is refused exactly like an unknown id.
- #1033: a dynamic template whose hypervisor is disabled is refused at booking
  with a 422 naming the hypervisor; a hypervisor that cannot be checked is a 503.
- #1030 A(a): the device-set PATCH judges topology-type uniformity over the booked
  set only, so a mixed physical-plus-instance reservation can change its devices
  while keeping its instance device.
- #1030 B(a): reservations' internal `GET /internal/held-devices` lists the
  devices a user's live reservations hold, which inventory reads to grant the
  owner visibility of their own instance device.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
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
from app.schemas.reservation import ReservationUpdate
from app.services import reservation_service as svc
from app.services.reservation_service import (
    HYPERVISOR_CHECK_UNAVAILABLE,
    update_reservation,
)
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

Session = async_sessionmaker(engine, expire_on_commit=False)

SVC = "app.services.reservation_service"
INV = "http://inventory:8000"
INTERNAL_TOKEN = "internal-test-token"
NOW = datetime.now(timezone.utc)
START = NOW.isoformat()
END = (NOW + timedelta(hours=3)).isoformat()

OWNER = uuid.uuid4()
PHYS_A = uuid.uuid4()
PHYS_B = uuid.uuid4()
INSTANCE = uuid.uuid4()
DYN_TEMPLATE = uuid.uuid4()
HYPERVISOR = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def internal_token(monkeypatch):
    monkeypatch.setattr(svc.settings, "internal_api_token", INTERNAL_TOKEN)


async def _override_get_db():
    async with Session() as session:
        yield session


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": str(OWNER),
        "username": "owner",
        "role": "user",
    }
    app.dependency_overrides[bearer_scheme] = lambda: HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="caller-jwt"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _template(template_id=DYN_TEMPLATE, hypervisor_id=HYPERVISOR, name="ubuntu-vm") -> dict:
    return {
        "id": str(template_id),
        "name": name,
        "template_type": "dynamic",
        "hypervisor_id": str(hypervisor_id) if hypervisor_id else None,
        "sections": [],
    }


def _hypervisor(enabled: bool, name="lab-pve") -> dict:
    return {
        "id": str(HYPERVISOR),
        "name": name,
        "endpoint": "https://pve.example",
        "hypervisor_type": "proxmox",
        "secret_id": str(uuid.uuid4()),
        "enabled": enabled,
    }


def _booking() -> dict:
    return {
        "device_ids": [],
        "dynamic_requests": [{"template_id": str(DYN_TEMPLATE)}],
        "purpose": "gating",
        "start_time": START,
        "end_time": END,
    }


async def _reservation_count() -> int:
    async with Session() as s:
        return len((await s.execute(select(Reservation))).scalars().all())


# --- issue #1053: the template is read as the caller ---


async def test_hidden_dynamic_template_refused_exactly_like_an_unknown_id(client):
    with respx.mock:
        route = respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(404, json={"detail": "Template not found"})
        )
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"Template {DYN_TEMPLATE} not found in inventory"
    # The gate lives in inventory and depends on the caller's own JWT reaching it.
    assert route.calls.last.request.headers["Authorization"] == "Bearer caller-jwt"
    assert await _reservation_count() == 0


async def test_hidden_template_never_reaches_the_hypervisor_check(client, internal_token):
    with respx.mock:
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(return_value=httpx.Response(404))
        hv = respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(
            return_value=httpx.Response(200, json=_hypervisor(enabled=False))
        )
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 422
    assert "lab-pve" not in resp.json()["detail"]
    assert hv.call_count == 0


# --- issue #1033: a disabled hypervisor is refused at booking ---


async def test_disabled_hypervisor_refused_with_422_naming_it(client, internal_token):
    with respx.mock:
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(200, json=_template())
        )
        hv = respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(
            return_value=httpx.Response(200, json=_hypervisor(enabled=False))
        )
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Hypervisor 'lab-pve' is disabled; these dynamic templates cannot be booked "
        "until an admin enables it: ubuntu-vm"
    )
    assert hv.calls.last.request.headers["X-Internal-Token"] == INTERNAL_TOKEN
    assert await _reservation_count() == 0


async def test_enabled_hypervisor_books(client, internal_token):
    with (
        respx.mock,
        patch(f"{SVC}._create_reservation_fork_best_effort", new=AsyncMock()),
    ):
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(200, json=_template())
        )
        respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(
            return_value=httpx.Response(200, json=_hypervisor(enabled=True))
        )
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "PENDING_PROVISION"


async def test_each_distinct_hypervisor_is_read_once(internal_token):
    second = uuid.uuid4()
    templates = [_template(), _template(template_id=second, name="debian-vm")]
    requests = [SimpleNamespace(template_id=t) for t in (DYN_TEMPLATE, second)]
    with (
        respx.mock,
        patch(f"{SVC}._fetch_dynamic_templates", new=AsyncMock(return_value=templates)),
    ):
        hv = respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(
            return_value=httpx.Response(200, json=_hypervisor(enabled=False))
        )
        with pytest.raises(ValueError) as exc:
            await svc._validate_dynamic_requests(requests, "jwt")
    assert hv.call_count == 1
    assert str(exc.value).endswith("until an admin enables it: ubuntu-vm, debian-vm")


async def test_missing_hypervisor_refused_with_422(client, internal_token):
    with respx.mock:
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(200, json=_template())
        )
        respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal").mock(return_value=httpx.Response(404))
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "The hypervisor of these dynamic templates no longer exists: ubuntu-vm"
    )


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(403),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"id": str(HYPERVISOR), "name": "lab-pve"}),
        httpx.Response(200, json={"enabled": "false"}),
        httpx.ConnectError("refused"),
    ],
    ids=["5xx", "403", "unparseable", "no-enabled", "enabled-not-bool", "transport"],
)
async def test_unanswerable_hypervisor_check_fails_closed_with_503(client, internal_token, answer):
    with respx.mock:
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(200, json=_template())
        )
        route = respx.get(f"{INV}/hypervisors/{HYPERVISOR}/internal")
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json()["detail"] == HYPERVISOR_CHECK_UNAVAILABLE
    assert await _reservation_count() == 0


async def test_hypervisor_check_without_an_internal_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(svc.settings, "internal_api_token", "")
    with respx.mock:
        respx.get(f"{INV}/templates/{DYN_TEMPLATE}").mock(
            return_value=httpx.Response(200, json=_template())
        )
        resp = await client.post("/", json=_booking())
    assert resp.status_code == 503
    assert resp.json()["detail"] == HYPERVISOR_CHECK_UNAVAILABLE


# --- issue #1030 A(a): PATCH uniformity covers the booked set only ---


def _device(device_id, topology_type="PHYSICAL", template_id=None) -> dict:
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "template_id": str(template_id or uuid.uuid4()),
        "topology_type": topology_type,
        "status": "AVAILABLE",
        "exclusive": True,
    }


async def _insert_mixed(status=ReservationStatus.ACTIVE, dynamic=True) -> uuid.UUID:
    rid = uuid.uuid4()
    async with Session() as s:
        res = Reservation(
            id=rid,
            user_id=OWNER,
            device_ids=[str(PHYS_A), str(INSTANCE)],
            topology_type=TopologyType.PHYSICAL,
            purpose="mixed",
            start_time=NOW - timedelta(minutes=5),
            end_time=NOW + timedelta(hours=2),
            status=status,
        )
        if dynamic:
            res.dynamic_requests = [ReservationDynamicRequest(template_id=DYN_TEMPLATE)]
        s.add(res)
        await s.commit()
    return rid


def _inventory(devices: dict):
    async def fetch(ids, token):
        return [devices[str(i)] for i in ids]

    return fetch


@pytest.fixture
def patch_seams():
    with (
        patch(
            f"{SVC}._update_device_statuses", new=AsyncMock(side_effect=lambda ids, *a, **k: ids)
        ),
        patch(f"{SVC}._fetch_devices_best_effort", new=AsyncMock(return_value=[])),
        patch(f"{SVC}._prune_removed_devices_from_fork_best_effort", new=AsyncMock()),
    ):
        yield


DEVICES = {
    str(PHYS_A): _device(PHYS_A),
    str(PHYS_B): _device(PHYS_B),
    str(INSTANCE): _device(INSTANCE, "CLOUD", DYN_TEMPLATE),
}


async def _patch(rid, device_ids, devices=DEVICES):
    with patch(f"{SVC}._fetch_devices", new=_inventory(devices)):
        async with Session() as db:
            return await update_reservation(
                db, rid, OWNER, ReservationUpdate(device_ids=device_ids), token="t"
            )


async def test_mixed_reservation_adds_a_device_and_keeps_its_instance(patch_seams):
    rid = await _insert_mixed()
    out = await _patch(rid, [PHYS_A, PHYS_B, INSTANCE])
    assert {str(d) for d in out.device_ids} == {str(PHYS_A), str(PHYS_B), str(INSTANCE)}


async def test_mixed_reservation_can_drop_a_physical_device_and_keep_its_instance(patch_seams):
    rid = await _insert_mixed()
    out = await _patch(rid, [INSTANCE, PHYS_B])
    assert {str(d) for d in out.device_ids} == {str(PHYS_B), str(INSTANCE)}


async def test_a_cloud_device_the_reservation_does_not_hold_is_still_refused(patch_seams):
    rid = await _insert_mixed()
    foreign = uuid.uuid4()
    devices = {**DEVICES, str(foreign): _device(foreign, "CLOUD", DYN_TEMPLATE)}
    with pytest.raises(ValueError) as exc:
        await _patch(rid, [PHYS_A, INSTANCE, foreign], devices)
    assert str(exc.value).startswith("All devices must share the same topology type. Found: ")


async def test_a_held_cloud_device_of_another_template_is_still_refused(patch_seams):
    rid = await _insert_mixed()
    devices = {**DEVICES, str(INSTANCE): _device(INSTANCE, "CLOUD", uuid.uuid4())}
    with pytest.raises(ValueError) as exc:
        await _patch(rid, [PHYS_A, PHYS_B, INSTANCE], devices)
    assert str(exc.value).startswith("All devices must share the same topology type. Found: ")


async def test_without_dynamic_requests_the_whole_set_is_judged(patch_seams):
    rid = await _insert_mixed(dynamic=False)
    with pytest.raises(ValueError) as exc:
        await _patch(rid, [PHYS_A, PHYS_B, INSTANCE])
    assert str(exc.value).startswith("All devices must share the same topology type. Found: ")


async def test_mixed_patch_through_the_route_is_200(client, patch_seams):
    rid = await _insert_mixed()
    with (
        patch(f"{SVC}._fetch_devices", new=_inventory(DEVICES)),
        patch(
            "app.routers.reservations._fetch_visible_device_ids",
            new=AsyncMock(return_value={str(PHYS_A), str(PHYS_B), str(INSTANCE)}),
        ),
    ):
        resp = await client.patch(
            f"/{rid}", json={"device_ids": [str(PHYS_A), str(PHYS_B), str(INSTANCE)]}
        )
    assert resp.status_code == 200, resp.text


# --- issue #1030 B(a): the held-devices internal route ---


async def _insert_row(user_id, status, device_ids) -> uuid.UUID:
    rid = uuid.uuid4()
    async with Session() as s:
        s.add(
            Reservation(
                id=rid,
                user_id=user_id,
                device_ids=[str(d) for d in device_ids],
                topology_type=TopologyType.CLOUD,
                purpose="held",
                start_time=NOW - timedelta(minutes=5),
                end_time=NOW + timedelta(hours=2),
                status=status,
            )
        )
        await s.commit()
    return rid


@pytest.fixture
async def internal_client(monkeypatch):
    monkeypatch.setattr("app.routers.reservations.settings.internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_held_devices_lists_only_the_users_live_reservations(internal_client):
    live_active, live_provision, pending, done, foreign = (uuid.uuid4() for _ in range(5))
    await _insert_row(OWNER, ReservationStatus.ACTIVE, [live_active, live_provision])
    await _insert_row(OWNER, ReservationStatus.PENDING_PROVISION, [live_provision])
    await _insert_row(OWNER, ReservationStatus.PENDING, [pending])
    for terminal in (
        ReservationStatus.COMPLETED,
        ReservationStatus.CANCELLED,
        ReservationStatus.FAILED,
    ):
        await _insert_row(OWNER, terminal, [done])
    await _insert_row(uuid.uuid4(), ReservationStatus.ACTIVE, [foreign])

    resp = await internal_client.get(
        "/internal/held-devices",
        params={"user_id": str(OWNER)},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"device_ids": sorted([str(live_active), str(live_provision)])}


async def test_held_devices_for_a_user_with_nothing_live_is_empty(internal_client):
    resp = await internal_client.get(
        "/internal/held-devices",
        params={"user_id": str(uuid.uuid4())},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"device_ids": []}


async def test_held_devices_refuses_a_wrong_internal_token(internal_client):
    resp = await internal_client.get(
        "/internal/held-devices",
        params={"user_id": str(OWNER)},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"
