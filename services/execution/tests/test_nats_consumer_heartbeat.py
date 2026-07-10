"""Tests for the issue #317 consumer fixes: off-loop driver execution and the
per-message ack heartbeat that keeps a long provisioning handler from being
redelivered mid-flight."""

import asyncio
import threading

import pytest
from app.services import nats_consumer
from app.services.nats_consumer import _keep_messages_alive, _run_sandbox


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
async def test_keep_messages_alive_heartbeats_until_settled(monkeypatch):
    """Every in-flight message is heartbeated on the interval; a message removed
    from the list (settled/acked) stops receiving heartbeats while the others
    continue."""
    m1, m2 = _FakeMsg(), _FakeMsg()
    in_flight = [m1, m2]

    task = asyncio.create_task(_keep_messages_alive(in_flight, interval=0.01))
    # Let several heartbeat cycles run.
    await asyncio.sleep(0.05)
    assert m1.beats >= 1
    assert m2.beats >= 1

    # Settle m1: it should stop being heartbeated, m2 should keep going.
    in_flight.remove(m1)
    frozen = m1.beats
    before_m2 = m2.beats
    await asyncio.sleep(0.05)

    assert m1.beats == frozen
    assert m2.beats > before_m2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_keep_messages_alive_swallows_in_progress_errors():
    """A failing in_progress must never wedge the heartbeat loop: a raising
    message is skipped and healthy ones keep beating."""

    class _Raiser:
        async def in_progress(self):
            raise RuntimeError("broker hiccup")

    good = _FakeMsg()
    in_flight = [_Raiser(), good]

    task = asyncio.create_task(_keep_messages_alive(in_flight, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The raiser did not stop the good message's heartbeat.
    assert good.beats >= 1


@pytest.mark.asyncio
async def test_heartbeat_interval_is_below_ack_wait():
    """The heartbeat must fire well within ack_wait or a message would time out
    between beats. Pin the invariant so a future ack_wait change cannot silently
    break it."""
    assert nats_consumer.NATS_HEARTBEAT_SECONDS < nats_consumer.NATS_ACK_WAIT_SECONDS
    assert nats_consumer.NATS_HEARTBEAT_SECONDS >= 1
