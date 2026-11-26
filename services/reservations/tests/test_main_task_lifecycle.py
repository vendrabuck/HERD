"""Issue #702: the purpose-classify reconciler runs on its own asyncio task,
separate from expiration_task, at its own interval, and is cancelled
alongside expiration_task at shutdown. Mirrors auth's
test_lifespan_starts_ldap_sync_loop_when_enabled (services/auth/tests/
test_main.py): fakes for both loop bodies signal they started, proving the
tasks are actually scheduled, not just that create_task would have been
reachable.

The loop fakes of the four tests before the shutdown section return at once
(issue #1527), so a shutdown that fails to cancel a task has nothing to wait
on there; the shutdown tests below hold their fakes until cancelled and bound
every wait by event-loop iterations.
"""

import asyncio
from contextlib import ExitStack
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

    async def fake_purpose_classify_loop(interval_seconds=60):
        purpose_classify_intervals.append(interval_seconds)
        purpose_classify_started.set()

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
        return None

    async def fake_purpose_classify_loop(interval_seconds=60):
        seen_intervals.append(interval_seconds)
        purpose_classify_started.set()

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
        return None

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
        return None

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


# Shutdown (issue #1527). The tests below never read a clock: every wait, the
# clean-up included, is bounded by a count of event-loop iterations
# (`asyncio.sleep(0)` passes control to the loop exactly once), or the test's fakes
# return at once, so they fail fast instead of hanging when cancellation is broken,
# and give the same result on a loaded or single-core host. The pytest-timeout
# markers are a backstop only. A correct shutdown of the three faked tasks takes a
# few iterations; the budget is far above that.
_LOOP_BUDGET = 200


async def _spin(iterations: int = _LOOP_BUDGET) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


def _retrieve(*tasks: asyncio.Task) -> None:
    """Read the outcome of every finished task, so none is reported as never
    retrieved. A task still running after a bounded clean-up is not awaited: the
    test it belongs to has already failed."""
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()


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
            # Issue #1549: every task is cancelled before any is awaited, so the tasks
            # after a held one are already ending while it is held (a shutdown that
            # cancels and awaits each in turn leaves them running here).
            assert loops.cancel_seen == set(_LifespanLoops.NAMES), (
                f"while {held} was held, shutdown had cancelled only "
                f"{sorted(loops.cancel_seen)}: it must cancel every task before awaiting any"
            )
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
                await _spin()
            _retrieve(*started, exit_task)


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


class _TaskDied(Exception):
    """A fake loop raises this at once, before the lifespan yields (issue #1549)."""


class _TaskDiedHard(BaseException):
    """The same, but not an `Exception`: issue #1549 names this case first. asyncio
    stores it on the task like an Exception (only KeyboardInterrupt and SystemExit
    also leave the event loop), so the test loop keeps running."""


# Text an exception may carry from a driver, a model, or a stored value: the log lines
# name the exception class only.
_DEATH_TEXT = "upstream said: % Invalid input detected at marker"

# The task names the lifespan logs, keyed by the name each fake replaces.
_TASK_NAMES = {
    "expiration_loop": "expiration_loop",
    "purpose_classify_loop": "purpose_classify_loop",
    "run_outbox_relay": "outbox_relay",
}


