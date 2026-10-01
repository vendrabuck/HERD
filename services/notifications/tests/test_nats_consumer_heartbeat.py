"""Issue #911: notifications's consumer loop heartbeats every in-flight message.

The invariant: no in-flight JetStream message reaches ack_wait while its
handler is still running. These tests drive `nats_consumer.process_batch`, the
function every durable's consumer loop goes through, with a fake message that
counts `in_progress` calls and a handler that blocks on an event.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from app.services import nats_consumer

HEARTBEAT = 0.01
PRODUCTION_HEARTBEAT = nats_consumer.NATS_HEARTBEAT_SECONDS


class _FakeMsg:
    def __init__(self, num_delivered: int = 1, fail_in_progress: bool = False):
        self.data = json.dumps(
            {"event": "reservation.created", "reservation_id": str(uuid.uuid4())}
        ).encode()
        self.metadata = type("M", (), {"num_delivered": num_delivered, "sequence": None})
        self.ack = AsyncMock()
        self.nak = AsyncMock()
        self.beats = 0
        self._fail = fail_in_progress

    async def in_progress(self):
        self.beats += 1
        if self._fail:
            raise RuntimeError("broker hiccup")


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    monkeypatch.setattr(nats_consumer, "NATS_HEARTBEAT_SECONDS", HEARTBEAT)


async def _wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


def _blocking_handler(release: asyncio.Event, started: list | None = None):
    async def handler(event_data, session_factory, dedupe_key):
        if started is not None:
            started.append(1)
        await release.wait()

    return handler


@pytest.mark.asyncio
async def test_running_handler_is_heartbeated_then_stops_after_ack():
    msg = _FakeMsg()
    release = asyncio.Event()
    task = asyncio.create_task(
        nats_consumer.process_batch(
            [msg], AsyncMock(), object(), handler=_blocking_handler(release)
        )
    )
    await _wait_for(lambda: msg.beats >= 3)
    msg.ack.assert_not_awaited()

    release.set()
    await task
    msg.ack.assert_awaited_once()
    frozen = msg.beats
    await asyncio.sleep(HEARTBEAT * 5)
    assert msg.beats == frozen


@pytest.mark.asyncio
async def test_handler_raising_naks_and_stops_the_heartbeat():
    msg = _FakeMsg()

    async def handler(event_data, session_factory, dedupe_key):
        await asyncio.sleep(HEARTBEAT * 5)
        raise RuntimeError("downstream unavailable")

    await nats_consumer.process_batch([msg], AsyncMock(), object(), handler=handler)

    assert msg.beats >= 1
    msg.nak.assert_awaited_once()
    msg.ack.assert_not_awaited()
    frozen = msg.beats
    await asyncio.sleep(HEARTBEAT * 5)
    assert msg.beats == frozen


@pytest.mark.asyncio
async def test_handler_timeout_naks_and_stops_the_heartbeat():
    msg = _FakeMsg()

    async def handler(event_data, session_factory, dedupe_key):
        await asyncio.wait_for(asyncio.sleep(10), timeout=HEARTBEAT * 5)

    await nats_consumer.process_batch([msg], AsyncMock(), object(), handler=handler)

    assert msg.beats >= 1
    msg.nak.assert_awaited_once()
    frozen = msg.beats
    await asyncio.sleep(HEARTBEAT * 5)
    assert msg.beats == frozen


@pytest.mark.asyncio
async def test_cancelled_mid_message_leaks_no_heartbeat_task():
    msg = _FakeMsg()
    release = asyncio.Event()
    started: list = []
    before = asyncio.all_tasks()
    task = asyncio.create_task(
        nats_consumer.process_batch(
            [msg], AsyncMock(), object(), handler=_blocking_handler(release, started)
        )
    )
    await _wait_for(lambda: started and msg.beats >= 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leaked = {t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()}
    assert not leaked
    frozen = msg.beats
    await asyncio.sleep(HEARTBEAT * 5)
    assert msg.beats == frozen


@pytest.mark.asyncio
async def test_message_queued_behind_a_slow_one_is_heartbeated_too():
    first, second = _FakeMsg(), _FakeMsg()
    release = asyncio.Event()
    task = asyncio.create_task(
        nats_consumer.process_batch(
            [first, second], AsyncMock(), object(), handler=_blocking_handler(release)
        )
    )
    await _wait_for(lambda: first.beats >= 2 and second.beats >= 2)
    release.set()
    await task
    first.ack.assert_awaited_once()
    second.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_failing_in_progress_does_not_wedge_the_batch():
    msg = _FakeMsg(fail_in_progress=True)

    async def handler(event_data, session_factory, dedupe_key):
        await asyncio.sleep(HEARTBEAT * 5)

    await nats_consumer.process_batch([msg], AsyncMock(), object(), handler=handler)

    assert msg.beats >= 1
    msg.ack.assert_awaited_once()


def test_heartbeat_interval_is_below_ack_wait():
    """Pin the invariant so a future ack_wait change cannot silently break it
    (mirrors execution's test_nats_consumer_heartbeat.py)."""
    assert PRODUCTION_HEARTBEAT < nats_consumer.NATS_ACK_WAIT_SECONDS
    assert PRODUCTION_HEARTBEAT >= 1
