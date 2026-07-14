"""
Reservations service tests.
The inventory service HTTP calls are mocked with respx.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.main import app
from app.models.reservation import Reservation, ReservationStatus
from app.routers.reservations import bearer_scheme
from fastapi.security import HTTPAuthorizationCredentials
from herd_common.enums import TopologyType
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())
DEVICE_A = str(uuid.uuid4())
DEVICE_B = str(uuid.uuid4())
DEVICE_CLOUD = str(uuid.uuid4())

NOW = datetime.now(timezone.utc)
# Immediate ("start now") window: start_time within the start-grace of now, so
# create provisions immediately (status ACTIVE) rather than scheduling PENDING.
START = NOW.isoformat()
END = (NOW + timedelta(hours=3)).isoformat()


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth():
    return {"sub": USER_ID, "username": "testuser", "role": "user"}


def override_auth_other():
    return {"sub": OTHER_USER_ID, "username": "otheruser", "role": "user"}


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
async def other_client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_other
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


MOCK_TEMPLATE_ID = str(uuid.uuid4())


def make_device_response(
    device_id: str, topology_type: str = "PHYSICAL", exclusive: bool = True
) -> dict:
    return {
        "id": device_id,
        "name": f"device-{device_id[:8]}",
        "template_id": MOCK_TEMPLATE_ID,
        "template_name": "Firewall",
        "template_icon": None,
        "topology_type": topology_type,
        "status": "AVAILABLE",
        "field_data": {},
        "exclusive": exclusive,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


async def _create_test_reservation(client, device_ids=None, **overrides):
    """Helper: creates a reservation with mocked external calls."""
    if device_ids is None:
        device_ids = [DEVICE_A]
    topo = overrides.pop("topology_type", "PHYSICAL")
    devices = [make_device_response(did, topo) for did in device_ids]
    body = {
        "device_ids": device_ids,
        "purpose": "Test lab setup",
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
            "app.services.reservation_service._validate_topology_connectivity",
            new=AsyncMock(),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post("/", json=body)
    return resp


@pytest.mark.asyncio
async def test_create_reservation(client):
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(
                return_value=[
                    make_device_response(DEVICE_A, "PHYSICAL"),
                    make_device_response(DEVICE_B, "PHYSICAL"),
                ]
            ),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A, DEVICE_B],
                "purpose": "Test lab setup",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["topology_type"] == "PHYSICAL"
    assert data["status"] == "ACTIVE"
    assert data["owner_name"] == "testuser"
    assert len(data["device_ids"]) == 2


@pytest.mark.asyncio
async def test_mixed_topology_type_rejected(client):
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(
            return_value=[
                make_device_response(DEVICE_A, "PHYSICAL"),
                make_device_response(DEVICE_CLOUD, "CLOUD"),
            ]
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A, DEVICE_CLOUD],
                "purpose": "Mixed",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert "topology" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_conflict_detection(client):
    # First reservation succeeds
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp1 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp1.status_code == 201

    # Second reservation for same device in overlapping window conflicts
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_list_reservations(client):
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    resp = await client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["total"] == 1
    assert data["skip"] == 0
    assert data["limit"] == 50


@pytest.mark.asyncio
async def test_cancel_reservation(client):
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        create_resp = await client.post(
            "/",
            json={"device_ids": [DEVICE_A], "start_time": START, "end_time": END},
        )
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        del_resp = await client.delete(f"/{reservation_id}")
    assert del_resp.status_code == 204


# --- GET single reservation ---


@pytest.mark.asyncio
async def test_get_reservation(client):
    create_resp = await _create_test_reservation(client, [DEVICE_A, DEVICE_B])
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    resp = await client.get(f"/{reservation_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == reservation_id
    assert resp.json()["purpose"] == "Test lab setup"


@pytest.mark.asyncio
async def test_get_reservation_not_found(client):
    resp = await client.get(f"/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- Internal status endpoint (apply scheduler gate) ---


INTERNAL_TOKEN = "internal-test-token"


async def _insert_reservation_row(
    *,
    status: ReservationStatus = ReservationStatus.ACTIVE,
    start_offset: timedelta = timedelta(minutes=-30),
    end_offset: timedelta = timedelta(hours=1),
    topology_id: uuid.UUID | None = None,
) -> str:
    """Insert a reservation row directly, bypassing the create router.

    Lets each test pin status + window precisely without invoking the inventory
    mocks that `_create_test_reservation` requires.
    """
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as session:
        session.add(
            Reservation(
                id=rid,
                user_id=uuid.UUID(USER_ID),
                owner_name="testuser",
                device_ids=[DEVICE_A],
                topology_id=topology_id,
                topology_type=TopologyType.PHYSICAL,
                purpose="internal-status-test",
                start_time=now + start_offset,
                end_time=now + end_offset,
                status=status,
            )
        )
        await session.commit()
    return str(rid)


@pytest.fixture
async def internal_client(monkeypatch):
    """Client without JWT overrides; uses X-Internal-Token instead."""
    monkeypatch.setattr("app.routers.reservations.settings.internal_api_token", INTERNAL_TOKEN)
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_internal_status_active(internal_client):
    rid = await _insert_reservation_row(status=ReservationStatus.ACTIVE)
    resp = await internal_client.get(
        f"/internal/{rid}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rid
    assert data["status"] == "ACTIVE"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_internal_status_cancelled_is_not_active(internal_client):
    rid = await _insert_reservation_row(status=ReservationStatus.CANCELLED)
    resp = await internal_client.get(
        f"/internal/{rid}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_internal_status_outside_window_is_not_active(internal_client):
    # Status ACTIVE but start_time still in the future.
    rid = await _insert_reservation_row(
        status=ReservationStatus.ACTIVE,
        start_offset=timedelta(hours=2),
        end_offset=timedelta(hours=4),
    )
    resp = await internal_client.get(
        f"/internal/{rid}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_internal_status_bad_token_rejected(internal_client):
    rid = await _insert_reservation_row()
    resp = await internal_client.get(
        f"/internal/{rid}", headers={"X-Internal-Token": "wrong-token"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_by_topology_returns_matching_reservation(internal_client):
    """The by-topology lookup returns reservations on that topology, unfiltered."""
    topo = uuid.uuid4()
    rid = await _insert_reservation_row(status=ReservationStatus.ACTIVE, topology_id=topo)
    resp = await internal_client.get(
        f"/internal/by-topology/{topo}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == rid
    assert items[0]["topology_id"] == str(topo)
    assert items[0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_internal_by_topology_empty_for_unrelated_topology(internal_client):
    """A topology with no reservations returns an empty list."""
    await _insert_reservation_row(topology_id=uuid.uuid4())
    other_topo = uuid.uuid4()
    resp = await internal_client.get(
        f"/internal/by-topology/{other_topo}",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_internal_by_topology_bad_token_rejected(internal_client):
    topo = uuid.uuid4()
    await _insert_reservation_row(topology_id=topo)
    resp = await internal_client.get(
        f"/internal/by-topology/{topo}", headers={"X-Internal-Token": "wrong-token"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_by_device_returns_matching_reservation(internal_client):
    """The by-device lookup returns reservations containing that device, unfiltered.

    Issue #337: inventory's config-version restore guard uses this endpoint the
    same way cabling's topology-restore guard uses /internal/by-topology.
    """
    rid = await _insert_reservation_row(status=ReservationStatus.ACTIVE)
    resp = await internal_client.get(
        f"/internal/by-device/{DEVICE_A}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == rid
    assert items[0]["device_id"] == DEVICE_A
    assert items[0]["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_internal_by_device_empty_for_unrelated_device(internal_client):
    """A device with no reservations returns an empty list."""
    await _insert_reservation_row()
    other_device = str(uuid.uuid4())
    resp = await internal_client.get(
        f"/internal/by-device/{other_device}",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_internal_by_device_bad_token_rejected(internal_client):
    await _insert_reservation_row()
    resp = await internal_client.get(
        f"/internal/by-device/{DEVICE_A}", headers={"X-Internal-Token": "wrong-token"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_status_missing_token_rejected(internal_client):
    rid = await _insert_reservation_row()
    resp = await internal_client.get(f"/internal/{rid}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_status_not_found(internal_client):
    resp = await internal_client.get(
        f"/internal/{uuid.uuid4()}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_internal_status_no_collision_with_user_get(internal_client):
    """/internal/{id} must not be shadowed by /{reservation_id} catch-all."""
    rid = await _insert_reservation_row()
    resp = await internal_client.get(
        f"/internal/{rid}", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    # If routing matched /{reservation_id}, we'd get 401/403 from JWT dep, not 200.
    assert resp.status_code == 200
    assert "is_active" in resp.json()


# --- /internal/active endpoint (Stage 3: widened ACL for AI write tools) ---


@pytest.mark.asyncio
async def test_internal_active_owner_with_device_returns_true(internal_client):
    await _insert_reservation_row(status=ReservationStatus.ACTIVE)
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 200
    assert resp.json() == {"owns_active": True}


@pytest.mark.asyncio
async def test_internal_active_non_owner_returns_false(internal_client):
    await _insert_reservation_row(status=ReservationStatus.ACTIVE)
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": OTHER_USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 200
    assert resp.json() == {"owns_active": False}


@pytest.mark.asyncio
async def test_internal_active_owner_wrong_device_returns_false(internal_client):
    await _insert_reservation_row(status=ReservationStatus.ACTIVE)
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": USER_ID, "device_id": DEVICE_B},
    )
    assert resp.status_code == 200
    assert resp.json() == {"owns_active": False}


@pytest.mark.asyncio
async def test_internal_active_outside_window_returns_false(internal_client):
    # ACTIVE status but window in the future.
    await _insert_reservation_row(
        status=ReservationStatus.ACTIVE,
        start_offset=timedelta(hours=2),
        end_offset=timedelta(hours=4),
    )
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 200
    assert resp.json() == {"owns_active": False}


@pytest.mark.asyncio
async def test_internal_active_cancelled_returns_false(internal_client):
    await _insert_reservation_row(status=ReservationStatus.CANCELLED)
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 200
    assert resp.json() == {"owns_active": False}


@pytest.mark.asyncio
async def test_internal_active_bad_token_rejected(internal_client):
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": "wrong"},
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_internal_active_missing_token_rejected(internal_client):
    resp = await internal_client.get(
        "/internal/active",
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_internal_active_no_collision_with_int_status_path(internal_client):
    """/internal/active must NOT be captured by /internal/{reservation_id}."""
    resp = await internal_client.get(
        "/internal/active",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"user_id": USER_ID, "device_id": DEVICE_A},
    )
    # If /internal/{id} captured it, FastAPI would 422 on "active" not being a UUID,
    # before even reaching the handler. We get a 200 with our own schema body.
    assert resp.status_code == 200
    assert "owns_active" in resp.json()


# --- Cancel edge cases ---


@pytest.mark.asyncio
async def test_cancel_reservation_not_found(client):
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.delete(f"/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_already_cancelled(client):
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        first = await client.delete(f"/{reservation_id}")
        assert first.status_code == 204
        second = await client.delete(f"/{reservation_id}")
        assert second.status_code == 204


# --- Release tests ---


@pytest.mark.asyncio
async def test_release_reservation(client):
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.put(f"/{reservation_id}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_release_reservation_not_found(client):
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.put(f"/{uuid.uuid4()}/release")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_release_non_active_reservation(client):
    """Cancel, then try to release; should return reservation unchanged (still CANCELLED)."""
    create_resp = await _create_test_reservation(client)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
        resp = await client.put(f"/{reservation_id}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


# --- Device not available ---


@pytest.mark.asyncio
async def test_device_not_available(client):
    """Reservation creation should fail if a device is not AVAILABLE."""
    reserved_device = make_device_response(DEVICE_A, "PHYSICAL")
    reserved_device["status"] = "RESERVED"
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[reserved_device]),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 422
    assert "not available" in resp.json()["detail"].lower()


# --- Validation tests ---


@pytest.mark.asyncio
async def test_create_reservation_empty_device_ids(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [],
            "purpose": "Test",
            "start_time": START,
            "end_time": END,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_end_before_start(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": END,
            "end_time": START,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_end_equals_start(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": START,
            "end_time": START,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_invalid_device_id(client):
    resp = await client.post(
        "/",
        json={
            "device_ids": ["not-a-uuid"],
            "purpose": "Test",
            "start_time": START,
            "end_time": END,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_reservation_rejects_past_start(client):
    """A start_time well past the grace window is rejected with 422."""
    past_start = (NOW - timedelta(hours=2)).isoformat()
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": past_start,
            "end_time": END,
        },
    )
    assert resp.status_code == 422
    assert "past" in resp.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_create_reservation_allows_start_within_grace(client):
    """A start_time a few seconds in the past (clock skew / 'start now') is
    allowed: it is within the default grace window."""
    near_start = (NOW - timedelta(seconds=5)).isoformat()
    near_end = (NOW + timedelta(hours=1)).isoformat()
    resp = await _create_test_reservation(client, start_time=near_start, end_time=near_end)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_reservation_rejects_overlong_duration(client):
    """A window longer than the max-duration cap (default 30 days) is rejected."""
    long_start = (NOW + timedelta(hours=1)).isoformat()
    long_end = (NOW + timedelta(days=31)).isoformat()
    resp = await client.post(
        "/",
        json={
            "device_ids": [DEVICE_A],
            "purpose": "Test",
            "start_time": long_start,
            "end_time": long_end,
        },
    )
    assert resp.status_code == 422
    assert "duration" in resp.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_create_reservation_allows_long_but_capped_duration(client):
    """A window at the long end but under the cap (e.g. 29 days) is allowed."""
    long_start = (NOW + timedelta(hours=1)).isoformat()
    long_end = (NOW + timedelta(days=29)).isoformat()
    resp = await _create_test_reservation(client, start_time=long_start, end_time=long_end)
    assert resp.status_code == 201


# --- Conflict edge cases ---


@pytest.mark.asyncio
async def test_adjacent_reservations_allowed(client):
    """Reservation B starts exactly when A ends; no conflict (half-open interval)."""
    a_start = START
    a_end = END
    resp_a = await _create_test_reservation(client, [DEVICE_A], start_time=a_start, end_time=a_end)
    assert resp_a.status_code == 201
    # B starts exactly at A's end
    b_start = a_end
    b_end = (NOW + timedelta(hours=5)).isoformat()
    resp_b = await _create_test_reservation(client, [DEVICE_A], start_time=b_start, end_time=b_end)
    assert resp_b.status_code == 201


@pytest.mark.asyncio
async def test_no_conflict_with_cancelled_reservation(client):
    """Cancel reservation A, then rebook same device and window; expect 201."""
    resp_a = await _create_test_reservation(client, [DEVICE_A])
    assert resp_a.status_code == 201
    reservation_id = resp_a.json()["id"]
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
    resp_b = await _create_test_reservation(client, [DEVICE_A])
    assert resp_b.status_code == 201


@pytest.mark.asyncio
async def test_conflict_then_cancel_then_rebook(client):
    """Create A, attempt overlapping B (409), cancel A, create B again (201)."""
    resp_a = await _create_test_reservation(client, [DEVICE_A])
    assert resp_a.status_code == 201
    reservation_id = resp_a.json()["id"]
    # Overlapping B should conflict
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ):
        resp_conflict = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Overlapping",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp_conflict.status_code == 409
    # Cancel A
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        await client.delete(f"/{reservation_id}")
    # Now B should succeed
    resp_b = await _create_test_reservation(client, [DEVICE_A])
    assert resp_b.status_code == 201


# --- User isolation tests ---


@pytest.mark.asyncio
async def test_user_b_cannot_list_user_a_reservations(client):
    """Other user's list should not include user A's reservations."""
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    # Switch to other user
    app.dependency_overrides[get_current_user_payload] = override_auth_other
    list_resp = await client.get("/")
    assert list_resp.status_code == 200
    assert list_resp.json()["items"] == []
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_user_b_cannot_get_user_a_reservation(client):
    """Other user cannot GET a reservation they did not create."""
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    reservation_id = resp.json()["id"]
    # Switch to other user
    app.dependency_overrides[get_current_user_payload] = override_auth_other
    get_resp = await client.get(f"/{reservation_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_user_b_cannot_cancel_user_a_reservation(client):
    """Other user cannot cancel a reservation they did not create."""
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    reservation_id = resp.json()["id"]
    # Switch to other user
    app.dependency_overrides[get_current_user_payload] = override_auth_other
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        del_resp = await client.delete(f"/{reservation_id}")
    assert del_resp.status_code == 404


@pytest.mark.asyncio
async def test_user_b_cannot_release_user_a_reservation(client):
    """Other user cannot release a reservation they did not create."""
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    reservation_id = resp.json()["id"]
    # Switch to other user
    app.dependency_overrides[get_current_user_payload] = override_auth_other
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        rel_resp = await client.put(f"/{reservation_id}/release")
    assert rel_resp.status_code == 404


@pytest.mark.asyncio
async def test_inventory_unreachable_returns_503(client):
    """When _fetch_devices raises RuntimeError, the endpoint returns 503."""
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(side_effect=RuntimeError("Connection refused")),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 503


# --- Exclusive flag tests ---


DEVICE_SHARED = str(uuid.uuid4())


@pytest.mark.asyncio
async def test_non_exclusive_device_no_conflict(client):
    """Two overlapping reservations with non-exclusive device both succeed."""
    shared_device = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp1 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "First",
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp1.status_code == 201

    # Second overlapping reservation should also succeed
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "Second",
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp2.status_code == 201


@pytest.mark.asyncio
async def test_exclusive_device_still_conflicts(client):
    """Two overlapping reservations with exclusive device still returns 409."""
    exclusive_device = make_device_response(DEVICE_A, "PHYSICAL", exclusive=True)
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[exclusive_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp1 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "First",
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp1.status_code == 201

    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[exclusive_device]),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Second",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_mixed_exclusive_non_exclusive(client):
    """Reservation with both exclusive and non-exclusive devices;
    conflict only on the exclusive device."""
    shared_device = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    exclusive_device = make_device_response(DEVICE_B, "PHYSICAL", exclusive=True)

    # First reservation with both devices
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared_device, exclusive_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp1 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED, DEVICE_B],
                "purpose": "First",
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp1.status_code == 201

    # Second reservation with only the shared device should succeed
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "Second",
                "start_time": START,
                "end_time": END,
            },
        )
        assert resp2.status_code == 201

    # Third reservation with the exclusive device should conflict
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[exclusive_device]),
    ):
        resp3 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_B],
                "purpose": "Third",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp3.status_code == 409


@pytest.mark.asyncio
async def test_create_reservation_duplicate_device_ids(client):
    """device_ids=[A, A] behavior."""
    device = make_device_response(DEVICE_A, "PHYSICAL")
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A, DEVICE_A],
                "purpose": "Dupe test",
                "start_time": START,
                "end_time": END,
            },
        )
    # Either succeeds with deduplication or fails with validation
    assert resp.status_code in (201, 422)


@pytest.mark.asyncio
async def test_create_reservation_exact_time_overlap(client):
    """Identical start/end on exclusive device returns 409."""
    resp1 = await _create_test_reservation(client, [DEVICE_A])
    assert resp1.status_code == 201
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A, "PHYSICAL")]),
    ):
        resp2 = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "Exact overlap",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_create_reservation_purpose_none(client):
    """purpose omitted accepted."""
    resp = await _create_test_reservation(client, [DEVICE_A], purpose=None)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_reservation_purpose_empty_string(client):
    """purpose="" accepted."""
    resp = await _create_test_reservation(client, [DEVICE_A], purpose="")
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_release_pending_reservation(client):
    """Release on PENDING does nothing (only works on ACTIVE)."""
    # Insert PENDING reservation directly via DB since create always sets ACTIVE
    from app.models.reservation import Reservation, ReservationStatus

    res_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        res = Reservation(
            id=res_id,
            user_id=uuid.UUID(USER_ID),
            device_ids=[DEVICE_A],
            topology_type="PHYSICAL",
            purpose="pending test",
            start_time=datetime.fromisoformat(START),
            end_time=datetime.fromisoformat(END),
            status=ReservationStatus.PENDING,
        )
        session.add(res)
        await session.commit()
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.put(f"/{res_id}/release")
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING"


@pytest.mark.asyncio
async def test_cancel_completed_reservation(client):
    """Cancel on COMPLETED is idempotent."""
    from app.models.reservation import Reservation, ReservationStatus

    res_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        res = Reservation(
            id=res_id,
            user_id=uuid.UUID(USER_ID),
            device_ids=[DEVICE_A],
            topology_type="PHYSICAL",
            purpose="completed test",
            start_time=datetime.fromisoformat(START),
            end_time=datetime.fromisoformat(END),
            status=ReservationStatus.COMPLETED,
        )
        session.add(res)
        await session.commit()
    with patch(
        "app.services.reservation_service._update_device_statuses",
        new=AsyncMock(),
    ):
        resp = await client.delete(f"/{res_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_list_reservations_empty(client):
    """No reservations returns 200 + empty paginated response."""
    resp = await client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_reservations_ordering(client):
    """Newest first (needs sleep(1.1) for SQLite timestamp resolution)."""
    import asyncio

    resp1 = await _create_test_reservation(client, [DEVICE_A])
    assert resp1.status_code == 201
    await asyncio.sleep(1.1)
    resp2 = await _create_test_reservation(
        client,
        [DEVICE_B],
        start_time=(NOW + timedelta(hours=5)).isoformat(),
        end_time=(NOW + timedelta(hours=7)).isoformat(),
    )
    assert resp2.status_code == 201
    second_id = resp2.json()["id"]
    list_resp = await client.get("/")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 2
    assert list_resp.json()["total"] == 2
    # Most recently created should be first
    assert items[0]["id"] == second_id


@pytest.mark.asyncio
async def test_create_reservation_single_device(client):
    """Single device_id works."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    assert len(resp.json()["device_ids"]) == 1


