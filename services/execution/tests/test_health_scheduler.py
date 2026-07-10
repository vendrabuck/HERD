"""Unit tests for app.services.health_scheduler.

Same shape as inventory.test_apply_scheduler: in-memory sqlite,
fake httpx client driven by a route table, deterministic per-poll
pipeline tests for fire_poll and the supporting helpers. The full
loop is integration-shaped and exercised separately.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from app.config import settings
from app.database import Base
from app.models.device_health_status import DeviceHealthStatus
from app.models.execution_run import ExecutionRun
from app.models.outbox import OutboxEvent
from app.services import health_scheduler
from app.services.health_scheduler import (
    SYSTEM_POLL_USER_ID,
    _claim_row,
    _due_rows,
    _next_poll_at,
    fire_poll,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    health_scheduler._registry.clear()
    health_scheduler._registry_last_refresh = None
    health_scheduler._template_cache.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_health_row(
    db,
    *,
    next_poll_at: datetime,
    consecutive_failures: int = 0,
    last_status: str = "UNKNOWN",
) -> DeviceHealthStatus:
    row = DeviceHealthStatus(
        device_id=uuid.uuid4(),
        next_poll_at=next_poll_at,
        consecutive_failures=consecutive_failures,
        last_status=last_status,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# --- _due_rows ---


@pytest.mark.asyncio
async def test_due_rows_returns_only_rows_in_the_past():
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        due = await _seed_health_row(db, next_poll_at=now - timedelta(seconds=10))
        await _seed_health_row(db, next_poll_at=now + timedelta(minutes=5))

        rows = await _due_rows(db, now)
        assert len(rows) == 1
        assert rows[0].device_id == due.device_id


# --- _claim_row ---


@pytest.mark.asyncio
async def test_claim_row_wins_when_still_due():
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        row = await _seed_health_row(db, next_poll_at=now - timedelta(seconds=5))
        won = await _claim_row(db, row.device_id, now)
        assert won is True
        await db.refresh(row)
        # next_poll_at pushed forward by the claim window. SQLite drops
        # tzinfo on read; tag UTC for the comparison.
        nxt = row.next_poll_at
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        assert nxt > now + timedelta(minutes=4)


@pytest.mark.asyncio
async def test_claim_row_loses_when_already_claimed():
    """Two concurrent ticks: only the first claim succeeds."""
    async with TestSessionLocal() as db:
        now = datetime.now(timezone.utc)
        row = await _seed_health_row(db, next_poll_at=now - timedelta(seconds=5))

        first = await _claim_row(db, row.device_id, now)
        # Second claim with the same `now` should see next_poll_at already
        # pushed past `now` and fail the conditional update.
        second = await _claim_row(db, row.device_id, now)

        assert first is True
        assert second is False


# --- _next_poll_at math ---


def test_next_poll_at_uses_base_interval_within_threshold():
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    nxt = _next_poll_at(60, consecutive_failures=2, now=now)
    # Threshold is 3 by default; 2 failures is within, so just now + 60s.
    assert nxt == now + timedelta(seconds=60)


def test_next_poll_at_uses_base_interval_at_exactly_threshold():
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    threshold = settings.health_poll_max_consecutive_failures
    nxt = _next_poll_at(60, consecutive_failures=threshold, now=now)
    # At-threshold: still base interval (overflow == 0 triggers backoff
    # path otherwise). Comment in code says "<= threshold" stays base.
    assert nxt == now + timedelta(seconds=60)


def test_next_poll_at_backoff_kicks_in_past_threshold():
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    threshold = settings.health_poll_max_consecutive_failures
    nxt = _next_poll_at(60, consecutive_failures=threshold + 1, now=now)
    # overflow=1, raw=60*2=120, jitter up to 30s -> [120, 150).
    diff = (nxt - now).total_seconds()
    assert 120 <= diff < 150 + 0.001


def test_next_poll_at_backoff_capped():
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    threshold = settings.health_poll_max_consecutive_failures
    # 20 failures past threshold: 60 * 2**20 explodes; cap should pin.
    nxt = _next_poll_at(60, consecutive_failures=threshold + 20, now=now)
    diff = (nxt - now).total_seconds()
    cap = settings.health_poll_backoff_cap_seconds
    assert cap <= diff < cap + 30 + 0.001


# --- fire_poll outcomes ---


def _device_data():
    return {
        "id": str(uuid.uuid4()),
        "template_id": str(uuid.uuid4()),
        "driver_id": str(uuid.uuid4()),
        "driver_sha256": "abc",
        "driver_filename": "d.zip",
        "connection_type": "Management",
        "field_data": {},
        "name": "dev",
    }


def _template_data():
    return {"sections": []}


async def _existing_row(
    device_id: uuid.UUID,
    now: datetime,
    *,
    consecutive_failures: int = 0,
    last_status: str = "UNKNOWN",
) -> DeviceHealthStatus:
    async with TestSessionLocal() as db:
        row = DeviceHealthStatus(
            device_id=device_id,
            next_poll_at=now,
            consecutive_failures=consecutive_failures,
            last_status=last_status,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


def _make_run(status: str = "SUCCESS") -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        driver_id=uuid.uuid4(),
        driver_sha256="abc",
        action="status",
        status=status,
        user_id=SYSTEM_POLL_USER_ID,
        input_params={},
    )


@pytest.mark.asyncio
async def test_fire_poll_login_failure_marks_unreachable(monkeypatch):
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    now = datetime.now(timezone.utc)
    await _existing_row(device_id, now)

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(return_value=_make_run(status="FAILED")),
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.last_status == "UNREACHABLE"
        assert row.consecutive_failures == 1
        assert row.last_polled_at is not None


@pytest.mark.asyncio
async def test_fire_poll_status_success_marks_healthy(monkeypatch):
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    now = datetime.now(timezone.utc)
    await _existing_row(device_id, now)

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )

    # All three actions succeed.
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(side_effect=[_make_run(), _make_run(), _make_run()]),
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.last_status == "HEALTHY"
        assert row.consecutive_failures == 0


@pytest.mark.asyncio
async def test_fire_poll_status_non_success_marks_degraded(monkeypatch):
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    now = datetime.now(timezone.utc)
    await _existing_row(device_id, now)

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )

    # login SUCCESS, status FAILED, logout SUCCESS.
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(side_effect=[_make_run(), _make_run(status="FAILED"), _make_run()]),
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.last_status == "DEGRADED"
        assert row.consecutive_failures == 1


@pytest.mark.asyncio
async def test_fire_poll_resets_failures_on_recovery(monkeypatch):
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    now = datetime.now(timezone.utc)
    # Seed with prior failures.
    async with TestSessionLocal() as db:
        db.add(
            DeviceHealthStatus(
                device_id=device_id,
                next_poll_at=now,
                consecutive_failures=5,
                last_status="UNREACHABLE",
            )
        )
        await db.commit()

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(side_effect=[_make_run(), _make_run(), _make_run()]),
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.last_status == "HEALTHY"
        assert row.consecutive_failures == 0


@pytest.mark.asyncio
async def test_fire_poll_logout_failure_does_not_mask_outcome(monkeypatch):
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    now = datetime.now(timezone.utc)
    await _existing_row(device_id, now)

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )

    async def _action(_db, _d, _t, action, _u):
        if action == "logout":
            raise RuntimeError("logout broke")
        return _make_run()

    monkeypatch.setattr(health_scheduler, "run_driver_action", _action)

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        # login + status both succeeded; logout exception did not flip outcome.
        assert row.last_status == "HEALTHY"
        assert row.consecutive_failures == 0


@pytest.mark.asyncio
async def test_fire_poll_fetch_device_failure_marks_unreachable(monkeypatch):
    device_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await _existing_row(device_id, now)

    async def _boom(_id):
        raise RuntimeError("inventory down")

    monkeypatch.setattr(health_scheduler, "fetch_device", _boom)

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.last_status == "UNREACHABLE"
        assert row.consecutive_failures == 1


@pytest.mark.asyncio
async def test_fire_poll_creates_row_if_missing(monkeypatch):
    """Race: registry-refresh missed this device but the loop picked it.

    fire_poll should create the row rather than crash on the get(None).
    """
    device = _device_data()
    device_id = uuid.UUID(device["id"])
    # No pre-existing health-status row.

    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(side_effect=[_make_run(), _make_run(), _make_run()]),
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row is not None
        assert row.last_status == "HEALTHY"


# --- ROADMAP #13 iter 2 + issue #21: transition enqueued to the outbox ---
#
# The publish path is gone: fire_poll now stages the health-transition event
# into the outbox table in the SAME transaction as the status update, and a
# separate relay (main.py) publishes it. These tests assert against the staged
# OutboxEvent rows rather than a direct js.publish.


def _setup_driver_outcomes(monkeypatch, *, login_status: str, status_status: str | None):
    """Wire fetch_device/template + the three run_driver_action calls."""
    device = _device_data()
    monkeypatch.setattr(health_scheduler, "fetch_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        health_scheduler, "fetch_template", AsyncMock(return_value=_template_data())
    )
    runs = [_make_run(status=login_status)]
    if status_status is not None:
        # status run + logout run
        runs.extend([_make_run(status=status_status), _make_run()])
    monkeypatch.setattr(
        health_scheduler,
        "run_driver_action",
        AsyncMock(side_effect=runs),
    )
    return uuid.UUID(device["id"])


async def _outbox_rows() -> list[OutboxEvent]:
    async with TestSessionLocal() as db:
        result = await db.execute(select(OutboxEvent))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_enqueue_on_threshold_crossing(monkeypatch):
    """2 to 3 failures (threshold=3): exactly one bad_news OutboxEvent, unpublished."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=2, last_status="HEALTHY")

    await fire_poll(TestSessionLocal, device_id, 60)

    rows = await _outbox_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.subject == health_scheduler.HEALTH_NATS_SUBJECT
    # Staged but not yet relayed: the relay sets published_at on publish.
    assert row.published_at is None
    payload = row.payload
    # enqueue_event stamps a stable event_id that the consumer dedupes on.
    assert payload["event_id"] == str(row.id)
    assert uuid.UUID(payload["event_id"])  # well-formed UUID
    assert payload["event"] == "device.health_transition"
    assert payload["transition_kind"] == "bad_news"
    assert payload["new_status"] == "UNREACHABLE"
    assert payload["old_status"] == "HEALTHY"
    assert payload["consecutive_failures"] == 3


