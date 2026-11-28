"""The end-of-reservation purpose-classification sweep reconciler (issue #646
phase 2, ADR 0013 point 8's second pass; issue #706's outcome-taxonomy fix):
app.tasks.expiration._run_purpose_classify_reconcile and its per-row helper
app.services.purpose_service.classify_purpose_one (moved there from this
module by issue #808, which added a second caller, the on-demand admin
trigger; call_service is patched at its new home,
app.services.purpose_service, not app.tasks.expiration).
"""

import inspect
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
from app.services.purpose_service import classify_purpose_one
from app.tasks.expiration import _run_purpose_classify_reconcile
from herd_common.internal_client import InternalTokenAuth
from herd_common.internal_client import call_service as real_call_service
from herd_common.upstream_body import UpstreamBodyUnusable, read_json_body
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests._purpose_classify_answers import (
    ANSWER_IDS,
    ANSWERS,
    OUTCOME_WORDS,
    SUGGESTION_BODY,
    Answer,
    ProbeUnexpectedError,
    deliver,
)

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
    topology_id: uuid.UUID | None = None,
    user_id: uuid.UUID = USER_ID,
    device_ids: list[str] | None = None,
    purpose: str = "a support case replication run",
    status: ReservationStatus = ReservationStatus.COMPLETED,
    purpose_category: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> uuid.UUID:
    res = Reservation(
        user_id=user_id,
        owner_name="owner",
        device_ids=device_ids if device_ids is not None else [str(uuid.uuid4())],
        topology_id=topology_id,
        topology_type=TopologyType.PHYSICAL,
        purpose=purpose,
        start_time=start_time if start_time is not None else NOW - timedelta(hours=3),
        end_time=end_time if end_time is not None else NOW - timedelta(hours=1),
        status=status,
        purpose_category=purpose_category,
        purpose_classify_requested_at=purpose_classify_requested_at,
        purpose_classify_attempts=purpose_classify_attempts,
        purpose_suggestion=purpose_suggestion,
        purpose_suggested_at=NOW - timedelta(days=1) if purpose_suggestion else None,
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


async def _snapshot(rid: uuid.UUID) -> dict:
    """Every mapped column of the row, read on a fresh session."""
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        return {a.key: getattr(res, a.key) for a in Reservation.__mapper__.column_attrs}


def _naive_utc(value: datetime) -> datetime:
    """SQLite hands a timezone-aware column back naive (in UTC); compare that way."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _assert_answer_applied(
    answer: Answer, before: dict, after: dict, started: datetime, finished: datetime
) -> None:
    """The row after one classification is the row before, with exactly the columns
    the answer's rule writes changed: one more attempt (RES-PURPOSE-7), or the body
    verbatim as the suggestion with its time (RES-PURPOSE-8), or nothing at all
    (RES-PURPOSE-6). `updated_at` is left out only when something was written, since
    the column's onupdate moves it then."""
    expected = dict(before)
    after = dict(after)
    if answer.counts_attempt:
        expected["purpose_classify_attempts"] = before["purpose_classify_attempts"] + 1
    if answer.stores is not None:
        expected["purpose_suggestion"] = answer.stores
        suggested_at = after["purpose_suggested_at"]
        assert suggested_at is not None
        assert _naive_utc(started) <= _naive_utc(suggested_at) <= _naive_utc(finished)
        expected["purpose_suggested_at"] = suggested_at
    if answer.counts_attempt or answer.stores is not None:
        expected.pop("updated_at")
        after.pop("updated_at")
    assert after == expected


@pytest.mark.asyncio
async def test_reconcile_stores_suggestion_on_200():
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5), dynamic=True)
    call = AsyncMock(return_value=_suggestion_response("qa_regression"))
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    sent = "Invalid internal token orchestrator-403-sentinel-5e1b"
    call = AsyncMock(return_value=httpx.Response(403, json={"detail": sent}))
    with patch("app.services.purpose_service.call_service", call), caplog.at_level(logging.DEBUG):
        await _run_purpose_classify_reconcile()
    # The orchestrator's detail is logged by its JSON type only, never its text: not in
    # any record's message or traceback, and not in any attribute (the JSON log
    # formatter emits every `extra=` key).
    for text in [caplog.text] + [str(vars(r)) for r in caplog.records]:
        assert "orchestrator-403-sentinel-5e1b" not in text
    assert "a detail of type str" in caplog.text

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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
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
    with patch("app.services.purpose_service.call_service", call):
        for _ in range(3):
            await _run_purpose_classify_reconcile()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_timeout_bumps_attempts_and_does_not_end_tick():
    """2026-09-05 amendment (issue #706 follow-up): a timeout is per-row
    evidence, not provider-wide evidence, unlike the other transient
    outcomes. The first (oldest-requested) row's call times out and must
    have its attempts bumped, but the reconciler keeps going: the second row
    is still classified in the SAME tick.
    """
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(side_effect=[httpx.TimeoutException("timed out"), _suggestion_response()])
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()

    assert call.await_count == 2
    first = await _get(rid_first)
    assert first.purpose_suggestion is None
    assert first.purpose_classify_attempts == 1
    second = await _get(rid_second)
    assert second.purpose_suggestion is not None
    assert second.purpose_classify_attempts == 0