@pytest.mark.asyncio
async def test_reservation_response_has_timestamps(client):
    """created_at present in response."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    data = resp.json()
    assert "created_at" in data
    assert data["created_at"] is not None


@pytest.mark.asyncio
async def test_create_reservation_with_topology_id(client):
    """topology_id is stored and returned when provided."""
    topo_id = str(uuid.uuid4())
    resp = await _create_test_reservation(client, [DEVICE_A], topology_id=topo_id)
    assert resp.status_code == 201
    data = resp.json()
    assert data["topology_id"] == topo_id


@pytest.mark.asyncio
async def test_create_reservation_without_topology_id(client):
    """topology_id defaults to null when omitted."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    assert resp.json()["topology_id"] is None


@pytest.mark.asyncio
async def test_topology_id_returned_on_get(client):
    """GET single reservation includes topology_id."""
    topo_id = str(uuid.uuid4())
    create_resp = await _create_test_reservation(client, [DEVICE_A], topology_id=topo_id)
    assert create_resp.status_code == 201
    reservation_id = create_resp.json()["id"]
    resp = await client.get(f"/{reservation_id}")
    assert resp.status_code == 200
    assert resp.json()["topology_id"] == topo_id


@pytest.mark.asyncio
async def test_create_reservation_blocked_by_invalid_topology(client):
    """A topology with unreachable edges is rejected at 422."""
    topo_id = str(uuid.uuid4())
    devices = [make_device_response(DEVICE_A, "PHYSICAL")]
    body = {
        "device_ids": [DEVICE_A],
        "topology_id": topo_id,
        "purpose": "Cross-fabric",
        "start_time": START,
        "end_time": END,
    }
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=AsyncMock(side_effect=ValueError("Topology has unreachable edges")),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post("/", json=body)
    assert resp.status_code == 422
    assert "unreachable" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_reservation_validation_called_when_topology_present(client):
    """The cabling validation is invoked exactly once with the topology id."""
    topo_id = str(uuid.uuid4())
    devices = [make_device_response(DEVICE_A, "PHYSICAL")]
    validate_mock = AsyncMock()
    body = {
        "device_ids": [DEVICE_A],
        "topology_id": topo_id,
        "start_time": START,
        "end_time": END,
    }
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=validate_mock,
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post("/", json=body)
    assert resp.status_code == 201
    validate_mock.assert_awaited_once()
    args, _ = validate_mock.call_args
    assert str(args[0]) == topo_id