@pytest.mark.asyncio
async def test_enqueue_shares_transaction_with_status_update(monkeypatch):
    """The enqueue runs in the same session/transaction as the status update.

    Proven by inspecting the session at enqueue time: the modified
    DeviceHealthStatus row is already pending (dirty, uncommitted) in the very
    session enqueue_event is handed, so a single commit persists both rows.
    """
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=2, last_status="HEALTHY")

    captured: dict = {}
    real_enqueue = health_scheduler.enqueue_event

    def _spy(session, model, subject, payload, **kwargs):
        dirty = [o for o in session.dirty if isinstance(o, DeviceHealthStatus)]
        captured["dirty_failures"] = [o.consecutive_failures for o in dirty]
        return real_enqueue(session, model, subject, payload, **kwargs)

    monkeypatch.setattr(health_scheduler, "enqueue_event", _spy)
    await fire_poll(TestSessionLocal, device_id, 60)

    # The status update (consecutive_failures -> 3) was pending-uncommitted in
    # the same session at the moment the event was staged.
    assert captured["dirty_failures"] == [3]
    rows = await _outbox_rows()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_no_enqueue_below_threshold(monkeypatch):
    """1 to 2 failures, threshold=3: no transition, no outbox row."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=1, last_status="UNREACHABLE")

    await fire_poll(TestSessionLocal, device_id, 60)

    assert await _outbox_rows() == []


@pytest.mark.asyncio
async def test_no_re_enqueue_on_continued_failure(monkeypatch):
    """Already past threshold (3 to 4): no second bad_news row."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=3, last_status="UNREACHABLE")

    await fire_poll(TestSessionLocal, device_id, 60)

    assert await _outbox_rows() == []


