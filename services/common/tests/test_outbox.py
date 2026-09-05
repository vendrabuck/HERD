"""Unit tests for the transactional outbox helper (issue #21).

Exercises the shared model shape, the in-transaction enqueue, the relay's
publish/claim/mark loop, the prune, and the consumer-side dedupe-key resolver,
all against in-memory SQLite with a mocked JetStream.
"""

import asyncio
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from herd_common.outbox import (
    EVENT_ID_FIELD,
    NATS_MSG_ID_HEADER,
    OutboxMixin,
    _listen_for_wakeups,
    _publish_pending,
    enqueue_event,
    event_dedupe_key,
    outbox_channel,
    prune_published,
    run_outbox_relay,
)
from sqlalchemy import make_url, select
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
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
        eid = await enqueue_event(
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
        await enqueue_event(session, OutboxEvent, "s", {})
        await session.rollback()

    async with session_factory() as session:
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    assert rows == []


async def test_enqueue_honors_explicit_event_id(session_factory):
    fixed = uuid.uuid4()
    async with session_factory() as session:
        returned = await enqueue_event(session, OutboxEvent, "s", {}, event_id=fixed)
        await session.commit()
    assert returned == fixed
    async with session_factory() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
    assert row.payload[EVENT_ID_FIELD] == str(fixed)


# --- _publish_pending ---


async def test_publish_marks_rows_and_sets_msg_id_header(session_factory):
    async with session_factory() as session:
        eid = await enqueue_event(session, OutboxEvent, "herd.reservations.created", {"k": "v"})
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
        await enqueue_event(session, OutboxEvent, "s", {})
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
        await enqueue_event(session, OutboxEvent, "s", {"n": 1})
        await enqueue_event(session, OutboxEvent, "s", {"n": 2})
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
            await enqueue_event(session, OutboxEvent, "s", {"n": i})
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
            # Raise a fresh instance each time rather than re-raising the same
            # exception object: re-raising one instance across many ticks
            # chains a new traceback frame onto it on every call (each tick's
            # locals, including the outbox session, stay reachable from the
            # growing chain until the whole exception is released), which is
            # unnecessary here since the tests only assert on the outcome
            # sequence and delay values, never on exception identity.
            raise type(outcome)(*outcome.args)
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


# --- outbox_channel (issue #682) ---


def test_outbox_channel_derives_from_table_schema():
    class LocalBase(DeclarativeBase):
        pass

    class ReservationsOutbox(OutboxMixin, LocalBase):
        __tablename__ = "outbox"
        __table_args__ = {"schema": "reservations"}

    assert outbox_channel(ReservationsOutbox) == "herd_outbox_reservations"


def test_outbox_channel_defaults_to_public_with_no_schema():
    # OutboxEvent (module-level, above) carries no explicit schema.
    assert outbox_channel(OutboxEvent) == "herd_outbox_public"


# --- enqueue_event: dialect-gated pg_notify (issue #682) ---


class _FakeSession:
    """Stands in for AsyncSession: only `bind.dialect.name`, `add`, and
    `execute` matter to enqueue_event."""

    def __init__(self, dialect_name: str):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.added: list = []
        self.execute = AsyncMock()

    def add(self, obj) -> None:
        self.added.append(obj)


async def test_enqueue_event_sqlite_only_adds_no_pg_notify():
    session = _FakeSession("sqlite")
    eid = await enqueue_event(session, OutboxEvent, "s", {})
    assert len(session.added) == 1
    assert session.added[0].id == eid
    session.execute.assert_not_awaited()


async def test_enqueue_event_postgresql_issues_pg_notify_on_same_session():
    session = _FakeSession("postgresql")
    await enqueue_event(session, OutboxEvent, "s", {})
    session.execute.assert_awaited_once()
    args, _ = session.execute.call_args
    stmt, params = args
    assert "pg_notify" in str(stmt)
    assert params == {"channel": outbox_channel(OutboxEvent)}


# --- run_outbox_relay: wake-on-write (issue #682) ---
#
# These exercise the `wake` seam directly (no real Postgres LISTEN
# connection): passing `wake` in tells the relay to honor it on a healthy
# tick exactly as a live `_listen_for_wakeups` task would. The Postgres-live
# proof that a real committed write actually wakes the relay lives in
# test_outbox_wake_live_pg.py.


def _always_connected_nc():
    """An NC that reports connected forever and hands back a fresh OK
    jetstream on every call, so a test can run an unbounded number of ticks
    without exhausting a finite outcomes list."""
    nc = SimpleNamespace(is_connected=True)
    nc.jetstream = lambda: _ok_js()
    return nc


def _fake_engine(dialect_name: str):
    return SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))


