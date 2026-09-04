"""Lab purpose classification: taxonomy config, create-time validation, the
PATCH ownership matrix, and the null-clears-all-three semantics (issue #646
phase 1).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from httpx import ASGITransport, AsyncClient

from tests._harness import TestSessionLocal, override_bearer, override_get_db

OWNER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())
DEVICE_A = str(uuid.uuid4())
MOCK_TEMPLATE_ID = str(uuid.uuid4())

NOW = datetime.now(timezone.utc)
START = NOW.isoformat()
END = (NOW + timedelta(hours=3)).isoformat()


def _client_as(sub: str, role: str = "user") -> AsyncClient:
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def make_device_response(device_id: str, topology_type: str = "PHYSICAL") -> dict:
    return {
        "id": device_id,
        "name": f"device-{device_id[:8]}",
        "template_id": MOCK_TEMPLATE_ID,
        "template_name": "Firewall",
        "template_icon": None,
        "topology_type": topology_type,
        "status": "AVAILABLE",
        "field_data": {},
        "exclusive": True,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


async def _insert_reservation(
    *,
    owner: str = OWNER_ID,
    status: ReservationStatus = ReservationStatus.ACTIVE,
    purpose_category: str | None = None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(owner),
            owner_name="owner",
            device_ids=[str(uuid.uuid4())],
            topology_type=TopologyType.PHYSICAL,
            purpose="test",
            start_time=NOW - timedelta(hours=1),
            end_time=NOW + timedelta(hours=2),
            status=status,
            purpose_category=purpose_category,
            purpose_category_set_by=uuid.UUID(owner) if purpose_category else None,
            purpose_category_set_at=NOW if purpose_category else None,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


# --- GET /purpose-categories ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_purpose_categories_default_list():
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.get("/purpose-categories")
    assert resp.status_code == 200
    assert resp.json() == {"categories": list(settings.purpose_categories)}


@pytest.mark.asyncio
async def test_get_purpose_categories_reflects_env_override(monkeypatch):
    monkeypatch.setattr(settings, "purpose_categories", ["custom_a", "custom_b"])
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.get("/purpose-categories")
    assert resp.status_code == 200
    assert resp.json() == {"categories": ["custom_a", "custom_b"]}


# --- Create-time validation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_reservation_with_valid_purpose_category():
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
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(
                "/",
                json={
                    "device_ids": [DEVICE_A],
                    "purpose": "regression run",
                    "purpose_category": "qa_regression",
                    "start_time": START,
                    "end_time": END,
                },
            )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["purpose_category"] == "qa_regression"
    assert data["purpose_category_set_at"] is not None


@pytest.mark.asyncio
async def test_create_reservation_with_unknown_purpose_category_is_422():
    with patch(
        "app.services.reservation_service._fetch_devices",
        new=AsyncMock(return_value=[make_device_response(DEVICE_A)]),
    ):
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(
                "/",
                json={
                    "device_ids": [DEVICE_A],
                    "purpose_category": "not_a_real_category",
                    "start_time": START,
                    "end_time": END,
                },
            )
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "Unknown purpose_category 'not_a_real_category'; allowed: "
        + ", ".join(settings.purpose_categories)
    )


@pytest.mark.asyncio
async def test_create_reservation_without_purpose_category_stays_unset():
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
        async with _client_as(OWNER_ID) as ac:
            resp = await ac.post(
                "/",
                json={
                    "device_ids": [DEVICE_A],
                    "start_time": START,
                    "end_time": END,
                },
            )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["purpose_category"] is None
    assert data["purpose_category_set_at"] is None


# --- PATCH /{id}/purpose-category: ownership matrix -----------------------------------


@pytest.mark.asyncio
async def test_patch_purpose_category_by_owner():
    rid = await _insert_reservation()
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(f"/{rid}/purpose-category", json={"purpose_category": "training"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["purpose_category"] == "training"
    assert data["purpose_category_set_at"] is not None

    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        assert res.purpose_category_set_by == uuid.UUID(OWNER_ID)


@pytest.mark.asyncio
async def test_patch_purpose_category_by_admin():
    rid = await _insert_reservation(owner=OWNER_ID)
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.patch(
            f"/{rid}/purpose-category", json={"purpose_category": "customer_demo_poc"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["purpose_category"] == "customer_demo_poc"

    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        # The acting admin, not the owner, is recorded as the classifier.
        assert res.purpose_category_set_by == uuid.UUID(ADMIN_ID)


@pytest.mark.asyncio
async def test_patch_purpose_category_by_third_user_is_403():
    rid = await _insert_reservation(owner=OWNER_ID)
    async with _client_as(OTHER_ID) as ac:
        resp = await ac.patch(f"/{rid}/purpose-category", json={"purpose_category": "training"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_purpose_category_unknown_reservation_is_404():
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(
            f"/{uuid.uuid4()}/purpose-category", json={"purpose_category": "training"}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_purpose_category_unknown_category_is_422():
    rid = await _insert_reservation()
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(
            f"/{rid}/purpose-category", json={"purpose_category": "not_a_real_category"}
        )
    assert resp.status_code == 422
    assert "Unknown purpose_category" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_purpose_category_allowed_on_completed_reservation():
    rid = await _insert_reservation(status=ReservationStatus.COMPLETED)
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(
            f"/{rid}/purpose-category", json={"purpose_category": "support_case_replication"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["purpose_category"] == "support_case_replication"


@pytest.mark.asyncio
async def test_patch_purpose_category_null_clears_all_three_fields():
    rid = await _insert_reservation(purpose_category="qa_regression")
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(f"/{rid}/purpose-category", json={"purpose_category": None})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["purpose_category"] is None
    assert data["purpose_category_set_at"] is None

    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        assert res.purpose_category is None
        assert res.purpose_category_set_by is None
        assert res.purpose_category_set_at is None