@pytest.mark.asyncio
async def test_enqueue_on_recovery(monkeypatch):
    """consecutive_failures resets from 5 to 0: one recovery OutboxEvent."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="SUCCESS", status_status="SUCCESS")
    await _existing_row(device_id, now, consecutive_failures=5, last_status="UNREACHABLE")

    await fire_poll(TestSessionLocal, device_id, 60)

    rows = await _outbox_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["transition_kind"] == "recovery"
    assert payload["old_status"] == "UNREACHABLE"
    assert payload["new_status"] == "HEALTHY"
    assert payload["consecutive_failures"] == 0
    assert payload["event_id"] == str(rows[0].id)


@pytest.mark.asyncio
async def test_no_enqueue_healthy_to_healthy(monkeypatch):
    """A device that has always been healthy does not generate noise."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="SUCCESS", status_status="SUCCESS")
    await _existing_row(device_id, now, consecutive_failures=0, last_status="HEALTHY")

    await fire_poll(TestSessionLocal, device_id, 60)

    assert await _outbox_rows() == []


@pytest.mark.asyncio
async def test_status_update_persists_alongside_enqueue(monkeypatch):
    """The status row commit and the outbox row land together."""
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=2, last_status="HEALTHY")

    await fire_poll(TestSessionLocal, device_id, 60)

    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.consecutive_failures == 3
        assert row.last_status == "UNREACHABLE"
    assert len(await _outbox_rows()) == 1