async def _crash_one_then_shut_down(dead: str, caplog, died_with=_TaskDied) -> None:
    """Run the real lifespan with NATS connected, make the `dead` loop end with
    `died_with` at once, and check that it is logged before shutdown and that shutdown
    then cancels the other two, closes NATS, and returns without raising."""
    from app.main import lifespan

    loops = _LifespanLoops()
    survivors = [name for name in _LifespanLoops.NAMES if name != dead]

    async def dies(*args, **kwargs):
        loops.tasks[dead] = asyncio.current_task()
        raise died_with(_DEATH_TEXT)

    patches = dict(zip(_LifespanLoops.NAMES, loops.patches(), strict=True))
    patches[dead] = patch(
        {
            "expiration_loop": "app.tasks.expiration.expiration_loop",
            "purpose_classify_loop": "app.tasks.expiration.purpose_classify_loop",
            "run_outbox_relay": "app.main.run_outbox_relay",
        }[dead],
        new=dies,
    )
    nc = MagicMock()
    nc.close = AsyncMock()
    caplog.clear()
    started: set[asyncio.Task] = set()
    exit_task: asyncio.Task | None = None
    with (
        caplog.at_level("ERROR", logger="app.main"),
        patch("app.main.create_all_and_stamp", new=AsyncMock()),
        patch("app.main.connect_nats", new=AsyncMock(return_value=nc)),
        patch("app.main.ensure_stream", new=AsyncMock()),
        patches["expiration_loop"],
        patches["purpose_classify_loop"],
        patches["run_outbox_relay"],
    ):
        before = asyncio.all_tasks()
        context = lifespan(MagicMock())
        await context.__aenter__()
        try:
            await _spin()
            started = asyncio.all_tasks() - before
            assert loops.tasks[dead].done() and not loops.tasks[dead].cancelled()

            # Logged when it ended, while the service still runs: not at shutdown.
            crashed = [
                r for r in caplog.records if getattr(r, "action", None) == "background_task_crashed"
            ]
            assert [(r.levelname, r.task_name, r.error_class) for r in crashed] == [
                ("ERROR", _TASK_NAMES[dead], died_with.__name__)
            ], f"{dead} died and was not logged when it ended"
            for name in survivors:
                assert not loops.tasks[name].done(), name

            exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            await _spin()
            assert exit_task.done(), f"shutdown did not return after {dead} died"
            assert await exit_task is False  # re-raises anything shutdown raised
            for name in survivors:
                assert name in loops.cancel_seen, f"shutdown never cancelled {name}"
                assert loops.tasks[name].cancelled(), name
            nc.close.assert_awaited_once()

            at_shutdown = [
                r
                for r in caplog.records
                if getattr(r, "action", None) == "background_task_failed_at_shutdown"
            ]
            assert [(r.levelname, r.task_name, r.error_class) for r in at_shutdown] == [
                ("ERROR", _TASK_NAMES[dead], died_with.__name__)
            ]
            assert _DEATH_TEXT not in caplog.text
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
                await _spin()
            _retrieve(*started, exit_task)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("died_with", [_TaskDied, _TaskDiedHard])
@pytest.mark.parametrize("dead", _LifespanLoops.NAMES)
async def test_a_task_that_dies_is_logged_at_once_and_shutdown_still_runs_every_step(
    dead, died_with, caplog
):
    """Issue #1549: each of the three lifespan tasks in turn ends with an exception
    before the lifespan yields, an `Exception` and a `BaseException` that is not one.
    It is logged at ERROR the moment it ends (fixed `action`, the task's name, the
    class name, no exception text), the others keep running, and shutdown cancels the
    other two, logs the dead one again, closes NATS, and returns without raising."""
    await _crash_one_then_shut_down(dead, caplog, died_with)


_LOOP_TARGETS = {
    "expiration_loop": "app.tasks.expiration.expiration_loop",
    "purpose_classify_loop": "app.tasks.expiration.purpose_classify_loop",
    "run_outbox_relay": "app.main.run_outbox_relay",
}


def _crash_patches(loops: "_LifespanLoops", dead: str, died_with: type[BaseException]):
    """The three loop patches, with `dead` replaced by a fake that raises at once."""

    async def dies(*args, **kwargs):
        loops.tasks[dead] = asyncio.current_task()
        raise died_with(_DEATH_TEXT)

    patches = dict(zip(_LifespanLoops.NAMES, loops.patches(), strict=True))
    patches[dead] = patch(_LOOP_TARGETS[dead], new=dies)
    return [patches[name] for name in _LifespanLoops.NAMES]


def _records(caplog, action: str) -> list[tuple[str, str, str]]:
    return [
        (r.levelname, r.task_name, r.error_class)
        for r in caplog.records
        if getattr(r, "action", None) == action
    ]


