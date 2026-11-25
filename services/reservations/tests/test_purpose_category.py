"""Lab purpose classification: taxonomy config, create-time validation, the
PATCH ownership matrix, and the null-clears-all-three semantics (issue #646
phase 1).
"""

import json
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
from sqlalchemy import func, select

from tests._harness import TestSessionLocal, override_bearer, override_get_db

OWNER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())
DEVICE_A = str(uuid.uuid4())
MOCK_TEMPLATE_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _clock():
    """Read the clock when each test starts, never at import (#1561)."""
    global NOW, START, END
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
    # The field and the allowed values, never the submitted value (issue #1459).
    assert resp.json()["detail"] == (
        "Unknown purpose_category; allowed: " + ", ".join(settings.purpose_categories)
    )
    assert "not_a_real_category" not in resp.text


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
    assert resp.json()["detail"] == (
        "Unknown purpose_category; allowed: " + ", ".join(settings.purpose_categories)
    )
    assert "not_a_real_category" not in resp.text


# --- Issue #1459: no echo, and a lone surrogate is a 422 that writes nothing --------

_SUR = chr(0xD800)  # built, so no source escape is decoded early


def _escaped(value) -> bytes:
    """A raw body with every non-ASCII code point escaped (`json=` cannot send a lone
    surrogate)."""
    return json.dumps(value, ensure_ascii=True).encode("ascii")


_STRING_UNICODE = {
    "type": "string_unicode",
    "loc": ["body"],
    "msg": "Input should be a valid string, unable to parse raw data as a unicode string",
}


@pytest.mark.asyncio
async def test_create_with_a_lone_surrogate_category_is_422_not_500():
    body = {
        "device_ids": [DEVICE_A],
        "purpose_category": "x" + _SUR,
        "start_time": START,
        "end_time": END,
    }
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.post(
            "/", content=_escaped(body), headers={"content-type": "application/json"}
        )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": [_STRING_UNICODE]}
    async with TestSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(Reservation))).scalar() == 0