@pytest.mark.asyncio
async def test_create_reservation_validation_skipped_without_topology(client):
    """Reservations without a topology id never call the cabling validator."""
    devices = [make_device_response(DEVICE_A, "PHYSICAL")]
    validate_mock = AsyncMock()
    body = {
        "device_ids": [DEVICE_A],
        "start_time": START,
        "end_time": END,
    }
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=validate_mock,
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post("/", json=body)
    assert resp.status_code == 201
    validate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_name_in_list(client):
    """List reservations includes owner_name."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    list_resp = await client.get("/")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["owner_name"] == "testuser"


@pytest.mark.asyncio
async def test_non_exclusive_device_status_not_changed(client):
    """Verify _update_device_statuses only called with exclusive device IDs."""
    shared_device = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    exclusive_device = make_device_response(DEVICE_A, "PHYSICAL", exclusive=True)
    mock_update = AsyncMock()

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared_device, exclusive_device]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED, DEVICE_A],
                "purpose": "Test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    # _update_device_statuses should have been called with only the exclusive device
    mock_update.assert_called_once()
    called_ids = mock_update.call_args[0][0]
    called_id_strs = [str(d) for d in called_ids]
    assert DEVICE_A in called_id_strs
    assert DEVICE_SHARED not in called_id_strs


# --- Calendar endpoint tests ---


@pytest.mark.asyncio
async def test_calendar_returns_cross_user_reservations(client, other_client):
    """Calendar shows reservations from multiple users."""
    resp1 = await _create_test_reservation(client, [DEVICE_A])
    assert resp1.status_code == 201

    resp2 = await _create_test_reservation(
        other_client,
        [DEVICE_B],
        start_time=(NOW + timedelta(hours=2)).isoformat(),
        end_time=(NOW + timedelta(hours=4)).isoformat(),
    )
    assert resp2.status_code == 201

    range_start = NOW.isoformat()
    range_end = (NOW + timedelta(hours=5)).isoformat()
    cal_resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert cal_resp.status_code == 200
    items = cal_resp.json()
    assert len(items) == 2


@pytest.mark.asyncio
async def test_calendar_date_range_filter(client):
    """Only overlapping reservations returned."""
    # Reservation from hour 1 to 3
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201

    # Query range that does not overlap (hour 5 to 7)
    range_start = (NOW + timedelta(hours=5)).isoformat()
    range_end = (NOW + timedelta(hours=7)).isoformat()
    cal_resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert cal_resp.status_code == 200
    assert cal_resp.json() == []


@pytest.mark.asyncio
async def test_calendar_status_filter(client):
    """Filter by specific status."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    # Reservation is ACTIVE; filter for PENDING only
    range_start = NOW.isoformat()
    range_end = (NOW + timedelta(hours=5)).isoformat()
    cal_resp = await client.get(
        "/calendar",
        params={"range_start": range_start, "range_end": range_end, "status": "PENDING"},
    )
    assert cal_resp.status_code == 200
    assert cal_resp.json() == []

    # Filter for ACTIVE
    cal_resp2 = await client.get(
        "/calendar",
        params={"range_start": range_start, "range_end": range_end, "status": "ACTIVE"},
    )
    assert cal_resp2.status_code == 200
    assert len(cal_resp2.json()) == 1


