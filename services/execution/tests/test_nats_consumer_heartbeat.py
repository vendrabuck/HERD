"""Tests for the issue #317 consumer fixes: off-loop driver execution and the
per-message ack heartbeat that keeps a long provisioning handler from being
redelivered mid-flight, plus the issue #944 cancel regression through the real
consumer loop. The loop runs on the shared herd_common.jetstream helper."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services import nats_consumer
from app.services.nats_consumer import _run_sandbox


class _FakeMsg:
    def __init__(self):
        self.beats = 0

    async def in_progress(self):
        self.beats += 1


@pytest.mark.asyncio
async def test_run_sandbox_runs_off_the_event_loop(monkeypatch):
    """The sandbox call executes on a worker thread, not the event-loop thread,
    so a blocking driver cannot stall the consumer loop (issue #317)."""
    loop_thread = threading.get_ident()
    seen = {}

    def fake_execute(driver_path, action, context, password_keys=None):
        seen["thread"] = threading.get_ident()
        seen["args"] = (driver_path, action, context, password_keys)
        return {"success": True, "action": action}

    monkeypatch.setattr(
        "app.services.driver_sandbox.execute_driver_method", fake_execute, raising=False
    )

    result = await _run_sandbox("/pkg", "login", {"k": "v"}, password_keys=["p"])

    assert result == {"success": True, "action": "login"}
    # Ran on a different thread than the event loop.
    assert seen["thread"] != loop_thread
    assert seen["args"] == ("/pkg", "login", {"k": "v"}, ["p"])


@pytest.mark.asyncio
async def test_process_batch_heartbeats_a_slow_message_until_it_settles(monkeypatch):
    """Through execution's own process_batch: a message waiting behind a slow
    handler is heartbeated while it waits, and stops being heartbeated once it
    settles (the shared helper's cancel and error semantics are proven in
    services/common/tests/test_jetstream.py)."""
    monkeypatch.setattr(nats_consumer, "NATS_HEARTBEAT_SECONDS", 0.01)
    slow, queued = _FakeMsg(), _FakeMsg()
    release = asyncio.Event()
    beats_at_settle = {}

    async def fake_process(msg, js, handler, session_factory):
        if msg is slow:
            await release.wait()
            beats_at_settle["slow"] = slow.beats
        return "ack"

    monkeypatch.setattr(nats_consumer, "process_reservation_message", fake_process)

    batch = asyncio.create_task(nats_consumer.process_batch([slow, queued], None, None))
    await asyncio.sleep(0.06)
    assert slow.beats >= 1
    assert queued.beats >= 1
    release.set()
    await batch

    assert beats_at_settle["slow"] >= 1
    # Both settled: nothing keeps beating after the batch returns.
    frozen = (slow.beats, queued.beats)
    await asyncio.sleep(0.05)
    assert (slow.beats, queued.beats) == frozen


@pytest.mark.asyncio
async def test_process_batch_logs_a_raising_message_and_keeps_draining(monkeypatch):
    """An exception escaping one message's handler is logged and the next
    message in the batch is still processed."""
    first, second = _FakeMsg(), _FakeMsg()
    seen = []

    async def fake_process(msg, js, handler, session_factory):
        seen.append(msg)
        if msg is first:
            raise RuntimeError("ack failed")
        return "ack"

    monkeypatch.setattr(nats_consumer, "process_reservation_message", fake_process)

    await nats_consumer.process_batch([first, second], None, None)

    assert seen == [first, second]


@pytest.mark.asyncio
async def test_heartbeat_interval_is_below_ack_wait():
    """The heartbeat must fire well within ack_wait or a message would time out
    between beats. Pin the invariant so a future ack_wait change cannot silently
    break it."""
    assert nats_consumer.NATS_HEARTBEAT_SECONDS < nats_consumer.NATS_ACK_WAIT_SECONDS
    assert nats_consumer.NATS_HEARTBEAT_SECONDS >= 1


def test_ack_wait_is_the_real_effective_window_no_backoff_shrinks_it():
    """Issue #895: a ConsumerConfig.backoff list used to make JetStream silently
    replace the server-side ack_wait with backoff[0] (measured 1s against
    nats-server 2.10.29), so the heartbeat assertion above protected nothing
    against the EFFECTIVE window. ack_wait must still be a real 30s, and this
    module must not reintroduce a NATS_BACKOFF_SECONDS-style constant."""
    assert nats_consumer.NATS_ACK_WAIT_SECONDS == 30
    assert not hasattr(nats_consumer, "NATS_BACKOFF_SECONDS")
    assert not hasattr(nats_consumer, "_keep_messages_alive")


class _OneBatchPullSub:
    """Fake pull subscription: fetch() returns the queued batch once, then
    behaves like an idle consumer (a stub TimeoutError after a short sleep)."""

    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def fetch(self, batch, timeout=None):
        if self._msgs:
            msgs, self._msgs = self._msgs, []
            return msgs
        await asyncio.sleep(0.02)
        raise _StubTimeoutError()


class _StubTimeoutError(Exception):
    """Stands in for nats.errors.TimeoutError so the loop's except clause (which
    references it by class) matches during a stubbed test."""


@pytest.mark.asyncio
async def test_consumer_cancelled_while_batch_finishes_still_ends():
    """Issue #944: a cancel that lands while the loop waits for its just-cancelled
    heartbeat (the one-iteration window after a batch settles) must end the
    consumer task. The old inline `try: await heartbeat / except CancelledError:
    pass` could not tell that cancel from the heartbeat's own and kept looping,
    so stop_nats_consumer waited on a task that never ended. Driven through the
    real loop built by start_nats_consumer; mirrors the common helper's
    regression test."""
    app = MagicMock()
    app.state = MagicMock()
    js = AsyncMock()
    nc = AsyncMock()
    nc.jetstream = MagicMock(return_value=js)
    js.pull_subscribe = AsyncMock(return_value=_OneBatchPullSub([MagicMock()]))

    nats_module = MagicMock()
    nats_module.connect = AsyncMock(return_value=nc)
    nats_module.errors.TimeoutError = _StubTimeoutError
    modules = {"nats": nats_module, "nats.js": MagicMock(), "nats.js.api": MagicMock()}

    settled = asyncio.Event()

    async def fake_process(msg, js, handler, session_factory):
        settled.set()
        return "ack"

    with (
        patch.dict("sys.modules", modules),
        patch("app.services.nats_consumer.ensure_stream_exists", AsyncMock()),
        patch("app.services.nats_consumer.process_reservation_message", fake_process),
    ):
        await nats_consumer.start_nats_consumer(app)
        task = app.state.nats_consumer_task
        try:
            await asyncio.wait_for(settled.wait(), 3)
            # The loop task is now parked waiting for its cancelled heartbeat.
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=1.0)
            assert task in done, "consumer task swallowed a cancel and kept running"
            assert task.cancelled()
        finally:
            if not task.done():
                # A swallowed cancel leaves the loop idling in fetch; cancel again
                # (outside the window) so a failing run does not leak the task.
                task.cancel()
                await asyncio.wait({task}, timeout=1.0)
