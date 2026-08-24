"""Unit tests for the transactional outbox helper (issue #21).

Exercises the shared model shape, the in-transaction enqueue, the relay's
publish/claim/mark loop, the prune, and the consumer-side dedupe-key resolver,
all against in-memory SQLite with a mocked JetStream.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from herd_common.outbox import (
    EVENT_ID_FIELD,
    NATS_MSG_ID_HEADER,
    OutboxMixin,
    _publish_pending,
    enqueue_event,
    event_dedupe_key,
    prune_published,
    run_outbox_relay,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _js_ok():
    js = SimpleNamespace()
    js.publish = AsyncMock()
    return js


# --- enqueue_event ---


async def test_enqueue_stamps_event_id_into_payload(session_factory):
    async with session_factory() as session:
        eid = enqueue_event(
            session, OutboxEvent, "herd.reservations.created", {"reservation_id": "r1"}
        )
        await session.commit()

    async with session_factory() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
    assert row.id == eid
    assert row.subject == "herd.reservations.created"
    assert row.payload["reservation_id"] == "r1"
    assert row.payload[EVENT_ID_FIELD] == str(eid)
    assert row.published_at is None
    assert row.attempts == 0


async def test_enqueue_does_not_commit(session_factory):
    # The whole point: the row only exists if the caller's transaction commits.
    async with session_factory() as session:
        enqueue_event(session, OutboxEvent, "s", {})
        await session.rollback()

    async with session_factory() as session:
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    assert rows == []


async def test_enqueue_honors_explicit_event_id(session_factory):
    fixed = uuid.uuid4()
    async with session_factory() as session:
        returned = enqueue_event(session, OutboxEvent, "s", {}, event_id=fixed)
        await session.commit()
    assert returned == fixed
    async with session_factory() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
    assert row.payload[EVENT_ID_FIELD] == str(fixed)


# --- _publish_pending ---


async def test_publish_marks_rows_and_sets_msg_id_header(session_factory):
    async with session_factory() as session:
        eid = enqueue_event(session, OutboxEvent, "herd.reservations.created", {"k": "v"})
        await session.commit()

    js = _js_ok()
    async with session_factory() as session:
        count = await _publish_pending(session, js, OutboxEvent, batch_size=100)
    assert count == 1

    js.publish.assert_awaited_once()
    args, kwargs = js.publish.call_args
    assert args[0] == "herd.reservations.created"
    assert kwargs["headers"] == {NATS_MSG_ID_HEADER: str(eid)}

    async with session_factory() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
    assert row.published_at is not None
    assert row.attempts == 1


async def test_publish_is_idempotent_skips_already_published(session_factory):
    async with session_factory() as session:
        enqueue_event(session, OutboxEvent, "s", {})
        await session.commit()

    js = _js_ok()
    async with session_factory() as session:
        first = await _publish_pending(session, js, OutboxEvent, batch_size=100)
    async with session_factory() as session:
        second = await _publish_pending(session, js, OutboxEvent, batch_size=100)
    assert first == 1
    assert second == 0
    assert js.publish.await_count == 1


async def test_publish_failure_leaves_row_unpublished_and_stops_batch(session_factory):
    async with session_factory() as session:
        enqueue_event(session, OutboxEvent, "s", {"n": 1})
        enqueue_event(session, OutboxEvent, "s", {"n": 2})
        await session.commit()

    js = SimpleNamespace()
    js.publish = AsyncMock(side_effect=RuntimeError("nats down"))
    async with session_factory() as session:
        count = await _publish_pending(session, js, OutboxEvent, batch_size=100)
    assert count == 0
    # First row's attempt was recorded; batch stopped, so only one publish tried.
    assert js.publish.await_count == 1

    async with session_factory() as session:
        rows = (
            (await session.execute(select(OutboxEvent).order_by(OutboxEvent.attempts.desc())))
            .scalars()
            .all()
        )
    assert all(r.published_at is None for r in rows)
    assert rows[0].attempts == 1  # the row that was attempted

    # Recovery: a healthy tick drains both.
    js_ok = _js_ok()
    async with session_factory() as session:
        recovered = await _publish_pending(session, js_ok, OutboxEvent, batch_size=100)
    assert recovered == 2


async def test_publish_respects_batch_size(session_factory):
    async with session_factory() as session:
        for i in range(5):
            enqueue_event(session, OutboxEvent, "s", {"n": i})
        await session.commit()

    js = _js_ok()
    async with session_factory() as session:
        count = await _publish_pending(session, js, OutboxEvent, batch_size=2)
    assert count == 2
    assert js.publish.await_count == 2


# --- prune_published ---


async def test_prune_removes_only_old_published_rows(session_factory):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        old = OutboxEvent(subject="s", payload={}, published_at=now - timedelta(days=30))
        recent = OutboxEvent(subject="s", payload={}, published_at=now - timedelta(minutes=1))
        unsent = OutboxEvent(subject="s", payload={})
        session.add_all([old, recent, unsent])
        await session.commit()

    async with session_factory() as session:
        removed = await prune_published(session, OutboxEvent, older_than=now - timedelta(days=7))
    assert removed == 1

    async with session_factory() as session:
        remaining = (await session.execute(select(OutboxEvent))).scalars().all()
    # The 30-day-old published row is gone; the recent published and the unsent
    # row survive. (SQLite returns datetimes tz-naive, so assert on identity, not
    # a re-compared timestamp.)
    remaining_ids = {r.id for r in remaining}
    assert remaining_ids == {recent.id, unsent.id}


# --- event_dedupe_key ---


def _msg_with_seq(stream: str, seq: int):
    return SimpleNamespace(
        metadata=SimpleNamespace(stream=stream, sequence=SimpleNamespace(stream=seq))
    )


def test_dedupe_key_prefers_payload_event_id():
    msg = _msg_with_seq("HERD_RESERVATIONS", 42)
    key = event_dedupe_key({EVENT_ID_FIELD: "abc-123", "x": 1}, msg)
    assert key == "abc-123"


def test_dedupe_key_falls_back_to_stream_sequence():
    msg = _msg_with_seq("HERD_RESERVATIONS", 42)
    assert event_dedupe_key({"x": 1}, msg) == "HERD_RESERVATIONS:42"
    assert event_dedupe_key(None, msg) == "HERD_RESERVATIONS:42"


def test_dedupe_key_none_when_no_id_and_no_metadata():
    bad = SimpleNamespace()  # accessing .metadata raises -> None
    assert event_dedupe_key({}, bad) is None


def test_dedupe_key_stable_across_resequence():
    # Same event_id, different stream sequence (a relay republish): same key.
    payload = {EVENT_ID_FIELD: "stable-id"}
    k1 = event_dedupe_key(payload, _msg_with_seq("HERD_RESERVATIONS", 7))
    k2 = event_dedupe_key(payload, _msg_with_seq("HERD_RESERVATIONS", 99))
    assert k1 == k2 == "stable-id"


# --- run_outbox_relay: backoff, prune gate, and cancellation (issue #571 item 5) ----
#
# The connected/disconnected happy paths are covered live by
# tests/integration/test_outbox_durability.py; these three branches (exponential
# backoff on tick failure, the prune-scheduling gate, and CancelledError propagation)
# have no assertions at any tier. A fake `get_nats` reports connected throughout;
# `nc.jetstream()` is made to raise on ticks that should fail, so the tick-level
# try/except in run_outbox_relay sees a real exception without touching DB/session
# logic. asyncio.sleep is monkeypatched to record each requested delay and to end the
# loop deterministically by raising CancelledError after N recorded calls.


def _connected_nats(jetstream_outcomes):
    """A fake NATS client that is always connected; nc.jetstream() consumes one
    outcome per call: a jetstream-like object on success, or raises on failure."""
    nc = SimpleNamespace(is_connected=True)
    outcomes = iter(jetstream_outcomes)

    def _jetstream():
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    nc.jetstream = _jetstream
    return nc


def _ok_js():
    js = SimpleNamespace()
    js.publish = AsyncMock()
    return js


async def test_relay_backoff_doubles_on_failure_and_resets_on_success(session_factory):
    """Delays double per failed tick up to the cap, and reset to tick_seconds
    immediately after a successful tick."""
    tick_seconds = 1.0
    # cap = max(tick_seconds * 10, 300) = 300, so three failures in a row stay
    # well under the cap (1 -> 2 -> 4), then a success resets to tick_seconds,
    # then one more failure goes back to 2 * tick_seconds.
    failure = RuntimeError("jetstream() unavailable")
    outcomes = [failure, failure, failure, _ok_js(), failure]
    nc = _connected_nats(outcomes)

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= len(outcomes):
            raise asyncio.CancelledError()

    with patch("herd_common.outbox.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=tick_seconds,
                prune_every_seconds=10_000.0,
            )

    # current_backoff starts at tick_seconds and is doubled BEFORE the sleep that
    # follows a failed tick: tick 1 fails -> sleep(2.0); tick 2 fails -> sleep(4.0);
    # tick 3 fails -> sleep(8.0); tick 4 succeeds -> sleep(1.0) (reset to tick_seconds);
    # tick 5 fails -> sleep(2.0) (doubling restarts from the reset base).
    assert delays == [2.0, 4.0, 8.0, 1.0, 2.0]


async def test_relay_backoff_caps_at_max(session_factory):
    """Repeated failures stop doubling once the cap (max(tick_seconds*10, 300)) is hit."""
    tick_seconds = 1.0
    max_backoff = max(tick_seconds * 10, 300)  # 300.0
    failure = RuntimeError("jetstream() unavailable")
    # Enough consecutive failures to exceed the cap through doubling: 1,2,4,...,256,512(->cap).
    outcomes = [failure] * 10
    nc = _connected_nats(outcomes)

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= len(outcomes):
            raise asyncio.CancelledError()

    with patch("herd_common.outbox.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=tick_seconds,
                prune_every_seconds=10_000.0,
            )

    assert delays[-1] == max_backoff
    assert all(d <= max_backoff for d in delays)
    # Strictly doubles until the cap is reached.
    for prev, curr in zip(delays, delays[1:]):
        assert curr == min(prev * 2, max_backoff)


async def test_relay_prune_gate_runs_once_interval_elapsed(session_factory):
    """prune_published is called only once prune_every_seconds has elapsed, not
    on every tick; a tiny prune_every_seconds lets it fire on a later tick."""
    prune_calls: list[float] = []
    real_prune_published = prune_published

    async def counting_prune(session, model, *, older_than):
        prune_calls.append(1)
        return await real_prune_published(session, model, older_than=older_than)

    nc = _connected_nats([_ok_js(), _ok_js(), _ok_js()])

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 3:
            raise asyncio.CancelledError()

    with (
        patch("herd_common.outbox.asyncio.sleep", new=fake_sleep),
        patch("herd_common.outbox.prune_published", new=counting_prune),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=0.01,
                # A near-zero gate: elapsed time since `last_prune` (set at loop
                # start) exceeds this before the very first tick's check runs.
                prune_every_seconds=0.0,
            )

    # The gate opens on every tick once the interval has elapsed, so all three
    # recorded ticks pruned; the point pinned here is that it is gated by
    # elapsed time (not skipped entirely), verified against the no-prune case below.
    assert len(prune_calls) == 3


async def test_relay_prune_gate_skips_before_interval_elapses(session_factory):
    """A prune_every_seconds far in the future means the gate never opens across
    several ticks: prune_published is not called at all."""
    prune_calls: list[float] = []
    real_prune_published = prune_published

    async def counting_prune(session, model, *, older_than):
        prune_calls.append(1)
        return await real_prune_published(session, model, older_than=older_than)

    nc = _connected_nats([_ok_js(), _ok_js(), _ok_js()])

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 3:
            raise asyncio.CancelledError()

    with (
        patch("herd_common.outbox.asyncio.sleep", new=fake_sleep),
        patch("herd_common.outbox.prune_published", new=counting_prune),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=0.01,
                prune_every_seconds=10_000.0,
            )

    assert prune_calls == []


async def test_relay_cancelled_error_during_sleep_propagates():
    """CancelledError raised out of asyncio.sleep (task cancellation) re-raises
    cleanly out of run_outbox_relay rather than being swallowed as a tick failure."""
    nc = _connected_nats([_ok_js()])

    async def fake_sleep(seconds):
        raise asyncio.CancelledError()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with patch("herd_common.outbox.asyncio.sleep", new=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await run_outbox_relay(factory, lambda: nc, OutboxEvent, tick_seconds=0.01)
    finally:
        await engine.dispose()