@pytest.mark.asyncio
async def test_calendar_device_filter(client):
    """Filter by specific device_id."""
    resp_a = await _create_test_reservation(client, [DEVICE_A])
    assert resp_a.status_code == 201
    resp_b = await _create_test_reservation(
        client,
        [DEVICE_B],
        start_time=(NOW + timedelta(hours=5)).isoformat(),
        end_time=(NOW + timedelta(hours=7)).isoformat(),
    )
    assert resp_b.status_code == 201

    range_start = NOW.isoformat()
    range_end = (NOW + timedelta(hours=10)).isoformat()
    cal_resp = await client.get(
        "/calendar",
        params={"range_start": range_start, "range_end": range_end, "device_id": DEVICE_A},
    )
    assert cal_resp.status_code == 200
    items = cal_resp.json()
    assert len(items) == 1
    assert DEVICE_A in items[0]["device_ids"]


@pytest.mark.asyncio
async def test_calendar_requires_auth():
    """Unauthenticated request returns 401."""
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides.pop(get_current_user_payload, None)
    app.dependency_overrides.pop(bearer_scheme, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/calendar",
            params={
                "range_start": NOW.isoformat(),
                "range_end": (NOW + timedelta(hours=5)).isoformat(),
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_calendar_requires_range_params(client):
    """Missing range params returns 422."""
    resp = await client.get("/calendar")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_calendar_span_over_max_rejected(client):
    """A window wider than calendar_max_span_days is rejected with 422
    (issue #315), instead of silently loading an unbounded result set."""
    from app.config import settings

    range_start = NOW.isoformat()
    range_end = (NOW + timedelta(days=settings.calendar_max_span_days + 1)).isoformat()
    resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert resp.status_code == 422
    assert str(settings.calendar_max_span_days) in resp.json()["detail"]


@pytest.mark.asyncio
async def test_calendar_span_at_max_succeeds(client):
    """A window just under (and at) calendar_max_span_days still succeeds."""
    from app.config import settings

    range_start = NOW.isoformat()
    range_end = (
        NOW + timedelta(days=settings.calendar_max_span_days) - timedelta(seconds=1)
    ).isoformat()
    resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_calendar_empty_range(client):
    """No reservations in range returns []."""
    range_start = (NOW + timedelta(days=30)).isoformat()
    range_end = (NOW + timedelta(days=31)).isoformat()
    cal_resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert cal_resp.status_code == 200
    assert cal_resp.json() == []


@pytest.mark.asyncio
async def test_calendar_boundary_exclusion(client):
    """Reservation ending exactly at range_start is excluded (half-open)."""
    # Reservation: hour 1 to hour 3
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201

    # Query starting exactly at reservation end_time
    range_start = (NOW + timedelta(hours=3)).isoformat()
    range_end = (NOW + timedelta(hours=5)).isoformat()
    cal_resp = await client.get(
        "/calendar", params={"range_start": range_start, "range_end": range_end}
    )
    assert cal_resp.status_code == 200
    assert cal_resp.json() == []


# --- Status update / exclusive flag deeper tests ---


@pytest.mark.asyncio
async def test_create_reservation_non_exclusive_skips_status_update(client):
    """Non-exclusive devices should not trigger _update_device_statuses."""
    shared = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "Shared only",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_create_reservation_mixed_exclusive_status_update(client):
    """Only exclusive device IDs are passed to _update_device_statuses."""
    shared = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    exclusive = make_device_response(DEVICE_B, "PHYSICAL", exclusive=True)
    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared, exclusive]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED, DEVICE_B],
                "purpose": "Mixed",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    mock_update.assert_called_once()
    called_ids = [str(d) for d in mock_update.call_args[0][0]]
    assert DEVICE_B in called_ids
    assert DEVICE_SHARED not in called_ids