@pytest.mark.asyncio
async def test_notify_disabled_knob_suppresses_enqueue(monkeypatch):
    """`HEALTH_POLL_NOTIFY_ENABLED=false` suppresses the enqueue on a crossing.

    The status update still commits; only the event staging is gated off.
    """
    now = datetime.now(timezone.utc)
    device_id = _setup_driver_outcomes(monkeypatch, login_status="FAILED", status_status=None)
    await _existing_row(device_id, now, consecutive_failures=2, last_status="HEALTHY")

    monkeypatch.setattr(
        health_scheduler.settings,
        "health_poll_notify_enabled",
        False,
    )

    await fire_poll(TestSessionLocal, device_id, 60)

    assert await _outbox_rows() == []
    async with TestSessionLocal() as db:
        row = await db.get(DeviceHealthStatus, device_id)
        assert row.consecutive_failures == 3


# --- _decide_transition unit tests ---


def test_decide_transition_bad_news_at_threshold():
    assert (
        health_scheduler._decide_transition(old_failures=2, new_failures=3, threshold=3)
        == "bad_news"
    )


def test_decide_transition_bad_news_crossing_above_threshold():
    """First poll past the threshold also fires (e.g. 2 to 4 via a big jump)."""
    assert (
        health_scheduler._decide_transition(old_failures=2, new_failures=5, threshold=3)
        == "bad_news"
    )


def test_decide_transition_no_re_fire_above_threshold():
    """3 to 4: silent. The threshold-crossing has already fired."""
    assert health_scheduler._decide_transition(old_failures=3, new_failures=4, threshold=3) is None


def test_decide_transition_recovery():
    assert (
        health_scheduler._decide_transition(old_failures=5, new_failures=0, threshold=3)
        == "recovery"
    )


def test_decide_transition_first_failure_silent():
    assert health_scheduler._decide_transition(old_failures=0, new_failures=1, threshold=3) is None


def test_decide_transition_healthy_to_healthy():
    assert health_scheduler._decide_transition(old_failures=0, new_failures=0, threshold=3) is None


def test_decide_transition_threshold_lowered_at_runtime():
    """If threshold lowers past a device already over it, no re-fire."""
    # Old failures (4) and new failures (5) both >= new threshold (3): no bad_news.
    assert health_scheduler._decide_transition(old_failures=4, new_failures=5, threshold=3) is None
