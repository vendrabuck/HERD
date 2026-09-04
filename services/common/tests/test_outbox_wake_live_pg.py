"""Real-Postgres coverage for the outbox relay's wake-on-write path (issue #682).

test_outbox.py proves the wake mechanics (the `wake` seam, the lost-wakeup
rule, listener supervision, connect retry) against SQLite and a stub asyncpg
module; none of that proves a REAL committed write on a REAL Postgres server
actually fires `pg_notify` and reaches a REAL `_listen_for_wakeups` task end
to end. This file is that missing half: it runs `run_outbox_relay` against a
real engine with a real Postgres LISTEN connection, commits an outbox row
from a SEPARATE session (exactly like a service's request handler would),
and measures the time from that commit to the relay's publish call.

Skipped automatically when no Postgres is reachable at the configured DSN,
so the suite stays green without one. Setting HERD_TEST_PG_REQUIRED=1
disables the skip: an unreachable server then fails every test with an
explicit message, mirroring test_advisory_lock_live_pg.py's
HERD_TEST_PG_REQUIRED contract; this file follows that env contract
identically (HERD_TEST_PG_DSN, HERD_TEST_PG_REQUIRED, POSTGRES_PORT). See
test_advisory_lock_live_pg.py in this directory for the full writeup.

Each test gets its own throwaway schema (a fresh uuid-suffixed name) holding
a single OutboxMixin table, dropped (CASCADE) in a fixture finally, so a
shared server (e.g. someone else's dev stack) is left clean regardless of
pass or fail.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from herd_common.outbox import OutboxMixin, enqueue_event, run_outbox_relay
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DEFAULT_PG_PORT = os.getenv("POSTGRES_PORT", "5433")
PG_DSN = os.getenv(
    "HERD_TEST_PG_DSN",
    f"postgresql+asyncpg://herd:herd@127.0.0.1:{DEFAULT_PG_PORT}/herd",
)
_PG_REQUIRED = os.getenv("HERD_TEST_PG_REQUIRED", "") not in ("", "0")

# The issue's acceptance number: commit-to-publish latency under wake-on-write.
_WAKE_LATENCY_BOUND_SECONDS = 0.2


async def _pg_reachable() -> bool:
    engine = create_async_engine(PG_DSN)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


def _run_sync_reachable() -> bool:
    return asyncio.run(_pg_reachable())


_PG_REACHABLE = _run_sync_reachable()

pytestmark = pytest.mark.skipif(
    not _PG_REQUIRED and not _PG_REACHABLE,
    reason=(
        f"No Postgres reachable at {PG_DSN!r}; set HERD_TEST_PG_DSN to point at one "
        "(see test_advisory_lock_live_pg.py for the full env contract) to run this suite."
    ),
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _PG_REQUIRED and not _PG_REACHABLE:
        pytest.fail(
            f"HERD_TEST_PG_REQUIRED is set but no Postgres is reachable at {PG_DSN!r}; "
            "start one or unset HERD_TEST_PG_REQUIRED."
        )


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(PG_DSN)
    yield engine
    await engine.dispose()


@pytest.fixture
async def outbox_model(pg_engine):
    """A throwaway schema holding one OutboxMixin table, dropped after."""
    schema_name = f"herd_test_outbox_wake_{uuid.uuid4().hex[:12]}"

    class LocalBase(DeclarativeBase):
        pass

    class ThrowawayOutbox(OutboxMixin, LocalBase):
        __tablename__ = "outbox"
        __table_args__ = {"schema": schema_name}

    async with pg_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        await conn.run_sync(LocalBase.metadata.create_all)

    try:
        yield ThrowawayOutbox
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))


def _fake_nats(publish_times: list[float]):
    """A minimal stand-in for the NATS client: always connected, and
    jetstream().publish records the wall-clock time of each call instead of
    touching a real broker."""

    async def _publish(*args, **kwargs):
        publish_times.append(time.monotonic())

    js = AsyncMock()
    js.publish = _publish

    class _NC:
        is_connected = True

        def jetstream(self):
            return js

    return _NC()


async def _cancel_and_await(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_wake_on_write_publishes_within_200ms_of_commit(pg_engine, outbox_model):
    """A committed enqueue_event wakes the relay well under the issue's
    200ms acceptance bound, using a 30s tick that would otherwise mask a
    wake failure (a pass here cannot be explained by the tick firing)."""
    session_factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    publish_times: list[float] = []
    nc = _fake_nats(publish_times)

    task = asyncio.create_task(
        run_outbox_relay(
            session_factory,
            lambda: nc,
            outbox_model,
            name="test-wake-live",
            tick_seconds=30.0,
            prune_every_seconds=10_000.0,
            engine=pg_engine,
            wake_on_write=True,
        )
    )
    try:
        # Let the listener connect and register LISTEN before we commit: the
        # first tick's own catch-up wake fires on an empty table and settles
        # out well before this window, so the timed commit below only ever
        # races a real NOTIFY, never the listener's own startup.
        await asyncio.sleep(1.0)

        async with session_factory() as session:
            await enqueue_event(session, outbox_model, "s", {"k": "v"})
            await session.commit()
            commit_time = time.monotonic()

        deadline = time.monotonic() + 5.0
        while not publish_times and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        assert publish_times, "relay never published the row within 5s"
        latency = publish_times[0] - commit_time
        assert latency < _WAKE_LATENCY_BOUND_SECONDS, (
            f"commit-to-publish latency was {latency:.4f}s, "
            f"wanted under {_WAKE_LATENCY_BOUND_SECONDS}s"
        )
    finally:
        await _cancel_and_await(task)


async def test_wake_on_write_disabled_does_not_publish_within_1s(pg_engine, outbox_model):
    """wake_on_write=False: the same write is NOT drained within 1s, proving
    the wake (not the 30s tick, which has not fired yet) did the draining
    above."""
    session_factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    publish_times: list[float] = []
    nc = _fake_nats(publish_times)

    task = asyncio.create_task(
        run_outbox_relay(
            session_factory,
            lambda: nc,
            outbox_model,
            name="test-wake-live-disabled",
            tick_seconds=30.0,
            prune_every_seconds=10_000.0,
            engine=pg_engine,
            wake_on_write=False,
        )
    )
    try:
        await asyncio.sleep(1.0)

        async with session_factory() as session:
            await enqueue_event(session, outbox_model, "s", {"k": "v"})
            await session.commit()

        await asyncio.sleep(1.0)
        assert publish_times == []
    finally:
        await _cancel_and_await(task)
