"""Issue #702: the purpose-classify reconciler runs on its own asyncio task,
separate from expiration_task, at its own interval, and is cancelled
alongside expiration_task at shutdown. Mirrors auth's
test_lifespan_starts_ldap_sync_loop_when_enabled (services/auth/tests/
test_main.py): fakes for both loop bodies signal they started and then block
until cancelled, proving the tasks are actually scheduled, not just that
create_task would have been reachable.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings


@pytest.mark.asyncio
async def test_lifespan_creates_and_cancels_expiration_and_purpose_classify_tasks():
    from app.main import lifespan

    expiration_started = asyncio.Event()
    purpose_classify_started = asyncio.Event()
    expiration_intervals: list[int] = []
    purpose_classify_intervals: list[int] = []

    async def fake_expiration_loop(interval_seconds=60):
        expiration_intervals.append(interval_seconds)
        expiration_started.set()
        await asyncio.Event().wait()

    async def fake_purpose_classify_loop(interval_seconds=60):
        purpose_classify_intervals.append(interval_seconds)
        purpose_classify_started.set()
        await asyncio.Event().wait()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("nats.connect", new=AsyncMock(side_effect=Exception("no nats in test env"))),
            patch("app.tasks.expiration.expiration_loop", new=fake_expiration_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_purpose_classify_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                await expiration_started.wait()
                await purpose_classify_started.wait()

    # This bound catches a hang before the lifespan yields: wait_for cancels _run()
    # and raises TimeoutError. It cannot catch a shutdown hang (issue #1527): the
    # cancellation lands in the lifespan's `await` of a task, which cancels that
    # task as well, the lifespan's own `except asyncio.CancelledError` swallows
    # it, and _run() returns normally. Shutdown is pinned without a clock by
    # test_shutdown_cancels_and_awaits_every_task_the_lifespan_starts below.
    await asyncio.wait_for(_run(), timeout=10)

    assert expiration_started.is_set()
    assert purpose_classify_started.is_set()
    assert expiration_intervals == [settings.expiration_interval_seconds]
    assert purpose_classify_intervals == [settings.purpose_classify_interval_seconds]


@pytest.mark.asyncio
async def test_lifespan_purpose_classify_task_uses_its_own_interval_setting():
    """The two tasks are independently configurable: a distinct
    purpose_classify_interval_seconds is threaded through to
    purpose_classify_loop, not expiration_interval_seconds."""
    from app.main import lifespan

    purpose_classify_started = asyncio.Event()
    seen_intervals: list[int] = []

    async def fake_expiration_loop(interval_seconds=60):
        await asyncio.Event().wait()

    async def fake_purpose_classify_loop(interval_seconds=60):
        seen_intervals.append(interval_seconds)
        purpose_classify_started.set()
        await asyncio.Event().wait()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("nats.connect", new=AsyncMock(side_effect=Exception("no nats in test env"))),
            patch.object(settings, "purpose_classify_interval_seconds", 12345),
            patch("app.tasks.expiration.expiration_loop", new=fake_expiration_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_purpose_classify_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                await purpose_classify_started.wait()

    await asyncio.wait_for(_run(), timeout=10)
    assert seen_intervals == [12345]


@pytest.mark.asyncio
async def test_lifespan_starts_without_nats_using_the_real_client(monkeypatch, caplog):
    """Issue #1083: with the REAL nats client and a broker that refuses
    connections, startup finishes (the bounded first connect raises) and the
    documented logged-and-continue branch runs, rather than the lifespan
    waiting forever inside nats.connect. No mock of nats.connect here: a mock
    that raises proved a path the real client never took."""
    import herd_common.jetstream as jetstream
    from app.main import lifespan

    monkeypatch.setattr(jetstream, "NATS_INITIAL_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(jetstream, "NATS_RECONNECT_TIME_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(settings, "nats_url", "nats://127.0.0.1:1")

    async def fake_loop(interval_seconds=60):
        await asyncio.Event().wait()

    entered = asyncio.Event()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("app.tasks.expiration.expiration_loop", new=fake_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                entered.set()
                assert mock_app.state.nats is None

    with caplog.at_level("WARNING", logger="app.main"):
        await asyncio.wait_for(_run(), timeout=10)

    assert entered.is_set()
    # RES-EVENT-4 (issue #1337): events are not skipped, they wait in the outbox,
    # and a connection that failed at boot is not picked up later.
    assert (
        "NATS unavailable at nats://127.0.0.1:1; events wait in the outbox until the "
        "service is restarted with the broker reachable"
    ) in caplog.text
    assert "skipped" not in caplog.text
    assert "could not be created or updated" not in caplog.text


@pytest.mark.asyncio
async def test_lifespan_names_a_failed_stream_ensure_apart_from_a_failed_connect(
    monkeypatch, caplog
):
    """Issue #1337: when the connect succeeds but ensuring HERD_RESERVATIONS fails,
    the connection is kept and the relay publishes: at once if the stream exists
    (an update failed), every tick while it is missing (a create failed). The
    warning must not say the broker is unavailable or that a restart is needed."""
    from app.main import lifespan

    monkeypatch.setattr(settings, "nats_url", "nats://broker:4222")
    nc = MagicMock()
    nc.close = AsyncMock()

    async def fake_loop(interval_seconds=60):
        await asyncio.Event().wait()

    async def _run():
        with (
            patch("app.main.create_all_and_stamp", new=AsyncMock()),
            patch("app.main.connect_nats", new=AsyncMock(return_value=nc)),
            patch("app.main.ensure_stream", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("app.main.run_outbox_relay", new=AsyncMock()),
            patch("app.tasks.expiration.expiration_loop", new=fake_loop),
            patch("app.tasks.expiration.purpose_classify_loop", new=fake_loop),
        ):
            mock_app = MagicMock()
            async with lifespan(mock_app):
                assert mock_app.state.nats is nc

    with caplog.at_level("WARNING", logger="app.main"):
        await asyncio.wait_for(_run(), timeout=10)

    assert (
        "JetStream stream HERD_RESERVATIONS could not be created or updated at "
        "nats://broker:4222; the connection is kept, so events publish if the stream "
        "exists, and while it is missing they wait in the outbox and the relay retries "
        "them every tick"
    ) in caplog.text
    assert "NATS unavailable" not in caplog.text
    assert "restarted" not in caplog.text
    nc.close.assert_awaited_once()


# Shutdown (issue #1527). The tests below never read a clock: every bound is a count
# of event-loop iterations (`asyncio.sleep(0)` passes control to the loop exactly
# once), so they fail fast instead of hanging when cancellation is broken, give the
# same result on a loaded or single-core host, and the pytest-timeout marker is only
# a backstop for a hang in the clean-up itself. A correct shutdown of the three
# faked tasks takes a few iterations; the budget is far above that.
_LOOP_BUDGET = 200


async def _spin(iterations: int = _LOOP_BUDGET) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


class _NoBrokerHere(Exception):
    """The fake `connect_nats` raises this, so the lifespan starts without NATS."""


class _LifespanLoops:
    """Fakes for the three loops the lifespan starts, keyed by the name it imports.

    Each fake records its task, blocks until cancelled, and on cancellation waits for
    its own release event before it ends cancelled. A fake built with `held=<name>`
    keeps that one task mid-cancellation until the test sets its event, so a
    shutdown that returns before the task has ended is visible.
    """

    NAMES = ("expiration_loop", "purpose_classify_loop", "run_outbox_relay")

    def __init__(self, held: str | None = None) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancel_seen: set[str] = set()
        self.release = {name: asyncio.Event() for name in self.NAMES}
        for name in self.NAMES:
            if name != held:
                self.release[name].set()

    def fake(self, name: str):
        async def run(*args, **kwargs):
            self.tasks[name] = asyncio.current_task()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.add(name)
                await self.release[name].wait()
                raise

        return run

    def patches(self):
        return (
            patch("app.tasks.expiration.expiration_loop", new=self.fake("expiration_loop")),
            patch(
                "app.tasks.expiration.purpose_classify_loop",
                new=self.fake("purpose_classify_loop"),
            ),
            patch("app.main.run_outbox_relay", new=self.fake("run_outbox_relay")),
        )


async def _shut_down_holding(held: str) -> None:
    """Run the real lifespan with every loop faked, hold `held` mid-cancellation at
    shutdown, and check that shutdown waits for it and then returns."""
    from app.main import lifespan

    loops = _LifespanLoops(held=held)
    started: set[asyncio.Task] = set()
    exit_task: asyncio.Task | None = None
    expiration_patch, classify_patch, relay_patch = loops.patches()
    with (
        patch("app.main.create_all_and_stamp", new=AsyncMock()),
        patch("app.main.connect_nats", new=AsyncMock(side_effect=_NoBrokerHere())),
        expiration_patch,
        classify_patch,
        relay_patch,
    ):
        before = asyncio.all_tasks()
        context = lifespan(MagicMock())
        await context.__aenter__()
        try:
            await _spin()
            started = asyncio.all_tasks() - before
            # The tasks are enumerated from the lifespan itself: every task it left
            # running must be one of the fakes, so a task added to the lifespan
            # later fails here until it is faked in _LifespanLoops.
            assert started == set(loops.tasks.values()), (
                "the lifespan started a task this test does not fake; add a fake for "
                f"it to _LifespanLoops (faked: {sorted(loops.tasks)})"
            )
            assert set(loops.tasks) == set(_LifespanLoops.NAMES), sorted(loops.tasks)

            exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            await _spin()
            assert held in loops.cancel_seen, f"shutdown never cancelled {held}"
            assert not exit_task.done(), (
                f"shutdown returned while {held} was still ending: it must await the task"
            )
            assert not loops.tasks[held].done()

            loops.release[held].set()
            await _spin()
            assert exit_task.done(), (
                f"shutdown did not return within {_LOOP_BUDGET} event-loop iterations "
                f"after {held} ended; a task it awaits was not cancelled"
            )
            assert await exit_task is False  # re-raises anything shutdown raised
            for name, task in loops.tasks.items():
                assert task.done() and task.cancelled(), name
        finally:
            for event in loops.release.values():
                event.set()
            for task in started:
                task.cancel()
            if exit_task is None:
                exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            await _spin()
            if not exit_task.done():
                exit_task.cancel()
            await asyncio.gather(*started, exit_task, return_exceptions=True)


@pytest.mark.timeout(60)
async def test_shutdown_cancels_and_awaits_every_task_the_lifespan_starts():
    """Issue #1527: at shutdown the lifespan cancels every background task it started
    (the expiration loop, the purpose-classify loop, and the outbox relay, each
    blocking until cancelled here, the relay included) and returns only after each
    one has ended, and then within a fixed number of event-loop iterations. Each
    task in turn is held mid-cancellation, so a task that is cancelled but not
    awaited is caught wherever it sits in the shutdown order, and a task that is
    awaited but never cancelled leaves shutdown waiting past the budget."""
    for held in _LifespanLoops.NAMES:
        await _shut_down_holding(held)


@pytest.mark.timeout(60)
async def test_startup_creates_the_schema_from_the_services_metadata():
    """The lifespan builds or checks the schema at startup with this service's
    metadata and its `DB_SCHEMA` setting (the unit suite's empty one here)."""
    from app.database import Base, engine
    from app.main import lifespan

    loops = _LifespanLoops()
    create_all = AsyncMock()
    expiration_patch, classify_patch, relay_patch = loops.patches()
    with (
        patch("app.main.create_all_and_stamp", new=create_all),
        patch("app.main.connect_nats", new=AsyncMock(side_effect=_NoBrokerHere())),
        expiration_patch,
        classify_patch,
        relay_patch,
    ):
        async with lifespan(MagicMock()):
            create_all.assert_awaited_once()
            args, kwargs = create_all.await_args
            assert args == (engine, Base.metadata)
            assert kwargs["schema"] == settings.db_schema


_EXPIRATION_STEPS = (
    "_run_expiration_cycle",
    "_run_reminder_cycle",
    "_run_fork_archive_reconcile",
    "_run_pending_prune_reconcile",
)
_CLASSIFY_STEPS = ("_run_purpose_classify_reconcile",)


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("loop_name", "steps", "blocking"),
    [
        pytest.param(loop_name, steps, blocking, id=f"{loop_name}-{blocking or 'sleep'}")
        for loop_name, steps in (
            ("expiration_loop", _EXPIRATION_STEPS),
            ("purpose_classify_loop", _CLASSIFY_STEPS),
        )
        for blocking in (*steps, None)
    ],
)
async def test_each_real_loop_ends_when_cancelled_in_a_step_or_in_its_sleep(
    loop_name, steps, blocking
):
    """The shutdown above fakes the loops; this runs the real ones. A cancel that
    lands while a step is running (the step blocks until cancelled) or while the
    loop sleeps (`blocking` None: every step returns at once and the loop reaches its
    real `asyncio.sleep` of an hour) ends the task cancelled within the iteration
    budget: each step's `except Exception` must not catch the cancellation. The
    steps are the four RES-SWEEP-10 names and the purpose-classify reconciler."""
    from contextlib import ExitStack

    from app.tasks import expiration

    calls: list[str] = []
    reached = asyncio.Event()

    def step(name):
        async def run():
            calls.append(name)
            if name == blocking:
                reached.set()
                await asyncio.Event().wait()

        return run

    with ExitStack() as stack:
        for name in steps:
            stack.enter_context(patch.object(expiration, name, new=step(name)))
        task = asyncio.create_task(getattr(expiration, loop_name)(interval_seconds=3600))
        try:
            await _spin()
            if blocking is None:
                assert sorted(calls) == sorted(steps)  # one tick ran; now in the sleep
            else:
                assert reached.is_set() and calls[-1] == blocking
            task.cancel()
            await _spin()
            assert task.done() and task.cancelled(), (
                f"{loop_name} did not end cancelled after a cancel in {blocking or 'its sleep'}"
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
