"""Admin purpose-review endpoints: accept, dismiss, the review-list filter, and
backfill (issue #646 phase 2, ADR 0013 point 10).
"""

import uuid
from datetime import datetime, timedelta, timezone

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

NOW = datetime.now(timezone.utc)


def _client_as(sub: str, role: str = "user") -> AsyncClient:
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _suggestion(top_category: str = "qa_regression") -> dict:
    return {
        "distribution": [
            {"category": top_category, "probability": 0.7},
            {"category": "other", "probability": 0.3},
        ],
        "top_category": top_category,
        "pass": "end",
        "model": "test-model",
        "rationale": "test rationale",
        "generated_at": NOW.isoformat(),
        "signals_used": ["purpose_text"],
    }


async def _insert_reservation(
    *,
    owner: str = OWNER_ID,
    status: ReservationStatus = ReservationStatus.COMPLETED,
    purpose_category: str | None = None,
    purpose_suggestion: dict | None = None,
    purpose_suggestion_dismissed_at: datetime | None = None,
    purpose_classify_requested_at: datetime | None = None,
    purpose_classify_attempts: int = 0,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        res = Reservation(
            user_id=uuid.UUID(owner),
            owner_name="owner",
            device_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
            topology_type=TopologyType.PHYSICAL,
            purpose="test",
            start_time=NOW - timedelta(hours=3),
            end_time=NOW - timedelta(hours=1),
            status=status,
            purpose_category=purpose_category,
            purpose_suggestion=purpose_suggestion,
            purpose_suggested_at=NOW if purpose_suggestion else None,
            purpose_suggestion_dismissed_at=purpose_suggestion_dismissed_at,
            purpose_classify_requested_at=purpose_classify_requested_at,
            purpose_classify_attempts=purpose_classify_attempts,
        )
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _get_reservation(rid: uuid.UUID) -> Reservation:
    async with TestSessionLocal() as db:
        return await db.get(Reservation, rid)


# --- GET /admin/purpose-review: the review-list filter ---------------------------------


@pytest.mark.asyncio
async def test_review_list_excludes_confirmed_agreeing_row():
    """A confirmed category matching the suggestion's top_category is not review work."""
    await _insert_reservation(
        purpose_category="qa_regression", purpose_suggestion=_suggestion("qa_regression")
    )
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_review_list_includes_confirmed_disagreeing_row():
    """A confirmed category that disagrees with the AI stays in the queue (an admin
    may want to overrule the human pick, ADR 0013 point 10)."""
    rid = await _insert_reservation(
        purpose_category="training", purpose_suggestion=_suggestion("qa_regression")
    )
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["reservation_id"] for i in items] == [str(rid)]
    assert items[0]["purpose_category"] == "training"
    assert items[0]["purpose_suggestion"]["top_category"] == "qa_regression"
    assert items[0]["device_count"] == 2