@pytest.mark.asyncio
async def test_cancel_non_exclusive_skips_status_update(client):
    """Cancel with non-exclusive device skips status update for that device."""
    shared = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "Cancel test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    res_id = resp.json()["id"]
    mock_update.reset_mock()

    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[shared]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        cancel_resp = await client.delete(f"/{res_id}")
    assert cancel_resp.status_code == 204
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_fetch_failure_falls_back_to_exclusive(client):
    """When best-effort fetch returns per-device failures during cancel, those
    devices are treated as exclusive and their status update still goes out."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[RuntimeError("network error")]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        cancel_resp = await client.delete(f"/{res_id}")
    assert cancel_resp.status_code == 204
    mock_update.assert_called_once()
    called_ids = [str(d) for d in mock_update.call_args[0][0]]
    assert DEVICE_A in called_ids


@pytest.mark.asyncio
async def test_release_non_exclusive_skips_status_update(client):
    """Release with non-exclusive device skips status update for that device."""
    shared = make_device_response(DEVICE_SHARED, "PHYSICAL", exclusive=False)
    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[shared]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        resp = await client.post(
            "/",
            json={
                "device_ids": [DEVICE_SHARED],
                "purpose": "Release test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201
    res_id = resp.json()["id"]
    mock_update.reset_mock()

    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[shared]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        release_resp = await client.put(f"/{res_id}/release")
    assert release_resp.status_code == 200
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_release_fetch_failure_falls_back_to_exclusive(client):
    """When best-effort fetch returns per-device failures during release, those
    devices are treated as exclusive and their status update still goes out."""
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    mock_update = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[RuntimeError("network error")]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update,
        ),
    ):
        release_resp = await client.put(f"/{res_id}/release")
    assert release_resp.status_code == 200
    mock_update.assert_called_once()
    called_ids = [str(d) for d in mock_update.call_args[0][0]]
    assert DEVICE_A in called_ids


@pytest.mark.asyncio
async def test_calendar_multiple_status_filter(client):
    """Calendar with multiple status params returns matching reservations."""
    from app.models.reservation import Reservation, ReservationStatus

    # Create ACTIVE reservation via API
    resp = await _create_test_reservation(client, [DEVICE_A])
    assert resp.status_code == 201

    # Insert PENDING reservation directly
    pending_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        res = Reservation(
            id=pending_id,
            user_id=uuid.UUID(USER_ID),
            device_ids=[DEVICE_B],
            topology_type="PHYSICAL",
            purpose="pending",
            start_time=datetime.fromisoformat(START),
            end_time=datetime.fromisoformat(END),
            status=ReservationStatus.PENDING,
        )
        session.add(res)
        await session.commit()

    range_start = NOW.isoformat()
    range_end = (NOW + timedelta(hours=5)).isoformat()
    cal_resp = await client.get(
        "/calendar",
        params={
            "range_start": range_start,
            "range_end": range_end,
            "status": ["ACTIVE", "PENDING"],
        },
    )
    assert cal_resp.status_code == 200
    items = cal_resp.json()
    statuses = {item["status"] for item in items}
    assert "ACTIVE" in statuses
    assert "PENDING" in statuses


# --- PATCH (update reservation) tests ---


@pytest.mark.asyncio
async def test_update_reservation_extend_end_time(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_end = (NOW + timedelta(hours=5)).isoformat()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"end_time": new_end})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["end_time"] is not None


@pytest.mark.asyncio
async def test_update_reservation_shorten_end_time(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_end = (NOW + timedelta(hours=2)).isoformat()
    patch_resp = await client.patch(f"/{res_id}", json={"end_time": new_end})
    assert patch_resp.status_code == 200


@pytest.mark.asyncio
async def test_update_reservation_purpose(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    patch_resp = await client.patch(f"/{res_id}", json={"purpose": "Updated purpose"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["purpose"] == "Updated purpose"


@pytest.mark.asyncio
async def test_update_reservation_end_before_start_rejected(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    bad_end = (NOW - timedelta(hours=1)).isoformat()
    patch_resp = await client.patch(f"/{res_id}", json={"end_time": bad_end})
    assert patch_resp.status_code == 400


@pytest.mark.asyncio
async def test_update_completed_reservation_rejected(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    # Release it first (makes it COMPLETED)
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        await client.put(f"/{res_id}/release")

    patch_resp = await client.patch(f"/{res_id}", json={"purpose": "Nope"})
    assert patch_resp.status_code == 400


@pytest.mark.asyncio
async def test_update_reservation_not_found(client):
    fake_id = str(uuid.uuid4())
    patch_resp = await client.patch(f"/{fake_id}", json={"purpose": "x"})
    assert patch_resp.status_code == 404


@pytest.mark.asyncio
async def test_update_reservation_conflict_on_extension(client):
    # Create reservation A
    resp_a = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp_a.status_code == 201
    res_a_id = resp_a.json()["id"]

    # Create reservation B for same device in a later window
    later_start = (NOW + timedelta(hours=4)).isoformat()
    later_end = (NOW + timedelta(hours=6)).isoformat()
    resp_b = await _create_test_reservation(
        client,
        device_ids=[DEVICE_A],
        start_time=later_start,
        end_time=later_end,
    )
    assert resp_b.status_code == 201

    # Try to extend reservation A past reservation B start
    conflict_end = (NOW + timedelta(hours=5)).isoformat()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
    ):
        patch_resp = await client.patch(f"/{res_a_id}", json={"end_time": conflict_end})
    assert patch_resp.status_code == 409


# --- PATCH device_ids tests ---

DEVICE_C = str(uuid.uuid4())


@pytest.mark.asyncio
async def test_update_reservation_add_device(client):
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_ids = [DEVICE_A, DEVICE_B]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(d) for d in new_ids]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": new_ids})
    assert patch_resp.status_code == 200
    assert len(patch_resp.json()["device_ids"]) == 2


@pytest.mark.asyncio
async def test_update_reservation_remove_device(client):
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A, DEVICE_B])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_ids = [DEVICE_A]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": new_ids})
    assert patch_resp.status_code == 200
    assert len(patch_resp.json()["device_ids"]) == 1


@pytest.mark.asyncio
async def test_update_reservation_device_change_breaks_topology_rejected(client):
    """Changing the device set on a topology-bound reservation re-validates
    connectivity; an edit that strands an edge is rejected at 422."""
    topo_id = str(uuid.uuid4())
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A], topology_id=topo_id)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_ids = [DEVICE_A, DEVICE_B]
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(d) for d in new_ids]),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=AsyncMock(side_effect=ValueError("Topology has unreachable edges")),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": new_ids})
    # The PATCH route maps a ValueError to 400 (the POST/create route uses 422).
    assert patch_resp.status_code == 400
    assert "unreachable" in patch_resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_reservation_device_change_valid_topology_succeeds(client):
    """A device change that keeps the topology connected passes re-validation
    and the validator is invoked once with the reservation's topology id."""
    topo_id = str(uuid.uuid4())
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A], topology_id=topo_id)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_ids = [DEVICE_A, DEVICE_B]
    validate_mock = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(d) for d in new_ids]),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=validate_mock,
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": new_ids})
    assert patch_resp.status_code == 200
    assert len(patch_resp.json()["device_ids"]) == 2
    validate_mock.assert_awaited_once_with(uuid.UUID(topo_id))


