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

The #1543 tests at the bottom read the row back through every route that reads a
stored suggestion, so they live here for the same shared-engine reason.
"""

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.database import AsyncSessionLocal, Base, engine, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.reservation import Reservation, ReservationStatus, TopologyType
from app.routers.reservations import bearer_scheme
from app.tasks.expiration import _run_purpose_classify_reconcile
from httpx import ASGITransport, AsyncClient
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests._harness import override_bearer
from tests._purpose_classify_answers import ANSWER_IDS, ANSWERS, deliver

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


# _insert's default request stamp, resolved against the test's NOW when _insert runs.
_FIVE_MINUTES_AGO = object()


async def _insert(
    *,
    purpose_classify_requested_at: datetime | None | object = _FIVE_MINUTES_AGO,
    purpose_classify_attempts: int = 0,
    purpose_suggestion: dict | None = None,
    status: ReservationStatus = ReservationStatus.COMPLETED,
) -> uuid.UUID:
    if purpose_classify_requested_at is _FIVE_MINUTES_AGO:
        purpose_classify_requested_at = NOW - timedelta(minutes=5)
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


async def _snapshot(rid: uuid.UUID) -> dict:
    """Every mapped column of the row, read on a fresh session."""
    async with TestSessionLocal() as db:
        res = await db.get(Reservation, rid)
        return {a.key: getattr(res, a.key) for a in Reservation.__mapper__.column_attrs}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ANSWERS, ids=ANSWER_IDS)
async def test_trigger_answers_the_outcome_word_for_every_answer(answer):
    """RES-PURPOSE-13 over every answer class in `tests/_purpose_classify_answers.py`:
    Classify now answers 503 `{"error": "purpose_classification_disabled"}` for a
    feature-off answer and 200 with exactly the reservation id, the rule's outcome
    word, and the stored suggestion (null unless one was stored) for every other. The
    row's attempt count moves by one exactly where RES-PURPOSE-7 counts an attempt,
    and a second eligible row is left as it was."""
    bystander = await _insert(purpose_classify_requested_at=NOW - timedelta(minutes=30))
    rid = await _insert()
    bystander_before = await _snapshot(bystander)
    call = AsyncMock(side_effect=lambda *a, **k: deliver(answer))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")

    if answer.outcome == "feature_off":
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": {"error": "purpose_classification_disabled"}}
    else:
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "reservation_id": str(rid),
            "outcome": answer.outcome,
            "purpose_suggestion": answer.stores,
        }
    call.assert_awaited_once()
    assert call.await_args.kwargs["json_body"]["reservation_id"] == str(rid)
    res = await _get(rid)
    assert res.purpose_suggestion == answer.stores
    assert res.purpose_classify_attempts == (1 if answer.counts_attempt else 0)
    assert await _snapshot(bystander) == bystander_before


# --- Issue #1543: every route that reads the row after an unusable 200 ------------------

UNUSABLE_200 = [a for a in ANSWERS if a.id.startswith("200-") and a.outcome == "failed"]


async def _assert_every_reader_answers(rid: uuid.UUID, *, owner_ids_listed: set[str]) -> None:
    """The routes that read a stored suggestion, on a row that holds none: the review
    list (unfiltered and filtered) answers 200 without the row, accept and dismiss 409
    `Reservation has no suggestion to accept`, and the owner's `GET /{id}` and `GET /`
    answer 200 with a null suggestion. Accept and dismiss write nothing."""
    before = await _snapshot(rid)
    async with _client_as(ADMIN_ID) as ac:
        for params in ({}, {"category": "qa_regression"}):
            listed = await ac.get("/admin/purpose-review", params=params)
            assert listed.status_code == 200, listed.text
            assert str(rid) not in {i["reservation_id"] for i in listed.json()["items"]}
        for step, body in (("accept", {"purpose_category": None}), ("dismiss", None)):
            resp = await ac.post(f"/admin/purpose-review/{rid}/{step}", json=body)
            assert resp.status_code == 409, resp.text
            assert resp.json() == {"detail": "Reservation has no suggestion to accept"}
    assert await _snapshot(rid) == before
    async with _client_as(str(USER_ID), role="user") as ac:
        one = await ac.get(f"/{rid}")
        assert one.status_code == 200, one.text
        assert one.json()["purpose_suggestion"] is None
        assert one.json()["purpose_suggested_at"] is None
        mine = await ac.get("/")
        assert mine.status_code == 200, mine.text
        assert {i["id"] for i in mine.json()["items"]} == owner_ids_listed


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", UNUSABLE_200, ids=[a.id for a in UNUSABLE_200])
async def test_every_reader_answers_after_classify_now_gets_an_unusable_200(answer):
    """#1543 through the real routes: after Classify now is given a 200 that is not a
    usable suggestion (each one in the table), it answered 200 `failed` (the table
    test), and the review list, accept, dismiss, and the owner's reads all answer
    their documented status, never 500. A second Classify now on the same row is still
    possible (no `already_suggested` 409) and stores a real suggestion."""
    rid = await _insert()
    call = AsyncMock(side_effect=lambda *a, **k: deliver(answer))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            resp = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "failed"

    await _assert_every_reader_answers(rid, owner_ids_listed={str(rid)})

    call = AsyncMock(return_value=_suggestion_response("training"))
    with patch("app.services.purpose_service.call_service", call):
        async with _client_as(ADMIN_ID) as ac:
            again = await ac.post(f"/admin/purpose-review/{rid}/classify")
    assert again.status_code == 200, again.text
    assert again.json()["outcome"] == "ok"
    assert again.json()["purpose_suggestion"]["top_category"] == "training"


_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0019_purpose_suggestion_unusable_reset.py"
)
_spec = importlib.util.spec_from_file_location("purpose_suggestion_unusable_reset", _MIGRATION_PATH)
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)

# Stored values a deployment that hit #1543 can hold, as stored JSON text, plus the
# `null` residue (no suggestion, but `purpose_suggested_at` set): SQL NULL here.
_STORED_BEFORE_THE_FIX = [
    "[1, 2]",
    '"just a string"',
    "7",
    "true",
    "null",
    "{}",
    '{"top_category": 7}',
    '{"top_category": null}',
    # Usable-shaped, but a reader refuses them (the #1543 review): an unpaired
    # surrogate (the response models), a NUL (Postgres's `->>`), and 300 levels (the
    # response models). Written as JSON escapes, so this file stays ASCII.
    '{"top_category": "training", "r": "\\ud800"}',
    '{"top_category": "training", "\\u0000": 1}',
    '{"top_category": "t", "x": ' + "[" * 299 + "]" * 299 + "}",
    None,
]


def _upgrade(sync_conn) -> None:
    with Operations.context(MigrationContext.configure(sync_conn)):
        _migration.upgrade()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", _STORED_BEFORE_THE_FIX)
async def test_a_row_stored_before_the_fix_answers_after_migration_0019(stored):
    """#1543, rows written before the fix: migration 0019 resets a stored value that is
    not a usable suggestion (and a set `purpose_suggested_at` or dismissal with no
    suggestion) to SQL NULL. After it, every reader answers as for a row that never had
    one (the review list 200 without the row, accept and dismiss 409, the owner's reads
    200 with a null suggestion), the row keeps its stamp and its attempts, and the next
    sweep tick classifies it again. A usable suggestion on another row is untouched."""
    rid = await _insert(purpose_classify_attempts=1)
    kept = await _insert(purpose_suggestion={"top_category": "qa_regression"})
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE reservations SET purpose_suggestion = :v, purpose_suggested_at = :at,"
                " purpose_suggestion_dismissed_at = :at WHERE id = :id"
            ).bindparams(bindparam("at", type_=DateTime(timezone=True))),
            {"v": stored, "at": NOW, "id": rid.hex},
        )
    kept_before = await _snapshot(kept)
    seeded = await _get(rid)
    assert seeded.purpose_suggested_at is not None
    assert seeded.purpose_suggestion_dismissed_at is not None
    requested_before = seeded.purpose_classify_requested_at

    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)

    row = await _get(rid)
    assert row.purpose_suggestion is None
    assert row.purpose_suggested_at is None
    assert row.purpose_suggestion_dismissed_at is None
    assert row.purpose_classify_requested_at == requested_before
    assert row.purpose_classify_attempts == 1
    assert await _snapshot(kept) == kept_before
    await _assert_every_reader_answers(rid, owner_ids_listed={str(rid), str(kept)})

    call = AsyncMock(return_value=_suggestion_response("training"))
    with patch("app.services.purpose_service.call_service", call):
        await _run_purpose_classify_reconcile()
    call.assert_awaited_once()
    assert call.await_args.kwargs["json_body"]["reservation_id"] == str(rid)
    assert (await _get(rid)).purpose_suggestion["top_category"] == "training"