@pytest.mark.asyncio
async def test_review_list_includes_unconfirmed_row():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("customer_demo_poc"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 200, resp.text
    assert [i["reservation_id"] for i in resp.json()["items"]] == [str(rid)]


@pytest.mark.asyncio
async def test_review_list_excludes_dismissed_row():
    await _insert_reservation(
        purpose_suggestion=_suggestion("qa_regression"),
        purpose_suggestion_dismissed_at=NOW,
    )
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_review_list_excludes_row_with_no_suggestion():
    await _insert_reservation(purpose_suggestion=None)
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_review_list_category_filter():
    await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    await _insert_reservation(purpose_suggestion=_suggestion("training"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review", params={"category": "training"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["purpose_suggestion"]["top_category"] == "training"


@pytest.mark.asyncio
async def test_review_list_pagination():
    for _ in range(3):
        await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.get("/admin/purpose-review", params={"skip": 0, "limit": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["skip"] == 0
    assert body["limit"] == 2


@pytest.mark.asyncio
async def test_review_list_is_admin_only():
    await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(OTHER_ID, role="user") as ac:
        resp = await ac.get("/admin/purpose-review")
    assert resp.status_code == 403


# --- POST /admin/purpose-review/{id}/accept ---------------------------------------------


@pytest.mark.asyncio
async def test_accept_with_null_uses_top_category():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/accept", json={"purpose_category": None})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["purpose_category"] == "qa_regression"

    res = await _get_reservation(rid)
    assert res.purpose_category_set_by == uuid.UUID(ADMIN_ID)
    assert res.purpose_category_set_at is not None


@pytest.mark.asyncio
async def test_accept_with_chosen_value_overrides_top_category():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{rid}/accept", json={"purpose_category": "training"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["purpose_category"] == "training"


@pytest.mark.asyncio
async def test_accept_unknown_category_value_is_422():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{rid}/accept",
            json={"purpose_category": "not_a_real_category"},
        )
    assert resp.status_code == 422
    assert "Unknown purpose_category" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_accept_unknown_reservation_is_404():
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{uuid.uuid4()}/accept", json={"purpose_category": None}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accept_no_suggestion_is_409():
    rid = await _insert_reservation(purpose_suggestion=None)
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/accept", json={"purpose_category": None})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Reservation has no suggestion to accept"


@pytest.mark.asyncio
async def test_accept_is_admin_only():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(OTHER_ID, role="user") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/accept", json={"purpose_category": None})
    assert resp.status_code == 403


# --- POST /admin/purpose-review/{id}/dismiss --------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_sets_dismissed_at_and_keeps_suggestion():
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/dismiss")
    assert resp.status_code == 200, resp.text
    assert resp.json()["purpose_suggestion"]["top_category"] == "qa_regression"

    res = await _get_reservation(rid)
    assert res.purpose_suggestion_dismissed_at is not None
    assert res.purpose_suggestion is not None


@pytest.mark.asyncio
async def test_dismiss_unknown_reservation_is_404():
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{uuid.uuid4()}/dismiss")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_no_suggestion_is_409():
    rid = await _insert_reservation(purpose_suggestion=None)
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/dismiss")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Reservation has no suggestion to accept"


# --- POST /admin/purpose/backfill ---------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_marks_terminal_rows_without_suggestion():
    eligible = await _insert_reservation(status=ReservationStatus.CANCELLED)
    already_suggested = await _insert_reservation(
        status=ReservationStatus.FAILED, purpose_suggestion=_suggestion("qa_regression")
    )
    active = await _insert_reservation(status=ReservationStatus.ACTIVE)

    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post("/admin/purpose/backfill")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"marked": 1}

    res_eligible = await _get_reservation(eligible)
    assert res_eligible.purpose_classify_requested_at is not None
    res_suggested = await _get_reservation(already_suggested)
    assert res_suggested.purpose_classify_requested_at is None
    res_active = await _get_reservation(active)
    assert res_active.purpose_classify_requested_at is None


@pytest.mark.asyncio
async def test_backfill_is_idempotent():
    await _insert_reservation(status=ReservationStatus.COMPLETED)
    async with _client_as(ADMIN_ID, role="admin") as ac:
        first = await ac.post("/admin/purpose/backfill")
        second = await ac.post("/admin/purpose/backfill")
    assert first.json() == {"marked": 1}
    assert second.json() == {"marked": 0}


@pytest.mark.asyncio
async def test_backfill_resets_capped_rows_without_suggestion():
    """A row that hit the sweep's attempt cap must not be permanently stuck
    (the defect this guards: before the fix, backfill only touched rows with
    purpose_classify_requested_at still null, so a capped row, which already
    has that column set from its first sweep pickup, was never selected).
    """
    max_attempts = settings.purpose_classify_max_attempts
    capped = await _insert_reservation(
        status=ReservationStatus.COMPLETED,
        purpose_classify_requested_at=NOW - timedelta(hours=1),
        purpose_classify_attempts=max_attempts,
    )
    # A row that has retried but is not yet at the cap must be left alone.
    not_yet_capped = await _insert_reservation(
        status=ReservationStatus.COMPLETED,
        purpose_classify_requested_at=NOW - timedelta(hours=1),
        purpose_classify_attempts=max_attempts - 1,
    )
    # A capped row that already carries a suggestion (classified on its last
    # attempt before the counter was read) must not be reset either.
    capped_but_suggested = await _insert_reservation(
        status=ReservationStatus.COMPLETED,
        purpose_suggestion=_suggestion("training"),
        purpose_classify_requested_at=NOW - timedelta(hours=1),
        purpose_classify_attempts=max_attempts,
    )

    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post("/admin/purpose/backfill")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"marked": 1}

    res_capped = await _get_reservation(capped)
    assert res_capped.purpose_classify_attempts == 0
    assert res_capped.purpose_classify_requested_at is not None

    res_not_yet_capped = await _get_reservation(not_yet_capped)
    assert res_not_yet_capped.purpose_classify_attempts == max_attempts - 1

    res_capped_but_suggested = await _get_reservation(capped_but_suggested)
    assert res_capped_but_suggested.purpose_classify_attempts == max_attempts


@pytest.mark.asyncio
async def test_backfill_counts_newly_marked_and_reset_rows_together():
    max_attempts = settings.purpose_classify_max_attempts
    await _insert_reservation(status=ReservationStatus.CANCELLED)
    await _insert_reservation(
        status=ReservationStatus.FAILED,
        purpose_classify_requested_at=NOW - timedelta(hours=1),
        purpose_classify_attempts=max_attempts,
    )

    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post("/admin/purpose/backfill")
    assert resp.json() == {"marked": 2}


@pytest.mark.asyncio
async def test_backfill_is_admin_only():
    async with _client_as(OTHER_ID, role="user") as ac:
        resp = await ac.post("/admin/purpose/backfill")
    assert resp.status_code == 403