@pytest.mark.asyncio
async def test_reconcile_timeout_head_of_line_row_stops_blocking_after_cap():
    """The literal defect this fix closes: before it, a row whose call always
    timed out was classed "transient" and ended every tick at the head of the
    queue, so it blocked every row behind it forever (observed: the same
    reservation timed out on seven consecutive ticks with attempts still 0
    while 45 eligible rows waited behind it). Now each timeout bumps that
    row's own attempts, so after purpose_classify_max_attempts ticks the row
    falls out of the eligible set (attempts < max_attempts) and the next tick
    skips it without calling the orchestrator again.
    """
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    with patch("app.services.purpose_service.call_service", call):
        for _ in range(settings.purpose_classify_max_attempts + 1):
            await _run_purpose_classify_reconcile()

    assert call.await_count == settings.purpose_classify_max_attempts
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == settings.purpose_classify_max_attempts


@pytest.mark.asyncio
async def test_reconcile_transport_error_ends_tick_without_bump():
    """A connection error is transient (RES-PURPOSE-6): the single-row classifier
    returns "transient" without raising, and the sweep makes one call, ends the tick
    there, and counts no attempt on either of the two eligible rows."""
    rid_first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    rid_second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    before = {rid: await _snapshot(rid) for rid in (rid_first, rid_second)}
    call = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_awaited_once()
    assert call.await_args.kwargs["json_body"]["reservation_id"] == str(rid_first)
    for rid in (rid_first, rid_second):
        assert await _snapshot(rid) == before[rid]

    call = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("app.services.purpose_service.call_service", call):
        assert await classify_purpose_one(rid_second) == "transient"
    call.assert_awaited_once()
    for rid in (rid_first, rid_second):
        assert await _snapshot(rid) == before[rid]


@pytest.mark.asyncio
async def test_reconcile_skips_rows_at_attempt_cap():
    rid = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_classify_attempts=settings.purpose_classify_max_attempts,
    )
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()
    res = await _get(rid)
    assert res.purpose_suggestion is None
    assert res.purpose_classify_attempts == settings.purpose_classify_max_attempts


@pytest.mark.asyncio
async def test_reconcile_ignores_rows_not_yet_requested():
    await _insert(purpose_classify_requested_at=None)
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_ignores_rows_already_suggested():
    await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_suggestion={"top_category": "training"},
    )
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_respects_batch_size(monkeypatch):
    monkeypatch.setattr(settings, "purpose_classify_batch_size", 2)
    # Inserted NEWEST first (issue #1136): an unordered SELECT returns insertion
    # order, so only the ORDER BY on purpose_classify_requested_at can pick the
    # two oldest rows. rids[i] was requested (10 - i) minutes ago.
    by_index = {}
    for i in reversed(range(3)):
        by_index[i] = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10 - i))
    rids = [by_index[i] for i in range(3)]
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.services.purpose_service.call_service", call):
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
    # Inserted newer first (issue #1136): an unordered SELECT returns insertion
    # order, so only the ORDER BY can put the older request first.
    newer = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=1))
    older = await _insert(purpose_classify_requested_at=NOW - timedelta(hours=2))
    seen_order: list[uuid.UUID] = []

    async def _fake_call(*args, **kwargs):
        seen_order.append(uuid.UUID(kwargs["json_body"]["reservation_id"]))
        return _suggestion_response()

    with patch("app.services.purpose_service.call_service", AsyncMock(side_effect=_fake_call)):
        await _run_purpose_classify_reconcile()
    assert seen_order == [older, newer]