@pytest.mark.asyncio
async def test_update_reservation_device_change_no_topology_skips_validation(client):
    """A reservation without a topology does not re-validate connectivity on
    a device change (mirrors the create-path behavior)."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]
    assert resp.json()["topology_id"] is None

    new_ids = [DEVICE_A, DEVICE_B]
    validate_mock = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(d) for d in new_ids]),
        ),
        patch(
            "app.services.reservation_service._validate_topology_connectivity",
            new=validate_mock,
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": new_ids})
    assert patch_resp.status_code == 200
    validate_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_reservation_empty_device_ids_rejected(client):
    resp = await _create_test_reservation(client)
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    patch_resp = await client.patch(f"/{res_id}", json={"device_ids": []})
    assert patch_resp.status_code == 422


@pytest.mark.asyncio
async def test_update_reservation_add_unavailable_device_rejected(client):
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    unavailable = make_device_response(DEVICE_B)
    unavailable["status"] = "RESERVED"

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A), unavailable]),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_B]})
    assert patch_resp.status_code == 400
    assert "not available" in patch_resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_reservation_topology_mismatch_rejected(client):
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(
                return_value=[
                    make_device_response(DEVICE_A, "PHYSICAL"),
                    make_device_response(DEVICE_CLOUD, "CLOUD"),
                ]
            ),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_CLOUD]})
    assert patch_resp.status_code == 400
    assert "topology type" in patch_resp.json()["detail"].lower()


# --- Health endpoint ---


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "reservations"


# --- Visibility / ACL tests ---


@pytest.fixture
async def non_admin_client():
    """Client with non-admin role for ACL tests."""
    non_admin_payload = {
        "sub": USER_ID,
        "username": "testuser",
        "role": "user",
    }
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: non_admin_payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client():
    """Client with admin role (bypasses visibility checks)."""
    admin_payload = {
        "sub": USER_ID,
        "username": "adminuser",
        "role": "admin",
    }
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    app.dependency_overrides[require_admin] = lambda: admin_payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_reservation_non_admin_invisible_device_rejected(non_admin_client):
    """Non-admin user trying to reserve an invisible device gets 403."""
    visible_set = {DEVICE_A}  # only DEVICE_A is visible
    with (
        patch(
            "app.routers.reservations._fetch_visible_device_ids",
            new=AsyncMock(return_value=visible_set),
        ),
    ):
        resp = await non_admin_client.post(
            "/",
            json={
                "device_ids": [DEVICE_B],
                "purpose": "test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 403
    assert "access" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_reservation_non_admin_visible_device_succeeds(non_admin_client):
    """Non-admin user can reserve a visible device."""
    visible_set = {DEVICE_A}
    with (
        patch(
            "app.routers.reservations._fetch_visible_device_ids",
            new=AsyncMock(return_value=visible_set),
        ),
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await non_admin_client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_reservation_admin_skips_visibility(admin_client):
    """Admin user skips visibility checks entirely."""
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await admin_client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_reservation_visibility_fetch_failure_allows(non_admin_client):
    """If _fetch_visible_device_ids returns None (network error), creation proceeds."""
    with (
        patch(
            "app.routers.reservations._fetch_visible_device_ids",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        resp = await non_admin_client.post(
            "/",
            json={
                "device_ids": [DEVICE_A],
                "purpose": "test",
                "start_time": START,
                "end_time": END,
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_update_reservation_non_admin_invisible_device_rejected(non_admin_client):
    """Non-admin user trying to add invisible device to reservation gets 403."""
    # First create as admin to have a reservation
    admin_payload = {"sub": USER_ID, "username": "adminuser", "role": "admin"}
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    resp = await _create_test_reservation(non_admin_client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    # Switch back to non-admin
    non_admin_payload = {"sub": USER_ID, "username": "testuser", "role": "user"}
    app.dependency_overrides[get_current_user_payload] = lambda: non_admin_payload

    visible_set = {DEVICE_A}  # DEVICE_B not visible
    with patch(
        "app.routers.reservations._fetch_visible_device_ids",
        new=AsyncMock(return_value=visible_set),
    ):
        patch_resp = await non_admin_client.patch(
            f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_B]}
        )
    assert patch_resp.status_code == 403


@pytest.mark.asyncio
async def test_update_reservation_inventory_unreachable_on_extension(client):
    """When inventory is unreachable during extension conflict check,
    falls back to all exclusive."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    new_end = (NOW + timedelta(hours=5)).isoformat()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=Exception("Connection refused")),
        ),
    ):
        # Should still succeed (no conflict, falls back to treating all as exclusive)
        patch_resp = await client.patch(f"/{res_id}", json={"end_time": new_end})
    assert patch_resp.status_code == 200


