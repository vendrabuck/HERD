"""Dynamic-resource tests for the reservations service (ADR 0004, issue #32).

Covers the phase 3 surface: booking-time dynamic requests and their inventory
validation, the gated PENDING_PROVISION activation, the provision_requested
outbox payload (field names pinned byte-for-byte against the execution
consumer), the internal provision-result callback (auth, idempotency, success
and failure transitions), and the expiration task's timeout backstop.

Uses app.database's engine (like test_expiration.py) so the API paths and the
expiration cycle share one in-memory SQLite database.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from app.database import Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
)
from app.routers.reservations import bearer_scheme
from app.tasks.expiration import _run_expiration_cycle
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = str(uuid.uuid4())
DEVICE_A = str(uuid.uuid4())
TEMPLATE_ID = str(uuid.uuid4())
INTERNAL_TOKEN = "internal-test-token"

NOW = datetime.now(timezone.utc)
START = NOW.isoformat()
END = (NOW + timedelta(hours=3)).isoformat()


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth():
    return {"sub": USER_ID, "username": "testuser", "role": "user"}


def override_bearer():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def internal_client(monkeypatch):
    monkeypatch.setattr("app.routers.reservations.settings.internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def make_device_response(device_id: str, exclusive: bool = True) -> dict:
    return {
        "id": device_id,
        "name": f"device-{device_id[:8]}",
        "template_id": str(uuid.uuid4()),
        "template_name": "Firewall",
        "template_icon": None,
        "topology_type": "PHYSICAL",
        "status": "AVAILABLE",
        "field_data": {},
        "exclusive": exclusive,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def make_template_response(template_id: str, template_type: str = "dynamic") -> dict:
    return {
        "id": template_id,
        "name": f"template-{template_id[:8]}",
        "template_type": template_type,
        "sections": [],
    }


async def _outbox_rows(subject=None):
    async with TestSessionLocal() as session:
        stmt = select(OutboxEvent)
        if subject is not None:
            stmt = stmt.where(OutboxEvent.subject == subject)
        return (await session.execute(stmt.order_by(OutboxEvent.created_at))).scalars().all()


async def _get_reservation(res_id) -> Reservation:
    async with TestSessionLocal() as session:
        result = await session.execute(
            select(Reservation).where(Reservation.id == uuid.UUID(str(res_id)))
        )
        return result.scalar_one()


async def _dynamic_request_rows(res_id=None):
    async with TestSessionLocal() as session:
        stmt = select(ReservationDynamicRequest)
        if res_id is not None:
            stmt = stmt.where(ReservationDynamicRequest.reservation_id == uuid.UUID(str(res_id)))
        return (await session.execute(stmt)).scalars().all()


async def _create_dynamic_reservation(
    client,
    *,
    template_ids=None,
    device_ids=None,
    exclusive=True,
    fork_mock=None,
    update_mock=None,
    **overrides,
):
    """Create a reservation through the API with inventory/cabling mocked out."""
    if template_ids is None:
        template_ids = [TEMPLATE_ID]
    if device_ids is None:
        device_ids = [DEVICE_A]
    devices = [make_device_response(d, exclusive=exclusive) for d in device_ids]
    templates = [make_template_response(t) for t in dict.fromkeys(template_ids)]
    body = {
        "device_ids": device_ids,
        "dynamic_requests": [{"template_id": t} for t in template_ids],
        "purpose": "dynamic test",
        "start_time": START,
        "end_time": END,
        **overrides,
    }
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        patch(
            "app.services.reservation_service._fetch_dynamic_templates",
            new=AsyncMock(return_value=templates),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=update_mock if update_mock is not None else AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._create_reservation_fork_best_effort",
            new=fork_mock if fork_mock is not None else AsyncMock(),
        ),
    ):
        return await client.post("/", json=body)


async def _insert_reservation(
    status: ReservationStatus,
    *,
    device_ids=None,
    dynamic_template_ids=None,
    start_time=None,
    end_time=None,
    updated_at=None,
) -> uuid.UUID:
    res_id = uuid.uuid4()
    res = Reservation(
        id=res_id,
        user_id=uuid.UUID(USER_ID),
        owner_name="testuser",
        device_ids=device_ids if device_ids is not None else [DEVICE_A],
        topology_type="PHYSICAL",
        purpose="dynamic test",
        start_time=start_time or NOW - timedelta(minutes=5),
        end_time=end_time or NOW + timedelta(hours=3),
        status=status,
    )
    if updated_at is not None:
        res.updated_at = updated_at
    if dynamic_template_ids:
        res.dynamic_requests = [
            ReservationDynamicRequest(template_id=uuid.UUID(t)) for t in dynamic_template_ids
        ]
    async with TestSessionLocal() as session:
        session.add(res)
        await session.commit()
    return res_id


# --- Booking: gated PENDING_PROVISION and request rows -----------------------


async def test_dynamic_booking_lands_pending_provision(client):
    resp = await _create_dynamic_reservation(client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING_PROVISION"
    assert len(data["dynamic_requests"]) == 1
    assert data["dynamic_requests"][0]["template_id"] == TEMPLATE_ID


async def test_dynamic_booking_creates_one_row_per_instance(client):
    """Two requests for the same template are two instances, two rows."""
    resp = await _create_dynamic_reservation(client, template_ids=[TEMPLATE_ID, TEMPLATE_ID])
    assert resp.status_code == 201
    rows = await _dynamic_request_rows(resp.json()["id"])
    assert len(rows) == 2
    assert {str(r.template_id) for r in rows} == {TEMPLATE_ID}
    assert len({r.id for r in rows}) == 2


async def test_dynamic_booking_stages_provision_requested_payload_exactly(client):
    """The outbox payload is pinned byte-for-byte: the execution consumer's
    _handle_provision_requested reads reservation_id, user_id, and
    dynamic_requests[].{id,template_id}; event_id is the consumer dedupe key."""
    resp = await _create_dynamic_reservation(client)
    assert resp.status_code == 201
    data = resp.json()
    rows = await _outbox_rows("herd.reservations.provision_requested")
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    event_id = payload.pop("event_id")
    assert str(uuid.UUID(event_id)) == event_id
    assert payload == {
        "event": "reservation.provision_requested",
        "reservation_id": data["id"],
        "user_id": USER_ID,
        "device_ids": [DEVICE_A],
        "topology_id": None,
        "topology_type": "PHYSICAL",
        "dynamic_requests": [{"id": data["dynamic_requests"][0]["id"], "template_id": TEMPLATE_ID}],
        "purpose_category": None,
    }


async def test_dynamic_booking_no_created_event_no_fork(client):
    fork_mock = AsyncMock()
    resp = await _create_dynamic_reservation(client, fork_mock=fork_mock)
    assert resp.status_code == 201
    assert await _outbox_rows("herd.reservations.created") == []
    fork_mock.assert_not_awaited()


async def test_dynamic_booking_still_flips_exclusive_devices(client):
    update_mock = AsyncMock()
    resp = await _create_dynamic_reservation(client, update_mock=update_mock)
    assert resp.status_code == 201
    update_mock.assert_called_once()
    assert update_mock.call_args[0][1] == "RESERVED"


async def test_dynamic_booking_non_exclusive_devices_still_gated(client):
    """No inventory flip to wait for, but dynamic requests still gate ACTIVE;
    provision_requested is staged atomically with the booking."""
    update_mock = AsyncMock()
    resp = await _create_dynamic_reservation(client, exclusive=False, update_mock=update_mock)
    assert resp.status_code == 201
    assert resp.json()["status"] == "PENDING_PROVISION"
    update_mock.assert_not_called()
    assert len(await _outbox_rows("herd.reservations.provision_requested")) == 1
    assert await _outbox_rows("herd.reservations.created") == []


async def test_dynamic_booking_scheduled_future_stays_pending(client):
    """A scheduled dynamic booking holds the window as PENDING; the event is
    published by the claim path at start_time, not at booking."""
    resp = await _create_dynamic_reservation(
        client,
        start_time=(NOW + timedelta(hours=2)).isoformat(),
        end_time=(NOW + timedelta(hours=4)).isoformat(),
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "PENDING"
    assert await _outbox_rows("herd.reservations.provision_requested") == []
    assert len(await _dynamic_request_rows(resp.json()["id"])) == 1


# --- Booking: regression, zero dynamic requests behave exactly as today ------


async def test_zero_dynamic_regression_activates_immediately(client):
    fork_mock = AsyncMock()
    resp = await _create_dynamic_reservation(client, template_ids=[], fork_mock=fork_mock)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "ACTIVE"
    assert data["dynamic_requests"] == []
    assert len(await _outbox_rows("herd.reservations.created")) == 1
    assert await _outbox_rows("herd.reservations.provision_requested") == []
    fork_mock.assert_awaited_once()


async def test_omitted_dynamic_field_regression_activates_immediately(client):
    """A request body without the dynamic_requests field at all is unchanged."""
    devices = [make_device_response(DEVICE_A)]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._create_reservation_fork_best_effort",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    assert resp.status_code == 201
    assert resp.json()["status"] == "ACTIVE"
    assert await _outbox_rows("herd.reservations.provision_requested") == []


# --- Booking: dynamic-only (no physical device) ------------------------------


async def test_dynamic_only_booking_lands_pending_provision_cloud(client):
    """A booking with zero devices and a dynamic request is accepted, books
    PENDING_PROVISION, and derives topology_type CLOUD (issue #274)."""
    resp = await _create_dynamic_reservation(client, device_ids=[])
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "PENDING_PROVISION"
    assert data["device_ids"] == []
    assert data["topology_type"] == "CLOUD"
    assert len(data["dynamic_requests"]) == 1


async def test_dynamic_only_stages_provision_requested_cloud(client):
    """The provision_requested payload for a dynamic-only booking carries an
    empty device list and topology_type CLOUD."""
    resp = await _create_dynamic_reservation(client, device_ids=[])
    assert resp.status_code == 201
    data = resp.json()
    rows = await _outbox_rows("herd.reservations.provision_requested")
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    payload.pop("event_id")
    assert payload == {
        "event": "reservation.provision_requested",
        "reservation_id": data["id"],
        "user_id": USER_ID,
        "device_ids": [],
        "topology_id": None,
        "topology_type": "CLOUD",
        "dynamic_requests": [{"id": data["dynamic_requests"][0]["id"], "template_id": TEMPLATE_ID}],
        "purpose_category": None,
    }
    assert await _outbox_rows("herd.reservations.created") == []


async def test_dynamic_only_booking_flips_no_devices(client):
    """With no physical devices there is no inventory status flip to make."""
    update_mock = AsyncMock()
    resp = await _create_dynamic_reservation(client, device_ids=[], update_mock=update_mock)
    assert resp.status_code == 201
    update_mock.assert_not_called()


async def test_dynamic_only_conflict_detection_unaffected(client):
    """Two dynamic-only bookings in the same window do not conflict: there are
    no physical devices, so the conflict query never degenerates or reports a
    false collision."""
    first = await _create_dynamic_reservation(client, device_ids=[])
    second = await _create_dynamic_reservation(client, device_ids=[])
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_neither_device_nor_dynamic_request_422_wording(client):
    """A booking with neither a device nor a dynamic request is rejected with a
    pinned 422 (schema-level model validation)."""
    resp = await client.post(
        "/",
        json={"device_ids": [], "start_time": START, "end_time": END},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert (
        detail[0]["msg"]
        == "Value error, A reservation must include at least one device or dynamic request"
    )


async def test_dynamic_only_callback_success_attaches_instances(internal_client):
    """A success callback activates a dynamic-only reservation and attaches the
    materialized instance device ids (the reservation started with none)."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        device_ids=[],
        dynamic_template_ids=[TEMPLATE_ID],
    )
    inst_a = str(uuid.uuid4())
    inst_b = str(uuid.uuid4())
    fork_mock = AsyncMock()
    with patch(
        "app.services.reservation_service._create_reservation_fork_best_effort",
        new=fork_mock,
    ):
        resp = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json={"succeeded": True, "device_ids": [inst_a, inst_b], "error": None},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "ACTIVE", "applied": True}

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert {str(d) for d in res.device_ids} == {inst_a, inst_b}
    fork_mock.assert_awaited_once()

    created = await _outbox_rows("herd.reservations.created")
    assert len(created) == 1
    assert set(created[0].payload["device_ids"]) == {inst_a, inst_b}


async def test_dynamic_only_callback_failure_no_device_release(internal_client):
    """A failure callback on a dynamic-only reservation lands FAILED and stages
    reservation.failed with no device-release side effect: the exclusive set is
    empty, so the release path is a clean no-op."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        device_ids=[],
        dynamic_template_ids=[TEMPLATE_ID],
    )
    update_mock = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=update_mock,
        ),
    ):
        resp = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json={"succeeded": False, "device_ids": [], "error": "create_instance failed"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "FAILED", "applied": True}

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    failed = await _outbox_rows("herd.reservations.failed")
    assert len(failed) == 1
    assert failed[0].payload["reservation_id"] == str(rid)
    update_mock.assert_not_called()