@pytest.mark.asyncio
async def test_patch_with_a_lone_surrogate_category_is_422_and_changes_nothing():
    rid = await _insert_reservation(purpose_category="training")
    async with _client_as(OWNER_ID) as ac:
        resp = await ac.patch(
            f"/{rid}/purpose-category",
            content=_escaped({"purpose_category": "x" + _SUR}),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": [_STRING_UNICODE]}
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        assert res.purpose_category == "training"
        assert res.purpose_category_set_by == uuid.UUID(OWNER_ID)
        assert res.purpose_category_set_at.replace(tzinfo=timezone.utc) == NOW


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


# --- PATCH /{id}/purpose-category writes the named row and no other (issue #1533) ------

SUPERADMIN_ID = str(uuid.uuid4())
PRIOR_SETTER_ID = str(uuid.uuid4())
BYSTANDER_SETTER_ID = str(uuid.uuid4())
_CALLERS = {
    "owner": (OWNER_ID, "user"),
    "admin": (ADMIN_ID, "admin"),
    "superadmin": (SUPERADMIN_ID, "superadmin"),
}


async def _insert_classified_row(
    *, status: ReservationStatus, category: str, set_by: str, set_at: datetime, purpose: str
) -> uuid.UUID:
    """An OWNER_ID row that already carries a category set by `set_by` at `set_at`."""
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(OWNER_ID),
            owner_name="owner",
            device_ids=[str(uuid.uuid4())],
            topology_type=TopologyType.PHYSICAL,
            purpose=purpose,
            start_time=NOW - timedelta(hours=1),
            end_time=NOW + timedelta(hours=2),
            status=status,
            purpose_category=category,
            purpose_category_set_by=uuid.UUID(set_by),
            purpose_category_set_at=set_at,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _target_row(status: ReservationStatus) -> uuid.UUID:
    return await _insert_classified_row(
        status=status,
        category="training",
        set_by=PRIOR_SETTER_ID,
        set_at=NOW - timedelta(days=3),
        purpose="target",
    )


async def _bystander_row(status: ReservationStatus) -> uuid.UUID:
    """Same owner and status as the target; every classification field differs."""
    return await _insert_classified_row(
        status=status,
        category="customer_demo_poc",
        set_by=BYSTANDER_SETTER_ID,
        set_at=NOW - timedelta(days=5),
        purpose="bystander",
    )


async def _snapshot(rid: uuid.UUID) -> dict:
    """Every mapped column of the row, plus its device set."""
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        snap = {attr.key: getattr(res, attr.key) for attr in Reservation.__mapper__.column_attrs}
        snap["device_ids"] = sorted(str(d) for d in res.device_ids)
        return snap


_WRITTEN = {"purpose_category", "purpose_category_set_by", "purpose_category_set_at", "updated_at"}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["performance_benchmark", None], ids=["set", "clear"])
@pytest.mark.parametrize("caller", sorted(_CALLERS))
@pytest.mark.parametrize("status", list(ReservationStatus), ids=lambda s: s.value)
async def test_patch_purpose_category_writes_only_the_named_row(status, caller, value):
    """RES-PURPOSE-2: the owner, an admin, or a superadmin sets or clears the
    category of the reservation named in the path, in every status. With a second
    reservation of the same owner and status present, the named row gets the new
    category with the caller as setter and a time taken during the request (all
    three null on a clear), and its columns other than those three and
    updated_at are unchanged; every column of the second row is unchanged."""
    target = await _target_row(status)
    bystander = await _bystander_row(status)
    target_before = await _snapshot(target)
    bystander_before = await _snapshot(bystander)
    sub, role = _CALLERS[caller]

    t0 = datetime.now(timezone.utc)
    async with _client_as(sub, role=role) as ac:
        resp = await ac.patch(f"/{target}/purpose-category", json={"purpose_category": value})
    t1 = datetime.now(timezone.utc)

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(target)
    assert resp.json()["purpose_category"] == value
    after = await _snapshot(target)
    assert after["purpose_category"] == value
    if value is None:
        assert after["purpose_category_set_by"] is None
        assert after["purpose_category_set_at"] is None
    else:
        assert after["purpose_category_set_by"] == uuid.UUID(sub)
        assert t0 <= after["purpose_category_set_at"].replace(tzinfo=timezone.utc) <= t1
    assert {k: v for k, v in after.items() if k not in _WRITTEN} == {
        k: v for k, v in target_before.items() if k not in _WRITTEN
    }
    assert await _snapshot(bystander) == bystander_before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(ReservationStatus), ids=lambda s: s.value)
async def test_patch_purpose_category_by_a_third_user_is_403_and_writes_nothing(status):
    """RES-PURPOSE-2: a user who is neither the owner nor an admin gets 403 with the
    pinned wording in every status, and neither of the two rows changes."""
    target = await _target_row(status)
    bystander = await _bystander_row(status)
    before = {rid: await _snapshot(rid) for rid in (target, bystander)}
    async with _client_as(OTHER_ID) as ac:
        resp = await ac.patch(
            f"/{target}/purpose-category", json={"purpose_category": "performance_benchmark"}
        )
    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Only the reservation owner or an admin may set its purpose category"
    }
    assert {rid: await _snapshot(rid) for rid in (target, bystander)} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 2])
@pytest.mark.parametrize("caller", sorted(_CALLERS))
async def test_patch_purpose_category_unknown_id_is_404_and_writes_nothing(caller, rows):
    """The error table's 404 row, which cites RES-PURPOSE-2: an id no reservation
    has answers 404 `Reservation not found` for the seeded rows' owner, an admin,
    and a superadmin, with one or two other rows present, and no seeded row
    changes."""
    seeded = [await _target_row(ReservationStatus.ACTIVE)]
    if rows == 2:
        seeded.append(await _bystander_row(ReservationStatus.ACTIVE))
    before = {rid: await _snapshot(rid) for rid in seeded}
    sub, role = _CALLERS[caller]
    async with _client_as(sub, role=role) as ac:
        resp = await ac.patch(
            f"/{uuid.uuid4()}/purpose-category", json={"purpose_category": "performance_benchmark"}
        )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Reservation not found"}
    assert {rid: await _snapshot(rid) for rid in seeded} == before