@pytest.mark.timeout(60)
@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("dead", _LifespanLoops.NAMES)
def test_a_process_exit_signal_a_task_ended_with_is_raised_after_every_shutdown_step(
    dead, signal, caplog
):
    """Issue #1549: a task that ends with KeyboardInterrupt or SystemExit is logged
    like any other, and shutdown still cancels the other two and closes NATS; then
    the signal is raised, not swallowed. asyncio raises these two out of the event
    loop when a task ends with one, so this test runs its own loop and counts each
    one where it leaves `run_until_complete`: once when the loop dies, once when the
    shutdown ends with it."""
    from app.main import lifespan

    loop = asyncio.new_event_loop()
    loops = _LifespanLoops()
    nc = MagicMock()
    nc.close = AsyncMock()
    survivors = [name for name in _LifespanLoops.NAMES if name != dead]

    def drive(coro) -> tuple[asyncio.Task, int]:
        """Run `coro` as a task to its end; count the signals that left the loop."""
        task = loop.create_task(coro)
        left_the_loop = 0
        while not task.done():
            try:
                loop.run_until_complete(task)
            except signal:
                left_the_loop += 1
            except RuntimeError:
                # A signal that interrupted the previous run left that run's stop
                # callback queued, so this run can stop early; run again.
                if task.done():
                    raise
        return task, left_the_loop

    try:
        with ExitStack() as stack:
            stack.enter_context(caplog.at_level("ERROR", logger="app.main"))
            stack.enter_context(patch("app.main.create_all_and_stamp", new=AsyncMock()))
            stack.enter_context(patch("app.main.connect_nats", new=AsyncMock(return_value=nc)))
            stack.enter_context(patch("app.main.ensure_stream", new=AsyncMock()))
            for loop_patch in _crash_patches(loops, dead, signal):
                stack.enter_context(loop_patch)
            context = lifespan(MagicMock())
            entered, during_start = drive(context.__aenter__())
            _, during_spin = drive(_spin())
            assert entered.exception() is None
            assert during_start + during_spin == 1, "the dead task's signal left the loop"
            assert _records(caplog, "background_task_crashed") == [
                ("ERROR", _TASK_NAMES[dead], signal.__name__)
            ]
            for name in survivors:
                assert not loops.tasks[name].done(), name

            exited, during_shutdown = drive(context.__aexit__(None, None, None))

            assert isinstance(exited.exception(), signal), "shutdown swallowed the signal"
            assert during_shutdown == 1
            nc.close.assert_awaited_once()
            for name in survivors:
                assert loops.tasks[name].cancelled(), name
            assert _records(caplog, "background_task_failed_at_shutdown") == [
                ("ERROR", _TASK_NAMES[dead], signal.__name__)
            ]
            assert _DEATH_TEXT not in caplog.text
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()

        async def _drain() -> None:
            await asyncio.gather(*pending, return_exceptions=True)
            await loop.shutdown_asyncgens()

        drive(_drain())
        loop.close()


async def _enter_holding(held: str, nc=None):
    """Enter the real lifespan with every loop faked and `held` held mid-cancellation;
    return (loops, context, started tasks, patch stack)."""
    from app.main import lifespan

    loops = _LifespanLoops(held=held)
    stack = ExitStack()
    stack.enter_context(patch("app.main.create_all_and_stamp", new=AsyncMock()))
    if nc is None:
        stack.enter_context(
            patch("app.main.connect_nats", new=AsyncMock(side_effect=_NoBrokerHere()))
        )
    else:
        stack.enter_context(patch("app.main.connect_nats", new=AsyncMock(return_value=nc)))
        stack.enter_context(patch("app.main.ensure_stream", new=AsyncMock()))
    for loop_patch in loops.patches():
        stack.enter_context(loop_patch)
    before = asyncio.all_tasks()
    context = lifespan(MagicMock())
    await context.__aenter__()
    await _spin()
    return loops, context, asyncio.all_tasks() - before, stack


async def _release_and_reap(loops, started, *extra) -> None:
    for event in loops.release.values():
        event.set()
    for task in started:
        task.cancel()
    await _spin()
    _retrieve(*started, *extra)


class _ThrownIntoShutdown(BaseException):
    """Thrown into the shutdown's frame from outside; never any task's exception."""