async def test_dynamic_only_timeout_backstop_no_device_release():
    """The timeout backstop fails a stuck dynamic-only reservation cleanly, with
    no device release for the empty exclusive set."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        device_ids=[],
        dynamic_template_ids=[TEMPLATE_ID],
        updated_at=NOW - timedelta(hours=2),
    )
    update_mock = AsyncMock()
    p1, p2, p3, p4 = _patch_expiration_inventory(update_mock=update_mock)
    # device_ids is empty, so the release loop's zip over (ids, fetch_results)
    # is empty and the mocked fetch return value is immaterial.
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    failed = await _outbox_rows("herd.reservations.failed")
    assert len(failed) == 1
    assert failed[0].payload["reservation_id"] == str(rid)
    update_mock.assert_not_called()


# --- Booking: template validation against inventory --------------------------


async def test_dynamic_booking_unknown_template_422_wording(client):
    devices = [make_device_response(DEVICE_A)]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        respx.mock,
    ):
        respx.get(f"http://inventory:8000/templates/{TEMPLATE_ID}").mock(
            return_value=httpx.Response(404)
        )
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "dynamic_requests": [{"template_id": TEMPLATE_ID}],
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == f"Template {TEMPLATE_ID} not found in inventory"


async def test_dynamic_booking_non_dynamic_template_422_wording(client):
    devices = [make_device_response(DEVICE_A)]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        respx.mock,
    ):
        respx.get(f"http://inventory:8000/templates/{TEMPLATE_ID}").mock(
            return_value=httpx.Response(
                200, json=make_template_response(TEMPLATE_ID, template_type="device")
            )
        )
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "dynamic_requests": [{"template_id": TEMPLATE_ID}],
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert (
        resp.json()["detail"] == f"The following templates are not dynamic templates: {TEMPLATE_ID}"
    )


async def test_dynamic_booking_inventory_unreachable_503_wording(client):
    devices = [make_device_response(DEVICE_A)]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        respx.mock,
    ):
        respx.get(f"http://inventory:8000/templates/{TEMPLATE_ID}").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "dynamic_requests": [{"template_id": TEMPLATE_ID}],
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 503
    assert resp.json()["detail"].startswith("Failed to contact inventory service:")


# --- Provision-result callback: auth ------------------------------------------


async def test_callback_wrong_token_403_wording(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    resp = await internal_client.post(
        f"/internal/{rid}/provision-result",
        headers={"X-Internal-Token": "wrong-token"},
        json={"succeeded": True, "device_ids": [], "error": None},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


async def test_callback_missing_token_rejected(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    resp = await internal_client.post(
        f"/internal/{rid}/provision-result",
        json={"succeeded": True, "device_ids": [], "error": None},
    )
    assert resp.status_code == 422


async def test_callback_unknown_reservation_404(internal_client):
    resp = await internal_client.post(
        f"/internal/{uuid.uuid4()}/provision-result",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        json={"succeeded": True, "device_ids": [], "error": None},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"


# --- Provision-result callback: success ---------------------------------------


async def test_callback_success_activates_and_attaches_devices(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    dyn_device = str(uuid.uuid4())
    fork_mock = AsyncMock()
    with patch(
        "app.services.reservation_service._create_reservation_fork_best_effort",
        new=fork_mock,
    ):
        resp = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json={"succeeded": True, "device_ids": [dyn_device], "error": None},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "ACTIVE", "applied": True}

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert {str(d) for d in res.device_ids} == {DEVICE_A, dyn_device}
    fork_mock.assert_awaited_once()

    created = await _outbox_rows("herd.reservations.created")
    assert len(created) == 1
    payload = created[0].payload
    assert payload["event"] == "reservation.created"
    assert payload["reservation_id"] == str(rid)
    # The dynamic device is in the event, so execution provisions it too.
    assert set(payload["device_ids"]) == {DEVICE_A, dyn_device}


async def test_callback_repeat_success_is_noop(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    dyn_device = str(uuid.uuid4())
    body = {"succeeded": True, "device_ids": [dyn_device], "error": None}
    with patch(
        "app.services.reservation_service._create_reservation_fork_best_effort",
        new=AsyncMock(),
    ):
        first = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json=body,
        )
        second = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json=body,
        )
    assert first.json()["applied"] is True
    assert second.status_code == 200
    assert second.json() == {"reservation_id": str(rid), "status": "ACTIVE", "applied": False}
    assert len(await _outbox_rows("herd.reservations.created")) == 1
    res = await _get_reservation(rid)
    assert len(res.device_ids) == 2


# --- Provision-result callback: failure ---------------------------------------


async def test_callback_failure_fails_and_stages_failed_event(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    update_mock = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=update_mock,
        ),
    ):
        resp = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json={"succeeded": False, "device_ids": [], "error": "create_instance failed"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "FAILED", "applied": True}

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    failed = await _outbox_rows("herd.reservations.failed")
    assert len(failed) == 1
    assert failed[0].payload["event"] == "reservation.failed"
    assert failed[0].payload["reservation_id"] == str(rid)
    assert await _outbox_rows("herd.reservations.created") == []
    # The exclusive physical device is released, so it is not orphaned RESERVED.
    update_mock.assert_called_once()
    assert [str(d) for d in update_mock.call_args[0][0]] == [DEVICE_A]
    assert update_mock.call_args[0][1] == "AVAILABLE"


async def test_callback_repeat_failure_is_noop(internal_client):
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    body = {"succeeded": False, "device_ids": [], "error": "boom"}
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json=body,
        )
        second = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json=body,
        )
    assert second.status_code == 200
    assert second.json() == {"reservation_id": str(rid), "status": "FAILED", "applied": False}
    assert len(await _outbox_rows("herd.reservations.failed")) == 1


async def test_callback_after_failed_does_not_resurrect(internal_client):
    """A success callback landing after the timeout backstop already failed the
    reservation must not resurrect it."""
    rid = await _insert_reservation(ReservationStatus.FAILED, dynamic_template_ids=[TEMPLATE_ID])
    resp = await internal_client.post(
        f"/internal/{rid}/provision-result",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        json={"succeeded": True, "device_ids": [str(uuid.uuid4())], "error": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "FAILED", "applied": False}
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    assert [str(d) for d in res.device_ids] == [DEVICE_A]
    assert await _outbox_rows("herd.reservations.created") == []


async def test_callback_after_cancel_does_not_retransition(internal_client):
    rid = await _insert_reservation(ReservationStatus.CANCELLED, dynamic_template_ids=[TEMPLATE_ID])
    resp = await internal_client.post(
        f"/internal/{rid}/provision-result",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        json={"succeeded": True, "device_ids": [str(uuid.uuid4())], "error": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "CANCELLED", "applied": False}
    assert (await _get_reservation(rid)).status == ReservationStatus.CANCELLED


# --- Expiration task: scheduled claim path and timeout backstop ---------------


def _patch_expiration_inventory(update_mock=None):
    """Patch inventory access for expiration-cycle tests in both namespaces
    (the cycle's own imports and the release helper's home module)."""
    device = make_device_response(DEVICE_A)
    return (
        patch(
            "app.tasks.expiration._fetch_devices_best_effort",
            new=AsyncMock(return_value=[device]),
        ),
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[device]),
        ),
        patch(
            "app.tasks.expiration._update_device_statuses",
            new=update_mock if update_mock is not None else AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=update_mock if update_mock is not None else AsyncMock(),
        ),
    )


async def test_scheduled_dynamic_claim_gates_activation():
    """The claim path publishes provision_requested and stays PENDING_PROVISION."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING,
        dynamic_template_ids=[TEMPLATE_ID],
        start_time=NOW - timedelta(minutes=5),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    fork_mock = AsyncMock()
    with (
        p1,
        p2,
        p3,
        p4,
        patch("app.tasks.expiration._create_reservation_fork_best_effort", new=fork_mock),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING_PROVISION
    events = await _outbox_rows("herd.reservations.provision_requested")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["event"] == "reservation.provision_requested"
    assert payload["reservation_id"] == str(rid)
    assert len(payload["dynamic_requests"]) == 1
    assert payload["dynamic_requests"][0]["template_id"] == TEMPLATE_ID
    assert await _outbox_rows("herd.reservations.created") == []
    fork_mock.assert_not_awaited()


async def test_scheduled_physical_claim_still_activates():
    """Regression: the claim path without dynamic requests activates as today."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING, start_time=NOW - timedelta(minutes=5)
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    with (
        p1,
        p2,
        p3,
        p4,
        patch("app.tasks.expiration._create_reservation_fork_best_effort", new=AsyncMock()),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert len(await _outbox_rows("herd.reservations.created")) == 1
    assert await _outbox_rows("herd.reservations.provision_requested") == []


async def test_timeout_backstop_fails_stuck_dynamic_reservation():
    stale = NOW - timedelta(hours=2)
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        dynamic_template_ids=[TEMPLATE_ID],
        updated_at=stale,
    )
    update_mock = AsyncMock()
    p1, p2, p3, p4 = _patch_expiration_inventory(update_mock=update_mock)
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    failed = await _outbox_rows("herd.reservations.failed")
    assert len(failed) == 1
    assert failed[0].payload["event"] == "reservation.failed"
    assert failed[0].payload["reservation_id"] == str(rid)
    # The exclusive device is released back to AVAILABLE.
    update_mock.assert_called_once()
    assert update_mock.call_args[0][1] == "AVAILABLE"


async def test_timeout_backstop_leaves_fresh_dynamic_reservation():
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        dynamic_template_ids=[TEMPLATE_ID],
        updated_at=NOW - timedelta(seconds=30),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING_PROVISION
    assert await _outbox_rows("herd.reservations.failed") == []


async def test_restart_backstop_reverts_stranded_physical_reservation():
    """A physical-only PENDING_PROVISION stranded past the deadline (issue #318)
    is reverted to PENDING, not failed, so a later claim cycle re-activates it.
    No reservation.created was emitted yet (it commits with ACTIVE), so nothing
    was provisioned and there is nothing to tear down: the fix reclaims rather
    than fails. No reservation.failed is staged and no device is released."""
    update_mock = AsyncMock()
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        updated_at=NOW - timedelta(hours=2),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory(update_mock=update_mock)
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING
    assert await _outbox_rows("herd.reservations.failed") == []
    # A revert flips no inventory status; the device release loop is for FAILED
    # rows only. Re-activation on the next cycle re-flips exclusive devices.
    update_mock.assert_not_called()


async def test_restart_backstop_reclaims_and_reactivates_across_cycles():
    """End to end: a stranded physical row reverted to PENDING is picked up by
    the next cycle's claim path and driven to ACTIVE, proving the reclaim is a
    real recovery, not just a status flip."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        start_time=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(hours=2),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    with (
        p1,
        p2,
        p3,
        p4,
        patch("app.tasks.expiration._create_reservation_fork_best_effort", new=AsyncMock()),
    ):
        # First cycle: the restart backstop reverts the stranded row to PENDING.
        await _run_expiration_cycle()
        assert (await _get_reservation(rid)).status == ReservationStatus.PENDING
        # Second cycle: the claim path re-activates it exactly like any PENDING
        # reservation whose start_time has passed.
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert len(await _outbox_rows("herd.reservations.created")) == 1


async def test_restart_backstop_leaves_fresh_physical_reservation():
    """A physical-only PENDING_PROVISION within the deadline is untouched: the
    in-process activation still owns it."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        updated_at=NOW - timedelta(seconds=30),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING_PROVISION
    assert await _outbox_rows("herd.reservations.failed") == []
    assert await _outbox_rows("herd.reservations.created") == []


async def test_restart_backstop_skips_row_activated_concurrently():
    """CAS race (issue #276 discipline): if an in-process activation commits
    ACTIVE between the backstop's SELECT and its conditional write, the revert
    matches zero rows and leaves the row ACTIVE, never dragging it back to
    PENDING. Simulated by having the conditional UPDATE observe an ACTIVE row."""
    import app.tasks.expiration as exp
    from app.services import reservation_service as svc

    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        updated_at=NOW - timedelta(hours=2),
    )
    real_claim = svc._claim_provision_transition

    async def racing_claim(db, reservation_id, new_status):
        # The competing in-process activation wins the row first: commit ACTIVE,
        # then run the real CAS, which now matches zero PENDING_PROVISION rows.
        await db.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(status=ReservationStatus.ACTIVE)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await real_claim(db, reservation_id, new_status)

    p1, p2, p3, p4 = _patch_expiration_inventory()
    # The cycle calls the name bound into the expiration module at import time,
    # so patch it there, not in reservation_service.
    with (
        p1,
        p2,
        p3,
        p4,
        patch.object(exp, "_claim_provision_transition", new=racing_claim),
    ):
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert await _outbox_rows("herd.reservations.failed") == []


async def test_restart_backstop_disabled_when_timeout_zero():
    """A provision_timeout_seconds of 0 disables both backstops: a stranded
    physical row is left in PENDING_PROVISION rather than reclaimed."""
    from app.config import settings

    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        updated_at=NOW - timedelta(hours=2),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    original = settings.provision_timeout_seconds
    settings.provision_timeout_seconds = 0
    try:
        with p1, p2, p3, p4:
            await _run_expiration_cycle()
    finally:
        settings.provision_timeout_seconds = original
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING_PROVISION


# --- Same-instant race: backstop vs provision-result callback (#276) ----------


async def test_claim_provision_transition_is_single_winner():
    """The conditional UPDATE is a compare-and-swap: it transitions a
    PENDING_PROVISION row exactly once, and a second attempt matches zero rows.
    This is the primitive both the callback and the backstop share to guarantee
    at most one writer wins."""
    from app.services.reservation_service import _claim_provision_transition

    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )
    async with TestSessionLocal() as session:
        first = await _claim_provision_transition(session, rid, ReservationStatus.ACTIVE)
        await session.commit()
    async with TestSessionLocal() as session:
        second = await _claim_provision_transition(session, rid, ReservationStatus.FAILED)
        await session.commit()
    assert first is True
    assert second is False
    # The first writer's transition stands; the loser did not overwrite it.
    assert (await _get_reservation(rid)).status == ReservationStatus.ACTIVE


async def test_success_callback_loses_race_to_backstop_is_clean_noop(internal_client):
    """A success callback that reads PENDING_PROVISION but reaches its conditional
    write after the timeout backstop already committed FAILED must be a clean
    no-op: applied=False, status stays FAILED, no reservation.created event, no
    fork, and the dynamic device is never attached."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION, dynamic_template_ids=[TEMPLATE_ID]
    )

    from app.services import reservation_service as svc

    real_claim = svc._claim_provision_transition

    async def racing_claim(db, reservation_id, new_status):
        # Simulate the backstop winning the row between the callback's read and
        # its conditional write: commit FAILED first, then run the real CAS,
        # which now matches zero rows.
        await db.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(status=ReservationStatus.FAILED)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await real_claim(db, reservation_id, new_status)

    fork_mock = AsyncMock()
    dyn_device = str(uuid.uuid4())
    with (
        patch.object(svc, "_claim_provision_transition", new=racing_claim),
        patch(
            "app.services.reservation_service._create_reservation_fork_best_effort",
            new=fork_mock,
        ),
    ):
        resp = await internal_client.post(
            f"/internal/{rid}/provision-result",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            json={"succeeded": True, "device_ids": [dyn_device], "error": None},
        )
    assert resp.status_code == 200
    assert resp.json() == {"reservation_id": str(rid), "status": "FAILED", "applied": False}

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.FAILED
    # The loser attached no device and emitted no created event.
    assert [str(d) for d in res.device_ids] == [DEVICE_A]
    assert await _outbox_rows("herd.reservations.created") == []
    fork_mock.assert_not_awaited()


async def test_backstop_loses_race_to_success_callback_is_clean_noop():
    """A backstop tick that selects a stuck PENDING_PROVISION row but reaches its
    conditional write after a success callback already committed ACTIVE must be a
    clean no-op: status stays ACTIVE, no reservation.failed event, and no device
    teardown of the reservation the callback just activated."""
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        dynamic_template_ids=[TEMPLATE_ID],
        updated_at=NOW - timedelta(hours=2),
    )

    import app.tasks.expiration as expmod

    real_claim = expmod._claim_provision_transition

    async def racing_claim(db, reservation_id, new_status):
        # Simulate a success callback activating this row between the backstop's
        # SELECT and its conditional write.
        await db.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(status=ReservationStatus.ACTIVE)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await real_claim(db, reservation_id, new_status)

    update_mock = AsyncMock()
    p1, p2, p3, p4 = _patch_expiration_inventory(update_mock=update_mock)
    with p1, p2, p3, p4, patch.object(expmod, "_claim_provision_transition", new=racing_claim):
        await _run_expiration_cycle()

    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.ACTIVE
    assert await _outbox_rows("herd.reservations.failed") == []
    # No teardown: the winner's exclusive device is not released to AVAILABLE.
    update_mock.assert_not_called()


async def test_timeout_backstop_disabled_when_zero(monkeypatch):
    monkeypatch.setattr("app.tasks.expiration.settings.provision_timeout_seconds", 0)
    rid = await _insert_reservation(
        ReservationStatus.PENDING_PROVISION,
        dynamic_template_ids=[TEMPLATE_ID],
        updated_at=NOW - timedelta(hours=2),
    )
    p1, p2, p3, p4 = _patch_expiration_inventory()
    with p1, p2, p3, p4:
        await _run_expiration_cycle()
    res = await _get_reservation(rid)
    assert res.status == ReservationStatus.PENDING_PROVISION
    assert await _outbox_rows("herd.reservations.failed") == []