# --- Issue #1525: row scope, payload, and the outcome of every answer -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", ["suggested", "at_attempt_cap", "not_requested"])
async def test_reconcile_batch_is_filled_only_by_eligible_rows(monkeypatch, ineligible):
    """RES-PURPOSE-5: a tick takes only stamped rows with no suggestion and fewer than
    the maximum attempts. Two rows that fail one of those three conditions, and only
    that one, sort ahead of the one eligible row (an older stamp, or no stamp, which
    SQLite sorts first), and the batch has two slots: the tick still makes exactly one
    call, for the eligible row, and leaves both other rows as they were."""
    monkeypatch.setattr(settings, "purpose_classify_batch_size", 2)
    ineligible_kwargs = {
        "suggested": {"purpose_suggestion": {"top_category": "training"}},
        "at_attempt_cap": {"purpose_classify_attempts": settings.purpose_classify_max_attempts},
        "not_requested": {"purpose_classify_requested_at": None},
    }[ineligible]
    others = []
    for minutes in (30, 20):
        kwargs = {"purpose_classify_requested_at": NOW - timedelta(minutes=minutes)}
        kwargs.update(ineligible_kwargs)
        others.append(await _insert(**kwargs))
    eligible = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=1))
    before = {rid: await _snapshot(rid) for rid in others}

    sent: list[str] = []

    async def _fake_call(*args, **kwargs):
        sent.append(kwargs["json_body"]["reservation_id"])
        return _suggestion_response("qa_regression")

    with patch("app.services.purpose_service.call_service", AsyncMock(side_effect=_fake_call)):
        await _run_purpose_classify_reconcile()

    assert sent == [str(eligible)]
    assert (await _get(eligible)).purpose_suggestion["top_category"] == "qa_regression"
    for rid in others:
        assert await _snapshot(rid) == before[rid]


