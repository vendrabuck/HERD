"""The end-of-reservation purpose-classification sweep reconciler (issue #646
phase 2, ADR 0013 point 8's second pass; issue #706's outcome-taxonomy fix):
app.tasks.expiration._run_purpose_classify_reconcile and its per-row helper
_classify_purpose_one.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import settings
from app.database import Base, engine
from app.models.reservation import (
    Reservation,
    ReservationDynamicRequest,
    ReservationStatus,
    TopologyType,
)
from app.tasks.expiration import _run_purpose_classify_reconcile
from sqlalchemy.ext.asyncio import async_sessionmaker

# The structured 403 detail the orchestrator's flag refusal carries (issue
# #706), matching services/ai-orchestrator/app/routes/purpose_classification.py's
# PURPOSE_CLASSIFICATION_DISABLED_DETAIL. Reservations does not import across
# the service boundary (HTTP only), so this is duplicated deliberately, the
# same way the two services' contracts are pinned independently everywhere
# else in this codebase.
_FEATURE_OFF_DETAIL = {
    "error": "purpose_classification_disabled",
    "message": "Purpose classification is disabled",
}
# The exact string a pre-#706 orchestrator image answers for the same
# refusal; the fallback the fix must still recognize as feature-off.
_LEGACY_FEATURE_OFF_DETAIL = "Purpose classification is disabled"

# The reconciler opens its own AsyncSessionLocal against the app engine
# (mirrors test_expiration.py and test_fork_archive_reconcile.py).
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

USER_ID = uuid.uuid4()
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


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
    purpose_classify_requested_at: datetime | None,
    purpose_classify_attempts: int = 0,
    purpose_suggestion: dict | None = None,
    dynamic: bool = False,
) -> uuid.UUID:
    res = Reservation(
        user_id=USER_ID,
        owner_name="owner",
        device_ids=[str(uuid.uuid4())],
        topology_type=TopologyType.PHYSICAL,
        purpose="a support case replication run",
        start_time=NOW - timedelta(hours=3),
        end_time=NOW - timedelta(hours=1),
        status=ReservationStatus.COMPLETED,
        purpose_classify_requested_at=purpose_classify_requested_at,
        purpose_classify_attempts=purpose_classify_attempts,
        purpose_suggestion=purpose_suggestion,
    )
    if dynamic:
        res.dynamic_requests = [ReservationDynamicRequest(template_id=uuid.uuid4())]
    async with TestSessionLocal() as db:
        db.add(res)
        await db.commit()
        await db.refresh(res)
        return res.id


async def _get(rid: uuid.UUID) -> Reservation:
    async with TestSessionLocal() as db:
        return await db.get(Reservation, rid)


@pytest.mark.asyncio
async def test_reconcile_stores_suggestion_on_200():
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5), dynamic=True)
    call = AsyncMock(return_value=_suggestion_response("qa_regression"))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()

    res = await _get(rid)
    assert res.purpose_suggestion["top_category"] == "qa_regression"
    assert res.purpose_suggested_at is not None
    assert res.purpose_classify_attempts == 0

    # The call carries the documented payload shape.
    sent = call.await_args.kwargs["json_body"]
    assert sent["reservation_id"] == str(rid)
    assert sent["categories"] == list(settings.purpose_categories)
    assert sent["status"] == "COMPLETED"
    assert sent["dynamic_requests"] == [
        {"template_id": str((await _get(rid)).dynamic_requests[0].template_id), "count": 1}
    ]


@pytest.mark.asyncio
async def test_reconcile_no_dynamic_requests_sends_null():
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    sent = call.await_args.kwargs["json_body"]
    assert sent["dynamic_requests"] is None
    assert (await _get(rid)).purpose_suggestion is not None


@pytest.mark.asyncio
async def test_reconcile_403_structured_marker_ends_tick_without_touching_any_row():
    """The structured {"error": "purpose_classification_disabled"} 403 (issue
    #706) is feature-off: the tick ends at the first row without touching any
    row, including later ones in the same batch."""
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(403, json={"detail": _FEATURE_OFF_DETAIL}))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()

    # Only the first (oldest-requested) row was even attempted; the tick ended
    # there without touching the second.
    call.assert_awaited_once()
    for rid in (rid_first, rid_second):
        res = await _get(rid)
        assert res.purpose_suggestion is None
        assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_403_legacy_string_detail_is_still_feature_off():
    """A pre-#706 orchestrator image answers the exact legacy plain-string
    detail instead of the structured marker; the reconciler must still read
    that as feature-off (the documented fallback), not burn an attempt."""
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(403, json={"detail": _LEGACY_FEATURE_OFF_DETAIL}))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_403_bad_token_is_not_feature_off(caplog):
    """Issue #706: a 403 that is NOT the feature-off marker (an internal-token
    mismatch, in practice) is a different problem. Observably it still ends
    the tick without bumping any row's attempts (like feature-off), but it
    must be logged at WARNING, not the feature-off case's INFO, since this is
    a configuration problem an operator needs to see and fix.
    """
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(403, json={"detail": "Invalid internal token"}))
    with patch("app.tasks.expiration.call_service", call), caplog.at_level(logging.WARNING):
        await _run_purpose_classify_reconcile()

    call.assert_awaited_once()
    for rid in (rid_first, rid_second):
        res = await _get(rid)
        assert res.purpose_suggestion is None
        assert res.purpose_classify_attempts == 0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(getattr(r, "action", None) == "purpose_classify_forbidden" for r in warnings)


@pytest.mark.asyncio
async def test_reconcile_404_ends_tick_without_touching_any_row():
    """A 404 means the orchestrator image predates the classify endpoint (a
    mixed-version deployment); this is "not available yet", the same as a
    403, not a per-row failure, so it must not burn any row's attempts.
    """
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()

    call.assert_awaited_once()
    for rid in (rid_first, rid_second):
        res = await _get(rid)
        assert res.purpose_suggestion is None
        assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_404_then_later_tick_still_untouched():
    """Three sweep ticks against a 404-returning orchestrator must never push
    a row toward the attempt cap (the defect this test guards against: before
    the fix, a 404 was treated as an ordinary failure and bumped attempts on
    every tick).
    """
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(404, json={"detail": "not found"}))
    with patch("app.tasks.expiration.call_service", call):
        for _ in range(3):
            await _run_purpose_classify_reconcile()

    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_500_increments_attempts():
    """A 500 is not in the transient set (429/502/503/504): it is an ordinary
    per-row failure and bumps attempts, unlike the transient codes below."""
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(500, text="internal error"))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
async def test_reconcile_transient_status_ends_tick_without_bump(status_code):
    """Issue #706: 429 (rate limit), 502/503/504 (misconfiguration or an
    outage, including the AI_NOT_CONFIGURED_DETAIL 503 case) are all
    transient: the tick ends at the first row, no attempt is counted, and a
    later row in the same batch is left untouched, exactly like feature_off.
    """
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(status_code, text="unavailable"))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()

    call.assert_awaited_once()
    for rid in (rid_first, rid_second):
        res = await _get(rid)
        assert res.purpose_suggestion is None
        assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_sustained_429_three_ticks_leave_attempts_at_zero():
    """The literal issue #706 regression case: a quota 429 can last until UTC
    midnight, hundreds of ticks; none of them may burn this row's attempt cap."""
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(return_value=httpx.Response(429, text="rate limited"))
    with patch("app.tasks.expiration.call_service", call):
        for _ in range(3):
            await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_timeout_is_transient_no_bump():
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_transport_error_never_raises_and_is_transient():
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("app.tasks.expiration.call_service", call):
        # Must not raise.
        await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_skips_rows_at_attempt_cap():
    rid = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_classify_attempts=settings.purpose_classify_max_attempts,
    )
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == settings.purpose_classify_max_attempts