@pytest.mark.timeout(60)
@pytest.mark.parametrize("thrown", [GeneratorExit, _ThrownIntoShutdown])
async def test_closing_the_lifespan_while_it_awaits_a_task_lets_generator_exit_through(thrown):
    """Issue #1549: what is thrown into the shutdown while it awaits a task is not the
    task's exception: it propagates at once instead of being logged as the task's and
    swallowed. For a GeneratorExit (the lifespan closed while suspended there) Python
    already closes the awaited frames first and raises it in the lifespan's own frame,
    so the lifespan must let it through rather than await the next task ("coroutine
    ignored GeneratorExit"); any other exception thrown in reaches `await_stopped_task`
    itself, which must tell it from the task's own by identity."""
    loops, context, started, stack = await _enter_holding("expiration_loop")
    with stack:
        try:
            # Drive the lifespan's own async generator by hand (closing the context
            # manager's `__aexit__` coroutine instead would close only its `asend`
            # wrapper and never reach the lifespan's frame).
            shutdown = context.gen.asend(None)  # resumes after the yield
            awaited = shutdown.send(None)  # runs to the await of the held task
            assert awaited is loops.tasks["expiration_loop"]
            # Swallowed, the shutdown would go on to await the next task and yield it
            # back here instead of raising.
            with pytest.raises(thrown):
                shutdown.throw(thrown())
        finally:
            await _release_and_reap(loops, started)


@pytest.mark.timeout(60)
async def test_a_lifespan_cancelled_during_shutdown_finishes_the_steps_then_ends_cancelled():
    """Issue #1549: the lifespan itself cancelled while it awaits a task gets the same
    CancelledError the task ends with. Shutdown still awaits the other tasks and
    closes NATS, then ends cancelled: the caller's cancellation is not swallowed
    (before this fix it was)."""
    nc = MagicMock()
    nc.close = AsyncMock()
    loops, context, started, stack = await _enter_holding("expiration_loop", nc=nc)
    exit_task = None
    with stack:
        try:
            exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            await _spin()
            assert not exit_task.done()
            exit_task.cancel()
            await _spin()
            assert exit_task.done(), "shutdown did not finish after it was cancelled"
            assert exit_task.cancelled(), "the lifespan swallowed its own cancellation"
            nc.close.assert_awaited_once()
            for name, task in loops.tasks.items():
                assert task.cancelled(), name
        finally:
            await _release_and_reap(loops, started, *([exit_task] if exit_task else []))


@pytest.mark.timeout(60)
async def test_startup_creates_the_schema_from_the_services_metadata():
    """The lifespan builds or checks the schema at startup with this service's
    metadata and its `DB_SCHEMA` setting (the unit suite's empty one here). The
    three loops are fakes that return at once, so shutdown has nothing to wait
    for whether or not it cancels them."""
    from app.database import Base, engine
    from app.main import lifespan

    create_all = AsyncMock()
    with (
        patch("app.main.create_all_and_stamp", new=create_all),
        patch("app.main.connect_nats", new=AsyncMock(side_effect=_NoBrokerHere())),
        patch("app.tasks.expiration.expiration_loop", new=AsyncMock()),
        patch("app.tasks.expiration.purpose_classify_loop", new=AsyncMock()),
        patch("app.main.run_outbox_relay", new=AsyncMock()),
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


class _LoopSurvivedCancel(BaseException):
    """A fake step raises this when it is called a second time: a loop that survived
    the cancel has reached its next tick. A BaseException, so a step's `except
    Exception` cannot swallow it, and the loop task ends at once."""


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
    budget: neither a step's `except Exception` nor anything around the sleep may
    catch the cancellation. A loop that does survive it and reaches its next tick
    calls a step a second time, which raises `_LoopSurvivedCancel` and ends the task,
    so the test fails at once and its clean-up never waits on a running loop. The
    steps are the four RES-SWEEP-10 names and the purpose-classify reconciler."""
    from contextlib import ExitStack

    from app.tasks import expiration

    calls: list[str] = []
    reached = asyncio.Event()

    def step(name):
        async def run():
            if name in calls:
                raise _LoopSurvivedCancel(f"{loop_name} ran {name} again after the cancel")
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
            assert task.done(), (
                f"{loop_name} was still running {_LOOP_BUDGET} event-loop iterations "
                f"after a cancel in {blocking or 'its sleep'}"
            )
            assert task.cancelled(), (
                f"{loop_name} did not end cancelled after a cancel in "
                f"{blocking or 'its sleep'}: {task.exception()!r}"
            )
        finally:
            if not task.done():
                task.cancel()
                await _spin()
            _retrieve(task)