@pytest.mark.asyncio
async def test_reconcile_sends_each_rows_own_payload(monkeypatch):
    """Each row's call is the request section 10's dependency table names, `POST
    /internal/classify-purpose` to ai-orchestrator with the internal token and
    `PURPOSE_CLASSIFY_TIMEOUT_SECONDS`, and its body carries exactly the keys of the
    orchestrator's `InternalClassifyRequest` with that row's own values. No rule states
    the body, so the body is pinned as built. A row with a topology sends its id as a
    string; a row without one sends null, not "None". The call is compared by
    `call_service`'s own signature, so passing an argument by name instead of by
    position still passes. The three settings are set to probe values, so a constant
    or another setting in their place shows."""
    monkeypatch.setattr(settings, "ai_orchestrator_service_url", "http://orchestrator.probe:1")
    monkeypatch.setattr(settings, "internal_api_token", "probe-token-1525")
    monkeypatch.setattr(settings, "purpose_classify_timeout_seconds", 17.5)
    topology_id = uuid.uuid4()
    other_user = uuid.uuid4()
    with_topology_devices = [str(uuid.uuid4()), str(uuid.uuid4())]
    without_topology_devices = [str(uuid.uuid4())]
    with_topology = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=10),
        topology_id=topology_id,
        device_ids=with_topology_devices,
        purpose="topology run",
        status=ReservationStatus.CANCELLED,
        start_time=NOW - timedelta(hours=5),
        end_time=NOW - timedelta(hours=4),
    )
    without_topology = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        user_id=other_user,
        device_ids=without_topology_devices,
        purpose="bare devices",
        status=ReservationStatus.FAILED,
        dynamic=True,
        start_time=NOW - timedelta(hours=3),
        end_time=NOW - timedelta(hours=2),
    )
    template_id = (await _get(without_topology)).dynamic_requests[0].template_id

    sent: dict[str, dict] = {}

    calls: list[dict] = []

    async def _fake_call(*args, **kwargs):
        bound = inspect.signature(real_call_service).bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        body = arguments.pop("json_body")
        calls.append(arguments)
        sent[body["reservation_id"]] = body
        return _suggestion_response()

    with patch("app.services.purpose_service.call_service", AsyncMock(side_effect=_fake_call)):
        await _run_purpose_classify_reconcile()

    assert set(sent) == {str(with_topology), str(without_topology)}
    expected_call = {
        "base_url": "http://orchestrator.probe:1",
        "method": "POST",
        "path": "/internal/classify-purpose",
        "params": None,
        "timeout": 17.5,
        "auth": InternalTokenAuth(token="probe-token-1525"),
    }
    assert calls == [expected_call, expected_call]
    expected = {
        str(with_topology): {
            "reservation_id": str(with_topology),
            "categories": list(settings.purpose_categories),
            "purpose": "topology run",
            "user_id": str(USER_ID),
            "device_ids": with_topology_devices,
            "topology_id": str(topology_id),
            "dynamic_requests": None,
            "status": "CANCELLED",
        },
        str(without_topology): {
            "reservation_id": str(without_topology),
            "categories": list(settings.purpose_categories),
            "purpose": "bare devices",
            "user_id": str(other_user),
            "device_ids": without_topology_devices,
            "topology_id": None,
            "dynamic_requests": [{"template_id": str(template_id), "count": 1}],
            "status": "FAILED",
        },
    }
    windows = {
        str(with_topology): (NOW - timedelta(hours=5), NOW - timedelta(hours=4)),
        str(without_topology): (NOW - timedelta(hours=3), NOW - timedelta(hours=2)),
    }
    for rid, body in sent.items():
        body = dict(body)
        start, end = body.pop("start_time"), body.pop("end_time")
        # The model keeps a device set, not its order (Reservation.device_ids).
        devices = body.pop("device_ids")
        assert sorted(devices) == sorted(expected[rid].pop("device_ids"))
        assert len(devices) == len(set(devices))
        assert body == expected[rid]
        assert _naive_utc(datetime.fromisoformat(start)) == _naive_utc(windows[rid][0])
        assert _naive_utc(datetime.fromisoformat(end)) == _naive_utc(windows[rid][1])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ANSWERS, ids=ANSWER_IDS)
async def test_classify_one_maps_each_answer_to_its_outcome(answer):
    """RES-PURPOSE-6, RES-PURPOSE-7, RES-PURPOSE-8: for every answer class in
    `tests/_purpose_classify_answers.py`, the single-row classifier returns the
    rule's outcome word without raising, makes one call for the named row, and writes
    exactly what the rule says on that row: the body as the suggestion with a time
    taken during the call, or one more attempt, or nothing (`updated_at` may move only
    when something is written). A second eligible row, with an older stamp, keeps
    every mapped column. The named row starts with a category of its own, which a
    stored suggestion must not change."""
    bystander = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=30), user_id=uuid.uuid4()
    )
    rid = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_classify_attempts=1,
        purpose_category="training",
    )
    before, bystander_before = await _snapshot(rid), await _snapshot(bystander)
    call = AsyncMock(side_effect=lambda *a, **k: deliver(answer))

    started = datetime.now(timezone.utc)
    with patch("app.services.purpose_service.call_service", call):
        outcome = await classify_purpose_one(rid)
    finished = datetime.now(timezone.utc)

    assert outcome == answer.outcome
    call.assert_awaited_once()
    assert call.await_args.kwargs["json_body"]["reservation_id"] == str(rid)
    _assert_answer_applied(answer, before, await _snapshot(rid), started, finished)
    assert await _snapshot(bystander) == bystander_before


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ANSWERS, ids=ANSWER_IDS)
async def test_reconcile_ends_or_continues_the_tick_by_answer(answer):
    """RES-PURPOSE-6 and RES-PURPOSE-7 at the sweep: two eligible rows, the older one
    answered with the case's answer and the newer one with a suggestion. An answer
    that ends the tick leaves one call made and both rows as they were; any other
    answer is applied to the older row only, and the newer row is classified in the
    same tick."""
    first = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=10))
    second = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    first_before, second_before = await _snapshot(first), await _snapshot(second)

    sent: list[str] = []

    async def _fake_call(*args, **kwargs):
        rid = kwargs["json_body"]["reservation_id"]
        sent.append(rid)
        if rid == str(first):
            return deliver(answer)
        return httpx.Response(200, json=SUGGESTION_BODY)

    started = datetime.now(timezone.utc)
    with patch("app.services.purpose_service.call_service", AsyncMock(side_effect=_fake_call)):
        await _run_purpose_classify_reconcile()
    finished = datetime.now(timezone.utc)

    if answer.ends_tick:
        assert sent == [str(first)]
        assert await _snapshot(first) == first_before
        assert await _snapshot(second) == second_before
        return
    assert sent == [str(first), str(second)]
    _assert_answer_applied(answer, first_before, await _snapshot(first), started, finished)
    stored = next(a for a in ANSWERS if a.stores is not None)
    _assert_answer_applied(stored, second_before, await _snapshot(second), started, finished)