@pytest.mark.asyncio
async def test_update_reservation_add_non_exclusive_device(client):
    """Adding a non-exclusive device should skip availability check for that device."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    non_excl = make_device_response(DEVICE_B)
    non_excl["exclusive"] = False
    non_excl["status"] = "RESERVED"  # non-exclusive can be RESERVED

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A), non_excl]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_B]})
    assert patch_resp.status_code == 200
    assert len(patch_resp.json()["device_ids"]) == 2


@pytest.mark.asyncio
async def test_update_reservation_inventory_unreachable_on_device_change(client):
    """When inventory is unreachable during device_ids change, returns 503."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=RuntimeError("Connection refused")),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_B]})
    assert patch_resp.status_code == 503


@pytest.mark.asyncio
async def test_update_reservation_device_not_found_on_change(client):
    """When a device in the new set is not found, returns 400."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=ValueError("Device not found")),
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A, DEVICE_B]})
    assert patch_resp.status_code == 400


@pytest.mark.asyncio
async def test_update_reservation_remove_device_releases_exclusive(client):
    """Removing an exclusive device should call _update_device_statuses with AVAILABLE."""
    resp = await _create_test_reservation(client, device_ids=[DEVICE_A, DEVICE_B])
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    mock_update_statuses = AsyncMock()
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=mock_update_statuses,
        ),
    ):
        patch_resp = await client.patch(f"/{res_id}", json={"device_ids": [DEVICE_A]})
    assert patch_resp.status_code == 200
    # _update_device_statuses called for removed device with AVAILABLE
    assert mock_update_statuses.call_count >= 1
    calls = mock_update_statuses.call_args_list
    # Find the AVAILABLE call (for released device)
    available_calls = [c for c in calls if c[0][1] == "AVAILABLE"]
    assert len(available_calls) >= 1


@pytest.mark.asyncio
async def test_calendar_non_admin_visibility_filtering(non_admin_client):
    """Non-admin calendar query filters by visible devices."""
    # Create reservation as admin first
    admin_payload = {"sub": USER_ID, "username": "admin", "role": "admin"}
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    resp = await _create_test_reservation(non_admin_client, device_ids=[DEVICE_A])
    assert resp.status_code == 201

    # Switch to non-admin
    non_admin_payload = {"sub": USER_ID, "username": "testuser", "role": "user"}
    app.dependency_overrides[get_current_user_payload] = lambda: non_admin_payload

    range_start = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    range_end = (NOW + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")

    # Visible devices include DEVICE_A, so reservation is visible
    with patch(
        "app.routers.reservations._fetch_visible_device_ids",
        new=AsyncMock(return_value={DEVICE_A}),
    ):
        resp = await non_admin_client.get(
            f"/calendar?range_start={range_start}&range_end={range_end}"
        )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    # Visible devices do NOT include DEVICE_A, so reservation is filtered out
    with patch(
        "app.routers.reservations._fetch_visible_device_ids",
        new=AsyncMock(return_value={str(uuid.uuid4())}),
    ):
        resp = await non_admin_client.get(
            f"/calendar?range_start={range_start}&range_end={range_end}"
        )
    assert resp.status_code == 200
    assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_update_reservation_conflict_on_added_device(client):
    """Adding a device that has a conflicting reservation returns 409."""
    # Create reservation A with DEVICE_A
    resp_a = await _create_test_reservation(client, device_ids=[DEVICE_A])
    assert resp_a.status_code == 201

    # Create reservation B with DEVICE_B in the same window
    resp_b = await _create_test_reservation(client, device_ids=[DEVICE_B])
    assert resp_b.status_code == 201
    res_b_id = resp_b.json()["id"]

    # Try to add DEVICE_A to reservation B - should conflict with reservation A
    with (
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(
                return_value=[make_device_response(DEVICE_A), make_device_response(DEVICE_B)]
            ),
        ),
        patch(
            "app.services.reservation_service._update_device_statuses",
            new=AsyncMock(),
        ),
    ):
        patch_resp = await client.patch(f"/{res_b_id}", json={"device_ids": [DEVICE_A, DEVICE_B]})
    assert patch_resp.status_code == 409


async def _seed_reservation(
    user_id: str,
    owner_name: str,
    device_ids: list[str],
    start: datetime,
    end: datetime,
    res_status: ReservationStatus = ReservationStatus.COMPLETED,
) -> None:
    async with TestSessionLocal() as session:
        session.add(
            Reservation(
                id=uuid.uuid4(),
                user_id=uuid.UUID(user_id),
                owner_name=owner_name,
                device_ids=list(device_ids),
                topology_id=None,
                topology_type=TopologyType.PHYSICAL,
                start_time=start,
                end_time=end,
                status=res_status,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_utilization_report_admin(admin_client):
    window_start = NOW - timedelta(days=30)
    window_end = NOW

    # User A: 2 completed reservations (DEVICE_A only) totaling 5h
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=5, hours=3),
        NOW - timedelta(days=5),
    )
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=2, hours=2),
        NOW - timedelta(days=2),
    )
    # User B: 1 completed reservation on DEVICE_A + DEVICE_B, 4h
    await _seed_reservation(
        OTHER_USER_ID,
        "bob",
        [DEVICE_A, DEVICE_B],
        NOW - timedelta(days=1, hours=4),
        NOW - timedelta(days=1),
    )
    # Cancelled reservation in window: must be excluded with default filter
    await _seed_reservation(
        OTHER_USER_ID,
        "bob",
        [DEVICE_B],
        NOW - timedelta(days=3, hours=10),
        NOW - timedelta(days=3),
        res_status=ReservationStatus.CANCELLED,
    )
    # Completed but fully outside the window: must be excluded
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=60, hours=5),
        NOW - timedelta(days=60),
    )
    # Spans window boundary: 4h of its 8h total fall inside
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_CLOUD],
        window_start - timedelta(hours=4),
        window_start + timedelta(hours=4),
    )

    resp = await admin_client.get(
        "/reports/utilization",
        params={"start": window_start.isoformat(), "end": window_end.isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Alice: 3+2 (full) + 4 (clamped) = 9h. Bob: 4h. Total: 13h.
    assert data["total_hours"] == pytest.approx(13.0, abs=0.01)
    assert data["total_reservations"] == 4

    by_user = {b["owner_name"]: b for b in data["by_user"]}
    assert by_user["alice"]["hours"] == pytest.approx(9.0, abs=0.01)
    assert by_user["alice"]["reservation_count"] == 3
    assert by_user["bob"]["hours"] == pytest.approx(4.0, abs=0.01)
    assert by_user["bob"]["reservation_count"] == 1
    # Sorted by hours desc: alice (9) before bob (4)
    assert data["by_user"][0]["owner_name"] == "alice"

    by_device = {b["device_id"]: b for b in data["by_device"]}
    # DEVICE_A: alice 3+2 + bob 4 = 9h
    assert by_device[DEVICE_A]["hours"] == pytest.approx(9.0, abs=0.01)
    assert by_device[DEVICE_A]["reservation_count"] == 3
    # DEVICE_B: bob 4h
    assert by_device[DEVICE_B]["hours"] == pytest.approx(4.0, abs=0.01)
    # DEVICE_CLOUD: alice clamped 4h
    assert by_device[DEVICE_CLOUD]["hours"] == pytest.approx(4.0, abs=0.01)


@pytest.mark.asyncio
async def test_utilization_report_non_admin_forbidden(client):
    window_start = NOW - timedelta(days=30)
    window_end = NOW
    resp = await client.get(
        "/reports/utilization",
        params={"start": window_start.isoformat(), "end": window_end.isoformat()},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_utilization_report_includes_topology_and_run_count(admin_client):
    window_start = NOW - timedelta(days=7)
    window_end = NOW
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=1, hours=2),
        NOW - timedelta(days=1),
    )
    with patch(
        "app.routers.reservations.fetch_execution_run_count",
        new=AsyncMock(return_value=11),
    ):
        resp = await admin_client.get(
            "/reports/utilization",
            params={"start": window_start.isoformat(), "end": window_end.isoformat()},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_run_count"] == 11
    assert any(b["topology_type"] == "PHYSICAL" for b in data["by_topology_type"])


@pytest.mark.asyncio
async def test_utilization_report_rejects_inverted_window(admin_client):
    now = NOW
    resp = await admin_client.get(
        "/reports/utilization",
        params={"start": now.isoformat(), "end": (now - timedelta(hours=1)).isoformat()},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_utilization_report_csv_user_section(admin_client):
    window_start = NOW - timedelta(days=7)
    window_end = NOW
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=1, hours=2),
        NOW - timedelta(days=1),
    )
    resp = await admin_client.get(
        "/reports/utilization.csv",
        params={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "section": "user",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert body.splitlines()[0] == "user_id,owner_name,hours,reservation_count"
    assert "alice" in body


@pytest.mark.asyncio
async def test_utilization_report_csv_device_section(admin_client):
    window_start = NOW - timedelta(days=7)
    window_end = NOW
    await _seed_reservation(
        USER_ID,
        "alice",
        [DEVICE_A],
        NOW - timedelta(days=1, hours=2),
        NOW - timedelta(days=1),
    )
    resp = await admin_client.get(
        "/reports/utilization.csv",
        params={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "section": "device",
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert body.splitlines()[0] == "device_id,hours,reservation_count"
    assert DEVICE_A in body


@pytest.mark.asyncio
async def test_utilization_report_csv_rejects_unknown_section(admin_client):
    window_start = NOW - timedelta(days=7)
    window_end = NOW
    resp = await admin_client.get(
        "/reports/utilization.csv",
        params={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "section": "template",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_utilization_report_csv_non_admin_forbidden(client):
    window_start = NOW - timedelta(days=7)
    window_end = NOW
    resp = await client.get(
        "/reports/utilization.csv",
        params={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "section": "user",
        },
    )
    assert resp.status_code == 403


# --- /internal/active-users (ROADMAP #13 iter 2 alerting recipient resolver) ---


async def _insert_reservation_for_user(
    *,
    user_id: str,
    device_ids: list[str],
    status: ReservationStatus = ReservationStatus.ACTIVE,
    start_offset: timedelta = timedelta(minutes=-30),
    end_offset: timedelta = timedelta(hours=1),
) -> str:
    """Insert a reservation row tied to a specific user and device list."""
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with TestSessionLocal() as session:
        session.add(
            Reservation(
                id=rid,
                user_id=uuid.UUID(user_id),
                owner_name="seeded",
                device_ids=list(device_ids),
                topology_id=None,
                topology_type=TopologyType.PHYSICAL,
                purpose="active-users-test",
                start_time=now + start_offset,
                end_time=now + end_offset,
                status=status,
            )
        )
        await session.commit()
    return str(rid)


@pytest.mark.asyncio
async def test_active_users_returns_holders(internal_client):
    """Two active reservations on the same device, by different users."""
    await _insert_reservation_for_user(user_id=USER_ID, device_ids=[DEVICE_A])
    await _insert_reservation_for_user(user_id=OTHER_USER_ID, device_ids=[DEVICE_A])

    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    ids = {str(uid) for uid in resp.json()}
    assert ids == {USER_ID, OTHER_USER_ID}


@pytest.mark.asyncio
async def test_active_users_dedupes_same_user_multiple_reservations(internal_client):
    """A user with two active reservations on the same device appears once."""
    await _insert_reservation_for_user(user_id=USER_ID, device_ids=[DEVICE_A])
    await _insert_reservation_for_user(user_id=USER_ID, device_ids=[DEVICE_A, DEVICE_B])

    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    ids = [str(uid) for uid in resp.json()]
    assert ids == [USER_ID]


@pytest.mark.asyncio
async def test_active_users_filters_by_time_window(internal_client):
    """A reservation whose window has not started yet is not returned."""
    await _insert_reservation_for_user(
        user_id=USER_ID,
        device_ids=[DEVICE_A],
        start_offset=timedelta(hours=1),
        end_offset=timedelta(hours=2),
    )
    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_active_users_filters_by_status(internal_client):
    """Only ACTIVE reservations count; PENDING/COMPLETED/CANCELLED are skipped."""
    await _insert_reservation_for_user(
        user_id=USER_ID,
        device_ids=[DEVICE_A],
        status=ReservationStatus.COMPLETED,
    )
    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_active_users_filters_by_device(internal_client):
    """Reservations on a different device do not bleed in."""
    await _insert_reservation_for_user(user_id=USER_ID, device_ids=[DEVICE_B])
    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_active_users_requires_valid_internal_token(internal_client):
    await _insert_reservation_for_user(user_id=USER_ID, device_ids=[DEVICE_A])
    resp = await internal_client.get(
        "/internal/active-users",
        params={"device_id": DEVICE_A},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_fetch_visible_device_ids_forwards_jwt_not_internal_token():
    """Regression: the inventory /device-groups/visible-devices endpoint is
    JWT-guarded, so this call must forward the caller's Bearer token. Previously
    it sent only X-Internal-Token, got 401, swallowed it, and returned None,
    silently disabling non-admin device-visibility filtering."""
    from app.routers.reservations import _fetch_visible_device_ids

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"device_ids": [DEVICE_A]}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.routers.reservations.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_visible_device_ids(uuid.uuid4(), "jwt-token-123")

    mock_client.get.assert_called_once()
    headers = mock_client.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer jwt-token-123"
    assert "X-Internal-Token" not in headers
    assert result == {DEVICE_A}
