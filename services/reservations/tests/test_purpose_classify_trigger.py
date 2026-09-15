"""POST /admin/purpose-review/{id}/classify (issue #808): the on-demand,
per-reservation escape hatch around the purpose-classify sweep's
oldest-requested-first queue.

The router calls app.services.purpose_service.classify_purpose_one directly,
the exact function the sweep reconciler uses, so this file shares
test_purpose_classify_reconcile.py's app.database-engine setup (rather than
tests/_harness.py's private one): classify_purpose_one opens its own
AsyncSessionLocal against app.database's engine, so a router-level test must
put the seeded reservation on that same engine, and app.dependency_overrides
therefore leaves get_db un-overridden (only the auth dependencies are
overridden) so the router's own session lands on the same engine too.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import AsyncSessionLocal, Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests._harness import override_bearer

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def _override_get_db():
    """Route the HTTP client's own session onto app.database's engine too.

    classify_purpose_one opens its own AsyncSessionLocal against
    app.database's engine (it has to: it also runs from the sweep loop,
    which has no request-scoped session to reuse), so the router's session
    must land on the SAME engine for a seeded row to be visible to both.
    Left un-overridden, FastAPI's default get_db already resolves there, but
    app.dependency_overrides is a global dict on the shared `app` object: a
    PRECEDING test file that overrode get_db to tests._harness's private
    engine and never cleared it would otherwise leak into this file's tests
    purely by import order. Setting it explicitly removes that dependency on
    collection order.
    """
    async with AsyncSessionLocal() as session:
        yield session


USER_ID = uuid.uuid4()
ADMIN_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


def _client_as(sub: str, role: str = "admin") -> AsyncClient:
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": sub,
        "username": "u",
        "role": role,
    }
    app.dependency_overrides[bearer_scheme] = override_bearer
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _suggestion_response(top_category: str = "qa_regression") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "distribution": [{"category": top_category, "probability": 0.9}],
            "top_category": top_category,
            "pass": "end",
            "model": "test-model",
            "rationale": "r",
            "generated_at": NOW.isoformat(),
            "signals_used": ["purpose_text"],
        },
    )


async def _insert(
    *,
    purpose_classify_requested_at: datetime | None = NOW - timedelta(minutes=5),
    purpose_classify_attempts: int = 0,
    purpose_suggestion: dict | None = None,
    status: ReservationStatus = ReservationStatus.COMPLETED,
) -> uuid.UUID:
    res = Reservation(
        user_id=USER_ID,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_type=TopologyType.PHYSICAL,
        purpose="a support case replication run",
        start_time=NOW - timedelta(hours=3),
        end_time=NOW - timedelta(hours=1),
        status=status,
        purpose_classify_requested_at=purpose_classify_requested_at,
        purpose_classify_attempts=purpose_classify_attempts,
        purpose_suggestion=purpose_suggestion,
    )
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _get(rid: uuid.UUID) -> Reservation:
    async with TestSessionLocal() as db:
        return await db.get(Reservation, rid)


@pytest.mark.asyncio
async def test_trigger_200_ok_stores_suggestion():
    rid = await _insert()
    call = AsyncMock(return_value=_suggestion_response("qa_regression"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reservation_id"] == str(rid)
    assert body["outcome"] == "ok"
    assert body["purpose_suggestion"]["top_category"] == "qa_regression"

    res = await _get(rid)
    assert res.purpose_suggestion["top_category"] == "qa_regression"
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_trigger_200_timeout_bumps_attempts():
    rid = await _insert()
    call = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "timeout"
    assert body["purpose_suggestion"] is None

    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 1


@pytest.mark.asyncio
async def test_trigger_200_transient_does_not_bump_attempts():
    rid = await _insert()
    call = AsyncMock(return_value=httpx.Response(503, text="unavailable"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "transient"

    res = await _get(rid)
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_trigger_200_failed_bumps_attempts():
    rid = await _insert()
    call = AsyncMock(return_value=httpx.Response(500, text="internal error"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "failed"

    res = await _get(rid)
    assert res.purpose_classify_attempts == 1


@pytest.mark.asyncio
async def test_trigger_200_forbidden_bad_token():
    rid = await _insert()
    call = AsyncMock(return_value=httpx.Response(403, json={"detail": "Invalid internal token"}))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "forbidden"

    res = await _get(rid)
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_trigger_ignores_the_attempt_cap():
    """A row already at the sweep's attempt cap is still classified: the
    trigger bypasses purpose_classify_max_attempts by construction, since it
    calls classify_purpose_one directly rather than going through the
    reconciler's capped SELECT (issue #808).
    """
    from app.config import settings

    rid = await _insert(purpose_classify_attempts=settings.purpose_classify_max_attempts)
    call = AsyncMock(return_value=_suggestion_response("training"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "ok"
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_503_feature_off():
    rid = await _insert()
    call = AsyncMock(
        return_value=httpx.Response(
            403,
            json={
                "detail": {
                    "error": "purpose_classification_disabled",
                    "message": "Purpose classification is disabled",
                }
            },
        )
    )
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == {"error": "purpose_classification_disabled"}

    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_trigger_503_feature_off_via_404():
    """A 404 (a mixed-version deployment) reads as feature_off too, exactly
    like the sweep (issue #706's amendment, unchanged by this endpoint)."""
    rid = await _insert()
    call = AsyncMock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == {"error": "purpose_classification_disabled"}


@pytest.mark.asyncio
async def test_trigger_404_unknown_reservation():
    async with _client_as(ADMIN_ID) as ac:
        resp = await ac.post(f"/admin/purpose-review/{uuid.uuid4()}/classify")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Reservation not found"


@pytest.mark.asyncio
async def test_trigger_409_not_eligible():
    rid = await _insert(purpose_classify_requested_at=None, status=ReservationStatus.ACTIVE)
    async with _client_as(ADMIN_ID) as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "not_eligible"}


@pytest.mark.asyncio
async def test_trigger_409_already_suggested():
    rid = await _insert(purpose_suggestion={"top_category": "qa_regression"})
    async with _client_as(ADMIN_ID) as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "already_suggested"}


@pytest.mark.asyncio
async def test_trigger_is_admin_only():
    rid = await _insert()
    async with _client_as(OTHER_ID, role="user") as ac:
        resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 403