@pytest.mark.asyncio
async def test_classify_one_never_raises_on_an_unexpected_exception():
    """The classifier never raises (section 10's dependency table): an exception from
    the call that is not an httpx error at all (call_service raises RuntimeError for a
    missing token) still comes back as an outcome word, and neither the named row nor
    a second eligible row gets a suggestion."""
    bystander = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=30))
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    bystander_before = await _snapshot(bystander)
    call = AsyncMock(side_effect=ProbeUnexpectedError("probe"))
    with patch("app.services.purpose_service.call_service", call):
        outcome = await classify_purpose_one(rid)
    assert outcome in OUTCOME_WORDS
    call.assert_awaited_once()
    assert (await _get(rid)).purpose_suggestion is None
    assert await _snapshot(bystander) == bystander_before


@pytest.mark.asyncio
async def test_classify_one_leaves_an_already_suggested_row_alone():
    """A row that already holds a suggestion when the classifier reads it (one that
    landed after the sweep's SELECT) is "ok" with no call: the stored suggestion and
    its time stay as they were, and so does every other column of it and of a second,
    eligible row."""
    bystander = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=30))
    rid = await _insert(
        purpose_classify_requested_at=NOW - timedelta(minutes=5),
        purpose_suggestion={"top_category": "training"},
    )
    before, bystander_before = await _snapshot(rid), await _snapshot(bystander)
    assert before["purpose_suggested_at"] is not None
    call = AsyncMock(return_value=_suggestion_response("qa_regression"))
    with patch("app.services.purpose_service.call_service", call):
        assert await classify_purpose_one(rid) == "ok"
    call.assert_not_awaited()
    assert await _snapshot(rid) == before
    assert await _snapshot(bystander) == bystander_before


@pytest.mark.asyncio
async def test_classify_one_on_an_unknown_id_is_ok_without_a_call():
    """A row deleted after the sweep's SELECT is "ok" with no call and no raise, and
    an eligible row that is present is left as it was."""
    bystander = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=30))
    bystander_before = await _snapshot(bystander)
    call = AsyncMock(return_value=_suggestion_response())
    with patch("app.services.purpose_service.call_service", call):
        assert await classify_purpose_one(uuid.uuid4()) == "ok"
    call.assert_not_awaited()
    assert await _snapshot(bystander) == bystander_before


# --- Issue #1543 and the classify part of #1577: a 200 that is not a usable suggestion --

UNUSABLE_200 = [a for a in ANSWERS if a.id.startswith("200-") and a.outcome == "failed"]
UNUSABLE_200_IDS = [a.id for a in UNUSABLE_200]


def _expected_reason(answer: Answer) -> str:
    """The fixed reason code each unusable 200 is logged with: nested too deep (for the
    decoder, or past the store bound), not JSON (it does not parse, holds a number that
    is not finite, or holds an unpaired surrogate in a string or key, which the shared
    read refuses before the shape check, #1577), bad text (a NUL in a string or key), or
    the wrong shape (anything else)."""
    if answer.id.startswith("200-nul-in-"):
        return "bad_text"
    if "deep" in answer.id or "store-bound" in answer.id:
        return "too_deep"
    not_json = (
        "not-json",
        "empty-body",
        "html",
        "invalid-utf8",
        "nan",
        "infinity",
        "overflow",
        "high-surrogate",
        "low-surrogate",
    )
    if any(word in answer.id for word in not_json):
        return "not_json"
    return "wrong_shape"