async def _run_until_cancel(coro_factory, *, condition, timeout: float = 2.0) -> asyncio.Task:
    """Start `coro_factory()` as a task, poll `condition()` until true (or
    `timeout`), then cancel and await the task. Returns the task so the
    caller can assert on it after cancellation if needed."""
    task = asyncio.create_task(coro_factory())
    try:
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert condition(), "condition never became true before timeout"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return task


async def test_relay_wake_seam_drains_immediately_after_wake_set(session_factory):
    """Passing `wake` in (the test seam) makes a healthy tick wait on it
    instead of sleeping the full tick, even with no engine/listener."""
    drain_times: list[float] = []

    async def fake_publish_pending(session, js, model, *, batch_size, publish_timeout=10.0):
        drain_times.append(time.monotonic())
        return 0

    wake = asyncio.Event()
    nc = _always_connected_nc()

    async def _driver():
        with patch("herd_common.outbox._publish_pending", new=fake_publish_pending):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=60.0,
                prune_every_seconds=10_000.0,
                wake=wake,
            )

    task = asyncio.create_task(_driver())
    try:
        # Wait for the first drain: the relay's startup tick runs before any
        # wait, regardless of the wake seam.
        deadline = time.monotonic() + 2.0
        while not drain_times and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert len(drain_times) == 1

        # Give the loop a moment to reach its post-drain wait (wake.clear()
        # already ran at the top of this same iteration), then wake it: with
        # tick_seconds=60 the ONLY way a second drain happens this fast is
        # via the wake seam, not the tick.
        await asyncio.sleep(0.05)
        wake.set()

        deadline = time.monotonic() + 2.0
        while len(drain_times) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert len(drain_times) == 2
        assert drain_times[1] - drain_times[0] < 1.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_relay_wake_set_during_drain_causes_immediate_next_drain(session_factory):
    """The lost-wakeup rule: wake.clear() runs BEFORE the drain, so a notify
    that lands during the drain is not lost. Simulated by having the fake
    _publish_pending set the event itself, mid-drain."""
    drain_times: list[float] = []
    wake = asyncio.Event()

    async def fake_publish_pending(session, js, model, *, batch_size, publish_timeout=10.0):
        drain_times.append(time.monotonic())
        if len(drain_times) == 1:
            wake.set()
        return 0

    nc = _always_connected_nc()

    async def _driver():
        with patch("herd_common.outbox._publish_pending", new=fake_publish_pending):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=60.0,
                prune_every_seconds=10_000.0,
                wake=wake,
            )

    await _run_until_cancel(_driver, condition=lambda: len(drain_times) >= 2)
    assert drain_times[1] - drain_times[0] < 1.0


async def test_relay_wake_ignored_during_backoff(session_factory):
    """A tick that fails goes to the plain-sleep branch regardless of the
    wake seam: a write during an outage must not turn backoff into a hot
    loop."""
    tick_seconds = 1.0
    wake = asyncio.Event()
    nc = SimpleNamespace(is_connected=True)

    def _failing_jetstream():
        # A write lands exactly as the tick fails.
        wake.set()
        raise RuntimeError("jetstream() unavailable")

    nc.jetstream = _failing_jetstream

    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        raise asyncio.CancelledError()

    with patch("herd_common.outbox.asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=tick_seconds,
                prune_every_seconds=10_000.0,
                wake=wake,
            )

    assert delays == [tick_seconds * 2]
    # The wake WAS set (proving its absence isn't why sleep ran); the relay
    # still took the plain-sleep branch instead of a wake-shortened wait.
    assert wake.is_set()


# --- run_outbox_relay: listener supervision (issue #682) ---


