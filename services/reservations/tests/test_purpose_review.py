"""Admin purpose-review endpoints: accept, dismiss, the review-list filter, and
backfill (issue #646 phase 2, ADR 0013 point 10).
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
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
    # The first distribution entry is deliberately NOT top_category (issue #1525), so a
    # read of the distribution in place of `top_category` (RES-PURPOSE-10) shows.
    first = "other" if top_category != "other" else "training"
    return {
        "distribution": [
            {"category": first, "probability": 0.3},
            {"category": top_category, "probability": 0.7},
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
    purpose_suggested_at: datetime | None = None,
    reservation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with TestSessionLocal() as db:
        res = Reservation(
            id=reservation_id or uuid.uuid4(),
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
            purpose_suggested_at=purpose_suggested_at or (NOW if purpose_suggestion else None),
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


async def _snapshot(rid: uuid.UUID) -> dict:
    """Every mapped column of the row, read on a fresh session."""
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        return {a.key: getattr(res, a.key) for a in Reservation.__mapper__.column_attrs}


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
    assert resp.json()["detail"] == (
        "Unknown purpose_category; allowed: " + ", ".join(settings.purpose_categories)
    )
    assert "not_a_real_category" not in resp.text


@pytest.mark.asyncio
async def test_accept_with_a_lone_surrogate_category_is_422_and_changes_nothing():
    """Issue #1459: the echo of the value made this a 500; the shared body check
    refuses it first, and the suggestion stays pending."""
    rid = await _insert_reservation(purpose_suggestion=_suggestion("qa_regression"))
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{rid}/accept",
            content=b'{"purpose_category": "x\\ud800"}',
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["type"] == "string_unicode"
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        assert res.purpose_category is None
        assert res.purpose_category_set_by is None
        assert res.purpose_suggestion is not None


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


# --- Issue #1525: accept and dismiss touch only the named row; the list's order ---------


def _without(snapshot: dict, *keys: str) -> dict:
    return {k: v for k, v in snapshot.items() if k not in keys}


async def _two_suggested_rows() -> list[uuid.UUID]:
    """Two rows that differ in their id and their suggestion's top category only. Both
    carry an owner's category that disagrees with the suggestion, so both are on the
    review list and a write that clears or replaces the category shows."""
    return [
        await _insert_reservation(
            purpose_category="customer_demo_poc", purpose_suggestion=_suggestion("qa_regression")
        ),
        await _insert_reservation(
            purpose_category="customer_demo_poc", purpose_suggestion=_suggestion("training")
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_index", [0, 1])
async def test_accept_changes_only_the_named_row(target_index):
    """RES-PURPOSE-10 with a second suggested row present, for each insertion position
    of the named row: the named row takes its own suggestion's top category, the
    accepting admin as setter, and a set time; every other mapped column of it but
    `updated_at`, and every mapped column of the other row, stay as they were."""
    rows = await _two_suggested_rows()
    target, other = rows[target_index], rows[1 - target_index]
    target_top = (await _get_reservation(target)).purpose_suggestion["top_category"]
    target_before, other_before = await _snapshot(target), await _snapshot(other)

    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{target}/accept", json={"purpose_category": None}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(target)
    assert resp.json()["purpose_category"] == target_top

    after = await _snapshot(target)
    assert after["purpose_category"] == target_top
    assert after["purpose_category_set_by"] == uuid.UUID(ADMIN_ID)
    assert after["purpose_category_set_at"] is not None
    written = (
        "purpose_category",
        "purpose_category_set_by",
        "purpose_category_set_at",
        "updated_at",
    )
    assert _without(after, *written) == _without(target_before, *written)
    assert await _snapshot(other) == other_before


@pytest.mark.asyncio
@pytest.mark.parametrize("target_index", [0, 1])
async def test_dismiss_changes_only_the_named_row(target_index):
    """RES-PURPOSE-11 with a second suggested row present, for each insertion position
    of the named row: the named row gets `purpose_suggestion_dismissed_at`, and no
    other mapped column of it but `updated_at` changes; every mapped column of the
    other row stays as it was."""
    rows = await _two_suggested_rows()
    target, other = rows[target_index], rows[1 - target_index]
    target_before, other_before = await _snapshot(target), await _snapshot(other)

    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(f"/admin/purpose-review/{target}/dismiss")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(target)

    after = await _snapshot(target)
    assert after["purpose_suggestion_dismissed_at"] is not None
    written = ("purpose_suggestion_dismissed_at", "updated_at")
    assert _without(after, *written) == _without(target_before, *written)
    assert await _snapshot(other) == other_before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "dismiss"])
async def test_accept_and_dismiss_of_an_unknown_id_is_404_beside_suggested_rows(action):
    """An id that names no row is 404 `Reservation not found` even with two suggested
    rows present, and no mapped column of either row changes."""
    rows = await _two_suggested_rows()
    before = {rid: await _snapshot(rid) for rid in rows}
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{uuid.uuid4()}/{action}", json={"purpose_category": None}
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Reservation not found"
    for rid in rows:
        assert await _snapshot(rid) == before[rid]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "dismiss"])
@pytest.mark.parametrize("target_index", [0, 1])
async def test_accept_and_dismiss_read_the_named_rows_suggestion(action, target_index):
    """The 409 for a row with no suggestion is decided on the named row: with a second
    row that has a suggestion, the named row in either insertion position still gets
    409 `Reservation has no suggestion to accept`, and no mapped column of either row
    changes."""
    rows = []
    for index in range(2):
        suggestion = None if index == target_index else _suggestion("qa_regression")
        rows.append(await _insert_reservation(purpose_suggestion=suggestion))
    target = rows[target_index]
    before = {rid: await _snapshot(rid) for rid in rows}
    async with _client_as(ADMIN_ID, role="admin") as ac:
        resp = await ac.post(
            f"/admin/purpose-review/{target}/{action}", json={"purpose_category": None}
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Reservation has no suggestion to accept"
    for rid in rows:
        assert await _snapshot(rid) == before[rid]


@pytest.mark.asyncio
async def test_review_list_is_newest_suggestion_first_then_by_id():
    """RES-PURPOSE-15: the review list is ordered by `purpose_suggested_at`, newest
    first, then by reservation id. Rows are inserted in an order that is neither the
    expected one nor its reverse, and the two rows that share the newest time are
    inserted larger id first, so the result is the expected order only through the
    ORDER BY. Paging by two returns the same order with no row repeated or skipped."""
    low_id, high_id = sorted([uuid.uuid4(), uuid.uuid4()])
    three_hours = await _insert_reservation(
        purpose_suggestion=_suggestion(), purpose_suggested_at=NOW - timedelta(hours=3)
    )
    one_hour = await _insert_reservation(
        purpose_suggestion=_suggestion(), purpose_suggested_at=NOW - timedelta(hours=1)
    )
    two_hours = await _insert_reservation(
        purpose_suggestion=_suggestion(), purpose_suggested_at=NOW - timedelta(hours=2)
    )
    for rid in (high_id, low_id):
        await _insert_reservation(
            reservation_id=rid,
            purpose_suggestion=_suggestion(),
            purpose_suggested_at=NOW - timedelta(minutes=30),
        )
    expected = [str(r) for r in (low_id, high_id, one_hour, two_hours, three_hours)]

    async with _client_as(ADMIN_ID, role="admin") as ac:
        whole = await ac.get("/admin/purpose-review")
        pages = [
            await ac.get("/admin/purpose-review", params={"skip": skip, "limit": 2})
            for skip in (0, 2, 4)
        ]
    assert whole.status_code == 200, whole.text
    assert [i["reservation_id"] for i in whole.json()["items"]] == expected
    paged = []
    for page in pages:
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 5
        paged += [i["reservation_id"] for i in page.json()["items"]]
    assert paged == expected


# Every purpose review route (RES-PURPOSE-14), as a literal list: the purpose review rows
# of the section 5 route table. A route added later is not picked up here on its own.
_PURPOSE_REVIEW_ROUTES = [
    ("GET", "/admin/purpose-review"),
    ("POST", "/admin/purpose-review/{rid}/accept"),
    ("POST", "/admin/purpose-review/{rid}/dismiss"),
    ("POST", "/admin/purpose-review/{rid}/classify"),
    ("POST", "/admin/purpose/backfill"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _PURPOSE_REVIEW_ROUTES)
async def test_every_purpose_review_route_refuses_a_non_admin(method, path):
    """RES-PURPOSE-14: a signed-in user without the admin role gets 403 `Admin or
    superadmin role required` from every purpose review route, no orchestrator call is
    made, and no mapped column of either seeded row changes (one of them is a terminal
    row backfill would stamp). The named row is stamped and carries a suggestion, and
    every request carries a valid accept body, so without the role check no route
    answers 403: list, accept, dismiss, and backfill answer 200 and classify answers
    409 `already_suggested`."""
    rid = await _insert_reservation(
        purpose_suggestion=_suggestion("qa_regression"),
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
    )
    unstamped = await _insert_reservation(status=ReservationStatus.COMPLETED)
    before = {r: await _snapshot(r) for r in (rid, unstamped)}
    call = AsyncMock(return_value=httpx.Response(500))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(OTHER_ID, role="user") as ac:
            resp = await ac.request(method, path.format(rid=rid), json={"purpose_category": None})
    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "Admin or superadmin role required"}
    call.assert_not_awaited()
    for r in (rid, unstamped):
        assert await _snapshot(r) == before[r]