@pytest.mark.asyncio
async def test_reconcile_ignores_rows_not_yet_requested():
    await _insert(purpose_classify_requested_at=None)
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_ignores_rows_already_suggested():
    await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_suggestion={"top_category": "training"},
    )
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_respects_batch_size(monkeypatch):
    monkeypatch.setattr(settings, "purpose_classify_batch_size", 2)
    rids = [
        await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10 - i))
        for i in range(3)
    ]
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.tasks.expiration.call_service", call):
        await _run_purpose_classify_reconcile()
    assert call.await_count == 2

    classified = [rid for rid in rids if (await _get(rid)).purpose_suggestion is not None]
    assert len(classified) == 2
    # Oldest-requested rows go first.
    assert rids[0] in classified
    assert rids[1] in classified
    assert rids[2] not in classified


@pytest.mark.asyncio
async def test_reconcile_oldest_requested_first():
    older = await _insert(purpose_classify_requested_at=NOW - timedelta(hours=2))
    newer = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=1))
    seen_order: list[uuid.UUID] = []

    async def _fake_call(*args, **kwargs):
        seen_order.append(uuid.UUID(kwargs["json_body"]["reservation_id"]))
        return _suggestion_response()

    with patch("app.tasks.expiration.call_service", AsyncMock(side_effect=_fake_call)):
        await _run_purpose_classify_reconcile()
    assert seen_order == [older, newer]