async def test_relay_starts_and_cancels_listener_for_postgres_engine(session_factory):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_listener(engine, channel, wake, *, retry_seconds):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    nc = _always_connected_nc()

    async def _driver():
        await run_outbox_relay(
            session_factory,
            lambda: nc,
            OutboxEvent,
            tick_seconds=60.0,
            prune_every_seconds=10_000.0,
            engine=_fake_engine("postgresql"),
        )

    with patch("herd_common.outbox._listen_for_wakeups", new=fake_listener):
        task = asyncio.create_task(_driver())
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"engine": _fake_engine("sqlite")},
        {"engine": _fake_engine("postgresql"), "wake_on_write": False},
        {"engine": None},
    ],
    ids=["non_postgres_dialect", "wake_on_write_false", "no_engine"],
)
async def test_relay_never_starts_listener_without_a_postgres_engine(session_factory, kwargs):
    calls: list[int] = []

    async def fake_listener(*args, **kw):
        calls.append(1)
        await asyncio.Event().wait()

    nc = _always_connected_nc()

    async def _driver():
        await run_outbox_relay(
            session_factory,
            lambda: nc,
            OutboxEvent,
            tick_seconds=60.0,
            prune_every_seconds=10_000.0,
            **kwargs,
        )

    with patch("herd_common.outbox._listen_for_wakeups", new=fake_listener):
        task = asyncio.create_task(_driver())
        try:
            # Give the relay a moment to run its first tick and reach the wait.
            await asyncio.sleep(0.1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert calls == []


# --- _listen_for_wakeups: connect retry, reconnect backoff/close, DSN
# (issue #695 review sweep: reconnect backoff, close-before-reconnect, DSN
# from connect kwargs) ---


def _fake_pg_engine(opts=None):
    """A fake AsyncEngine stand-in for `_listen_for_wakeups`: it only ever
    calls `engine.dialect.create_connect_args(engine.url)`, so `url` itself
    is never inspected here (the real translation is pinned separately by
    `test_listen_for_wakeups_dsn_from_dialect_connect_args`, which uses a
    real URL and a real dialect)."""
    fixed_opts = dict(
        opts or {"host": "h", "port": 5432, "user": "u", "password": "p", "database": "d"}
    )
    dialect = SimpleNamespace(create_connect_args=lambda url: ([], dict(fixed_opts)))
    return SimpleNamespace(url=SimpleNamespace(), dialect=dialect)


async def test_listen_for_wakeups_retries_then_wakes_on_connect_and_notify(monkeypatch):
    """A connect failure retries after retry_seconds; the successful connect
    sets wake once (catch-up), and the registered NOTIFY callback sets wake
    again on a later notification."""
    engine = _fake_pg_engine()
    wake = asyncio.Event()
    channel = "herd_outbox_test"
    registered: dict = {}
    attempts = {"n": 0}

    class _FakeConn:
        async def add_listener(self, ch, cb) -> None:
            # Real asyncpg.Connection.add_listener is a coroutine (it issues
            # a LISTEN); add_termination_listener below is plain sync.
            registered["notify_channel"] = ch
            registered["notify_cb"] = cb

        def add_termination_listener(self, cb) -> None:
            registered["term_cb"] = cb

        async def close(self) -> None:
            pass

    async def fake_connect(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection refused")
        return _FakeConn()

    fake_asyncpg = SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    task = asyncio.create_task(_listen_for_wakeups(engine, channel, wake, retry_seconds=0.01))
    try:
        await asyncio.wait_for(wake.wait(), timeout=2.0)
        assert attempts["n"] == 2  # one failure, then a successful connect
        assert registered["notify_channel"] == channel

        wake.clear()
        registered["notify_cb"](None, 1, channel, "")
        assert wake.is_set()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_listen_for_wakeups_termination_closes_conn_then_backs_off_before_reconnect(
    monkeypatch,
):
    """The lost-connection branch (the registered termination callback
    firing) closes the old connection and sleeps retry_seconds BEFORE the
    next connect, exactly like the exception branch: previously it
    reconnected immediately with no backoff and never closed the old
    connection."""
    engine = _fake_pg_engine()
    wake = asyncio.Event()
    channel = "herd_outbox_test"
    events: list[str] = []
    term_cbs: list = []

    class _FakeConn:
        def __init__(self, n: int):
            self.n = n

        async def add_listener(self, ch, cb) -> None:
            pass

        def add_termination_listener(self, cb) -> None:
            term_cbs.append(cb)

        async def close(self) -> None:
            events.append(f"close-{self.n}")

    conn_n = {"n": 0}

    async def fake_connect(**kwargs):
        n = conn_n["n"]
        conn_n["n"] += 1
        events.append(f"connect-{n}")
        return _FakeConn(n)

    fake_asyncpg = SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        events.append("sleep")

    real_sleep = asyncio.sleep

    task = asyncio.create_task(_listen_for_wakeups(engine, channel, wake, retry_seconds=7.0))
    try:
        await asyncio.wait_for(wake.wait(), timeout=2.0)
        assert events == ["connect-0"]

        with patch("herd_common.outbox.asyncio.sleep", new=fake_sleep):
            # Fire termination on the first (only) connection.
            term_cbs[0](None)

            # Wait for the loop to come all the way back around: close the
            # old connection, back off, and open a second one.
            deadline = time.monotonic() + 2.0
            while len(term_cbs) < 2 and time.monotonic() < deadline:
                await real_sleep(0.01)
            assert len(term_cbs) == 2, "listener never reconnected after termination"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Close-before-reconnect, and the sleep (retry_seconds) landed strictly
    # between the close and the next connect on this branch too. The trailing
    # close-1 is cancellation closing the live second connection.
    assert events == ["connect-0", "close-0", "sleep", "connect-1", "close-1"]
    assert sleeps == [7.0]


async def test_listen_for_wakeups_registration_failure_closes_conn_and_leaks_none(monkeypatch):
    """add_listener raising leaves the just-opened connection closed (not
    leaked), wake is never set for that failed attempt, and only one live
    connection is ever open at a time."""
    engine = _fake_pg_engine()
    wake = asyncio.Event()
    channel = "herd_outbox_test"
    conns: list = []

    class _FakeConn:
        def __init__(self, n: int, fail_registration: bool):
            self.n = n
            self.fail_registration = fail_registration
            self.closed = False

        async def add_listener(self, ch, cb) -> None:
            if self.fail_registration:
                raise RuntimeError("add_listener failed")

        def add_termination_listener(self, cb) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    async def fake_connect(**kwargs):
        conn = _FakeConn(len(conns), fail_registration=len(conns) == 0)
        conns.append(conn)
        return conn

    fake_asyncpg = SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    task = asyncio.create_task(_listen_for_wakeups(engine, channel, wake, retry_seconds=0.01))
    try:
        # wake is set only after a SUCCESSFUL registration, so this proves
        # the second (successfully-registered) connection was reached.
        await asyncio.wait_for(wake.wait(), timeout=2.0)
        assert len(conns) == 2
        assert conns[0].closed, "the connection from the failed registration attempt leaked"
        assert not conns[1].closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conns[1].closed  # cancellation closes the live connection too

    # No further reconnects: exactly one failed + one live connection.
    assert len(conns) == 2


async def test_listen_for_wakeups_dsn_from_dialect_connect_args(monkeypatch):
    """The connection is opened from `engine.dialect.create_connect_args`
    kwargs, not a rendered DSN string: `ssl` and
    `prepared_statement_cache_size` reach asyncpg as kwargs (the spellings
    its own DSN parser rejects), no `dsn` kwarg is passed, and the
    asyncpg-only pool-wrapper keys the dialect adds are stripped."""
    url = make_url("postgresql+asyncpg://u:p@h:5432/d?ssl=require&prepared_statement_cache_size=0")
    engine = SimpleNamespace(url=url, dialect=PGDialect_asyncpg())

    wake = asyncio.Event()
    channel = "herd_outbox_test"
    captured: dict = {}

    class _FakeConn:
        async def add_listener(self, ch, cb) -> None:
            pass

        def add_termination_listener(self, cb) -> None:
            pass

        async def close(self) -> None:
            pass

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return _FakeConn()

    fake_asyncpg = SimpleNamespace(connect=fake_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

    task = asyncio.create_task(_listen_for_wakeups(engine, channel, wake, retry_seconds=0.01))
    try:
        await asyncio.wait_for(wake.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert captured["ssl"] == "require"
    assert captured["host"] == "h"
    assert captured["port"] == 5432
    assert captured["user"] == "u"
    assert captured["password"] == "p"
    assert captured["database"] == "d"
    assert "prepared_statement_cache_size" not in captured
    assert "dsn" not in captured


# --- run_outbox_relay: saturated batch loops immediately (issue #695 review
# sweep item 3) ---


async def test_relay_saturated_batch_drains_remainder_without_waiting_a_tick(session_factory):
    """150 staged rows with batch_size=100: Postgres collapses the whole
    commit into one NOTIFY, so the drain that comes back exactly `batch_size`
    must loop immediately for the remaining 50 instead of waiting out
    tick_seconds=60 first."""
    async with session_factory() as session:
        for i in range(150):
            await enqueue_event(session, OutboxEvent, "s", {"n": i})
        await session.commit()

    real_publish_pending = _publish_pending
    drain_times: list[float] = []
    drain_counts: list[int] = []

    async def counting_publish_pending(session, js, model, *, batch_size, publish_timeout=10.0):
        drain_times.append(time.monotonic())
        count = await real_publish_pending(
            session, js, model, batch_size=batch_size, publish_timeout=publish_timeout
        )
        drain_counts.append(count)
        return count

    js = _js_ok()
    nc = SimpleNamespace(is_connected=True)
    nc.jetstream = lambda: js

    async def _driver():
        with patch("herd_common.outbox._publish_pending", new=counting_publish_pending):
            await run_outbox_relay(
                session_factory,
                lambda: nc,
                OutboxEvent,
                tick_seconds=60.0,
                batch_size=100,
                prune_every_seconds=10_000.0,
            )

    task = asyncio.create_task(_driver())
    try:
        deadline = time.monotonic() + 5.0
        while len(drain_counts) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert drain_counts == [100, 50]
        # The second drain followed the first back-to-back, nowhere near the
        # 60s tick.
        assert drain_times[1] - drain_times[0] < 1.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert js.publish.await_count == 150