def test_the_unusable_200_table_covers_every_json_type_and_reason():
    """The table holds a 200 of every JSON type that is not an object, an object of
    every reason, and a body that parses to each of them, so a row dropped from it
    shows here before it silently stops being tested."""
    ids = set(UNUSABLE_200_IDS)
    for kind in ("list", "string", "integer", "float", "true", "false", "null"):
        assert f"200-json-{kind}" in ids
    for kind in ("null", "integer", "float", "true", "list", "object"):
        assert f"200-top-category-{kind}" in ids
    assert {"200-deep-array", "200-deep-object", "200-no-top-category"} <= ids
    for text in ("nul", "high-surrogate", "low-surrogate"):
        for where in ("value", "nested-value", "key", "nested-key", "top-category"):
            assert f"200-{text}-in-{where}" in ids
    assert {_expected_reason(a) for a in UNUSABLE_200} == {
        "not_json",
        "too_deep",
        "wrong_shape",
        "bad_text",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", UNUSABLE_200, ids=UNUSABLE_200_IDS)
async def test_reconcile_unusable_200_reaches_the_attempt_cap_without_stalling(answer):
    """RES-PURPOSE-5 and RES-PURPOSE-7 for every 200 that is not a usable suggestion,
    on three eligible rows that all get it. Each tick calls every row, in stamp order,
    and counts one attempt on each, so no row stalls the ones behind it (#1577: a body
    nested past the decoder's limit used to end the tick after the first row with no
    attempt counted, every tick). After PURPOSE_CLASSIFY_MAX_ATTEMPTS ticks the rows
    are at the cap and the next tick makes no call (#1543: a JSON `null` used to count
    no attempt and was sent again every tick with no cap). Nothing is stored."""
    rids = [
        await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=minutes))
        for minutes in (30, 20, 10)
    ]
    sent: list[str] = []

    async def _fake_call(*args, **kwargs):
        sent.append(kwargs["json_body"]["reservation_id"])
        return deliver(answer)

    with patch("app.services.purpose_service.call_service", AsyncMock(side_effect=_fake_call)):
        for tick in range(1, settings.purpose_classify_max_attempts + 1):
            sent.clear()
            await _run_purpose_classify_reconcile()
            assert sent == [str(rid) for rid in rids], tick
            for rid in rids:
                row = await _get(rid)
                assert row.purpose_classify_attempts == tick
                assert row.purpose_suggestion is None
                assert row.purpose_suggested_at is None
        sent.clear()
        await _run_purpose_classify_reconcile()
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", UNUSABLE_200, ids=UNUSABLE_200_IDS)
async def test_classify_one_logs_an_unusable_200_with_fixed_fields_only(answer, caplog):
    """Nothing from the body reaches a log line: the classifier logs one WARNING with
    the fixed action `purpose_classify_bad_body`, a fixed message naming only the
    reservation id, the fixed reason code, and the parsed top-level type's name (none
    when the body does not parse), with no exception attached. It logs nothing else at
    WARNING or above."""
    rid = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=5))
    call = AsyncMock(side_effect=lambda *a, **k: deliver(answer))
    with (
        patch("app.services.purpose_service.call_service", call),
        caplog.at_level(logging.DEBUG, logger="app"),
    ):
        assert await classify_purpose_one(rid) == "failed"

    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1
    record = loud[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"Purpose classify reconcile: unusable 200 body for {rid}"
    assert record.action == "purpose_classify_bad_body"
    assert record.reservation_id == str(rid)
    assert record.reason == _expected_reason(answer)
    # The parsed top-level type, or None when the shared read refuses the body.
    try:
        expected_type = type(read_json_body(deliver(answer), expect=object)).__name__
    except UpstreamBodyUnusable:
        expected_type = None
    assert record.body_type == expected_type
    assert record.exc_info is None
    assert record.exc_text is None
